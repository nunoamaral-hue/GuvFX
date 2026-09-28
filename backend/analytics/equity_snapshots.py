"""analytics.equity_snapshots — durable equity ledger writer + truthful account/portfolio equity curves.

OBSERVATION ONLY. A snapshot is written only from an authoritative, identity-verified, FRESH broker read; values
are never derived from Trade rows or interpolated (data.md: raw evidence is immutable, no fabrication). The
portfolio curve aligns per-account observations into time buckets and honestly degrades to PARTIAL/STALE when an
account's observation is missing or too old — it never presents an incomplete portfolio point as complete.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from analytics import portfolio_fx as FX
from analytics.models import AccountEquitySnapshot

logger = logging.getLogger("guvfx.equity_snapshots")

DEFAULT_MIN_INTERVAL_S = 270      # ~4.5min throttle so a ~5min cadence never double-writes on retry/overlap (A4)
DEFAULT_BUCKET_S = 300            # 5-minute portfolio buckets (A7)
DEFAULT_FRESHNESS_S = 900         # an account observation older than 15min is STALE for a bucket (A7)


def _dec(x) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None


def capture_account_snapshot(account, *, balance, equity, currency=None, observed_login="", observed_server="",
                             floating_pnl=None, margin=None, free_margin=None, margin_level=None,
                             observed_at=None, source="account_read", min_interval_seconds=DEFAULT_MIN_INTERVAL_S,
                             verify_identity=True):
    """Persist one broker-observed equity snapshot for ``account``, fail-closed and idempotent.

    Returns ``(row_or_None, reason)`` where reason ∈ {created, identity_refused, no_values, throttled, duplicate}.
    Identity safety (A2): when ``verify_identity`` the observed login/server must match the account's expected
    identity or the row is refused (a mis-routed/stale runtime must never write a valid-looking row for the wrong
    account). Idempotency/throttle (A4): skip if a snapshot already exists within ``min_interval_seconds``.
    """
    if verify_identity:
        from execution.snapshot_transport import verify_snapshot_identity
        # Strict for a durable write: login MUST match, and a provided server MUST match too (require_server) so a
        # mis-routed runtime cannot persist a valid-looking row for the wrong account/server.
        idc = verify_snapshot_identity(account, observed_login, observed_server, require_server=True)
        if not idc.ok:
            logger.warning("equity snapshot identity refused acct %s: %s", getattr(account, "id", None), idc.reason_code)
            return None, "identity_refused"

    bal, eq = _dec(balance), _dec(equity)
    if bal is None and eq is None:
        return None, "no_values"           # never write an empty/manufactured row

    now = observed_at or timezone.now()
    cur = FX.normalise_currency(currency or getattr(account, "account_currency", None))
    floating = _dec(floating_pnl)
    if floating is None and eq is not None and bal is not None:
        # Approximation: equity = balance + floating (EXACT for a cash/demo account with no broker credit; it
        # OVERSTATES real floating by the credit amount on a credited account — the bridge account snapshot does
        # not currently expose credit or a direct floating figure). The live Open-Trades panel is the authoritative
        # floating source; this ledger field is a secondary convenience. Documented in KNOWN_ISSUES.
        floating = eq - bal
    try:
        # Serialize concurrent/overlapping captures for THIS account so the check-then-act throttle can't be raced
        # by two runs (cron overlap / retry / restart) into two near-simultaneous rows inside the throttle window.
        with transaction.atomic():
            from trading.models import TradingAccount
            TradingAccount.objects.select_for_update().filter(id=account.id).first()
            if min_interval_seconds:
                cutoff = now - timedelta(seconds=min_interval_seconds)
                if AccountEquitySnapshot.objects.filter(
                        trading_account_id=account.id, observed_at__gte=cutoff).exists():
                    return None, "throttled"
            row = AccountEquitySnapshot.objects.create(
                trading_account_id=account.id, observed_at=now, account_currency=cur,
                balance=bal, equity=eq, floating_pnl=floating, margin=_dec(margin),
                free_margin=_dec(free_margin), margin_level=_dec(margin_level),
                observed_login=str(observed_login or ""), observed_server=str(observed_server or ""), source=source)
        return row, "created"
    except IntegrityError:
        return None, "duplicate"           # exact-instant unique collision (concurrent write) — idempotent no-op


def account_equity_curve(account, *, since=None, max_points=1000) -> dict:
    """A single account's observed equity curve from durable snapshots (chronological). Native == reporting for USD.
    Returns ``{account_id, currency, points:[{t, equity, balance}], count, state}``. ``state`` ∈
    {BUILDING (0 pts), SINGLE (1 pt), OK (2+)}. Never interpolates — the curve begins when snapshots begin."""
    qs = AccountEquitySnapshot.objects.filter(trading_account_id=account.id)
    if since:
        qs = qs.filter(observed_at__gte=since)
    rows = list(qs.order_by("observed_at").values("observed_at", "equity", "balance", "account_currency"))
    if len(rows) > max_points:
        step = (len(rows) + max_points - 1) // max_points     # uniform downsample, keep last point
        rows = rows[::step] + ([rows[-1]] if rows[-1] not in rows[::step] else [])
    points = [{"t": r["observed_at"].isoformat(),
               "equity": (float(r["equity"]) if r["equity"] is not None else None),
               "balance": (float(r["balance"]) if r["balance"] is not None else None)} for r in rows]
    # Currency label from the OBSERVED snapshot rows (account_currency on the model is not populated in prod), so a
    # non-USD account's native points are never mislabeled USD. Fall back to USD only when there are no rows.
    cur = FX.normalise_currency(rows[-1]["account_currency"]) if rows else FX.USD
    state = "BUILDING" if len(points) == 0 else ("SINGLE" if len(points) == 1 else "OK")
    return {"account_id": account.id, "currency": cur, "points": points, "count": len(points), "state": state}


def portfolio_equity_curve(accounts, *, rate_source: FX.FxRateSource, since=None,
                           bucket_seconds=DEFAULT_BUCKET_S, freshness_seconds=DEFAULT_FRESHNESS_S,
                           max_points=1000) -> dict:
    """Truthful ALL-scope portfolio equity curve (A7): at each time bucket, sum the MOST-RECENT VALID account equity
    within a strict freshness window, converted to USD. Never averages or concatenates account curves; never sums
    materially stale observations without flagging. A bucket missing/stale for some accounts is marked PARTIAL and
    surfaces those accounts; a bucket with no fresh observation for any account is skipped. USD accounts convert
    trivially; a non-USD account with no historical rate makes that bucket PARTIAL (never fabricated).

    Returns ``{points:[{t, equity_usd, basis, complete, present, total, stale_accounts}], count, state, accounts,
    reporting_currency}``. ``state`` ∈ {BUILDING, SINGLE, OK}.
    """
    ids = [a.id for a in accounts]
    if not ids:
        return {"points": [], "count": 0, "state": "BUILDING", "accounts": 0, "reporting_currency": FX.USD}

    qs = AccountEquitySnapshot.objects.filter(trading_account_id__in=ids)
    if since:
        qs = qs.filter(observed_at__gte=since)
    snaps = list(qs.order_by("observed_at").values("trading_account_id", "observed_at", "equity", "account_currency"))
    if not snaps:
        return {"points": [], "count": 0, "state": "BUILDING", "accounts": len(ids), "reporting_currency": FX.USD}

    t0, tN = snaps[0]["observed_at"], snaps[-1]["observed_at"]
    step = timedelta(seconds=bucket_seconds)
    fresh = timedelta(seconds=freshness_seconds)
    # Bucket edges from first to last observation (cap the count; downsample the STEP, never fabricate points).
    span = (tN - t0).total_seconds()
    n_buckets = int(span // bucket_seconds) + 1
    if n_buckets > max_points:
        step = timedelta(seconds=(int(span // max_points) + 1))
    edges = []
    t = t0
    while t <= tN:
        edges.append(t)
        t += step
    if edges[-1] != tN:
        edges.append(tN)

    # Per-account EQUITY-bearing snapshots (skip balance-only rows so a newer equity=None row can't shadow an older
    # usable one — "most recent VALID observation"). Sorted times per account enable an O(log n) bisect lookup
    # instead of rescanning the whole list for every (bucket, account) — bounded for ~20 accts × a year of 5-min data.
    import bisect
    by_acct: dict = {}
    for s in snaps:
        if s["equity"] is None:
            continue
        by_acct.setdefault(s["trading_account_id"], []).append(s)
    acct_times = {aid: [s["observed_at"] for s in lst] for aid, lst in by_acct.items()}  # each already chronological

    def latest_valid(aid, at):
        times = acct_times.get(aid)
        if not times:
            return None
        i = bisect.bisect_right(times, at) - 1
        if i < 0:
            return None
        s = by_acct[aid][i]
        return s if s["observed_at"] >= at - fresh else None   # None when the newest ≤ edge is too stale

    points = []
    for edge in edges:
        total_usd = 0.0
        present = 0
        partial = False
        stale = []
        for aid in ids:
            s = latest_valid(aid, edge)
            if s is None:
                partial = True
                stale.append(aid)
                continue
            # Convert with the snapshot's OWN native currency (account_currency on the model is unpopulated), so a
            # non-USD account degrades this bucket to PARTIAL instead of being fabricated as USD (H-CURRENCY).
            usd = FX.to_usd(s["equity"], FX.normalise_currency(s["account_currency"]), rate_source)
            if usd["ok"]:
                total_usd += usd["usd"]
                present += 1
            else:
                partial = True                # non-USD w/ no historical rate -> can't convert this account's point
                stale.append(aid)
        if present == 0:
            continue                          # no fresh, convertible observation for any account -> skip the bucket
        points.append({"t": edge.isoformat(), "equity_usd": round(total_usd, 2),
                       "basis": ("PARTIAL" if partial else "USD"), "complete": (not partial),
                       "present": present, "total": len(ids), "stale_accounts": stale})

    # Distinguish "no data yet" (BUILDING) from "data exists but currently unusable/unconvertible" (PARTIAL) so the
    # UI never says "history is building" when snapshots are actually present.
    if not points:
        state = "PARTIAL"                     # snaps is non-empty here (empty case returned above) but none usable
    else:
        state = "SINGLE" if len(points) == 1 else "OK"
    return {"points": points, "count": len(points), "state": state, "accounts": len(ids),
            "reporting_currency": FX.USD}
