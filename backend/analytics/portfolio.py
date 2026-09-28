"""analytics.portfolio — read-only multi-account portfolio projection for the dashboard.

OBSERVATION ONLY. Nothing here mutates account state, strategy, magic, sizing, execution, or the broker; it never
places or modifies an order. It resolves the authenticated member's owned, non-decommissioned accounts, aggregates
their balance/equity/P&L into USD (honestly — see ``portfolio_fx``), computes portfolio performance from the
UNDERLYING closed-trade set (never by averaging per-account percentages), and lists live open positions with
account attribution.

Live broker figures (balance/equity/currency and open positions) are read per-account from that account's OWN
per-tenant bridge via injected fetchers (the views wire the identity-firewalled, fail-closed readers in
``views_trade_history``); a fetch failure marks that account ``stale`` and never drops it silently.
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from django.utils import timezone

from analytics import portfolio_fx as FX
from trading.models import Trade, TradingAccount

logger = logging.getLogger("guvfx.portfolio")

_MAGIC_BASE = 1_000_000_000   # magic = base + StrategyAssignment.id (Phase-A allocator)

# ``source_stage`` is a COMMENT-TAG CLASSIFIER inferred at ingest (TEST/LIVE/UNKNOWN — default UNKNOWN when the
# broker deal comment carries no recognised tag). It is NOT a performance dimension: virtually every real
# broker-observed trade is UNKNOWN (the TI/Wayond comments are "WAY.../[tp .]/[sl .]", not stage-tagged). So the
# member-facing analytics must NOT filter by it — doing so silently hides real closed trades and reports $0
# (P0 2026-09-28: dashboard filtered stage="LIVE" and showed Daily/Net/WinRate = 0 despite real broker profits).
# ``ALL`` (the default) applies NO source_stage filter, matching the established trade-history endpoint. A stage is
# only filtered when a caller explicitly asks for exactly "LIVE" or "TEST".
STAGE_ALL = "ALL"


def owned_accounts(user):
    """The dashboard portfolio universe: the user's OWNED, NON-decommissioned accounts (tombstones excluded).
    The accounts viewset does not exclude tombstones itself, so this helper is the canonical row source."""
    return TradingAccount.objects.filter(user=user, disconnected_at__isnull=True).order_by("id")


def resolve_scope(user, scope: Optional[str]):
    """Return ``(accounts, error_status)``. ``scope='ALL'`` -> all owned non-decommissioned; a numeric id -> that
    single OWNED account. Fail-closed: a foreign/nonexistent id returns ``(None, 404)`` (identical to nonexistent,
    so there is no account-existence oracle); a malformed scope returns ``(None, 400)``. Never trusts the client."""
    qs = owned_accounts(user)
    s = (scope or "ALL").strip()
    if s.upper() == "ALL":
        return list(qs), None
    try:
        aid = int(s)
    except (TypeError, ValueError):
        return None, 400
    acc = qs.filter(id=aid).first()
    if acc is None:
        return None, 404
    return [acc], None


def mask_account(number: Optional[str]) -> str:
    n = (number or "").strip()
    return ("••••" + n[-4:]) if len(n) >= 4 else "••••"


def _net(t: Trade) -> float:
    return float((t.profit or 0) + (t.commission or 0) + (t.swap or 0))


def _account_is_usd(account) -> bool:
    return FX.normalise_currency(getattr(account, "account_currency", None)) == FX.USD


def _partition_usd(accounts, observed_nonusd_ids=None):
    """Split ``accounts`` into (USD-denominated, non-USD). The realized-P&L path can only be summed within a single
    denomination, so non-USD accounts are EXCLUDED from the sum and surfaced (never silently added).

    ``account_currency`` is not populated in production (always NULL -> USD), so a DB-only guard is inert. When the
    caller knows each account's OBSERVED currency (from the live balance snapshot — the same source the balance
    aggregate uses), it passes ``observed_nonusd_ids`` so the realized path degrades a non-USD account to PARTIAL
    exactly as the balance path does, instead of silently summing it as USD."""
    nonusd = observed_nonusd_ids or set()
    def _is_usd(a):
        return _account_is_usd(a) and a.id not in nonusd
    usd = [a for a in accounts if _is_usd(a)]
    non_usd = [a for a in accounts if not _is_usd(a)]
    return usd, non_usd


def _trade_currencies_all_usd(ids, *, stage: str) -> bool:
    """True unless some in-scope CLOSED trade carries a non-USD (set, non-blank) profit/commission/swap currency.

    Blank/None is treated as USD (estate default). A single trade stores three INDEPENDENT currency fields, so
    ``_net`` (profit+commission+swap) is only a valid single-denomination number when all three are USD. One
    existence query; cheap. This is the guard that stops the realized path from ever summing mixed denominations
    (H1) — the exact invariant ``portfolio_fx`` was written to protect on the balance/open-P/L paths."""
    from django.db.models import Q
    if not ids:
        return True
    qs = Trade.objects.filter(account_id__in=ids, close_time__isnull=False)
    if stage in ("LIVE", "TEST"):
        qs = qs.filter(source_stage=stage)
    bad = Q()
    for f in ("profit_currency", "commission_currency", "swap_currency"):
        bad |= (Q(**{f"{f}__isnull": False}) & ~Q(**{f: ""}) & ~Q(**{f"{f}__iexact": FX.USD}))
    return not qs.filter(bad).exists()


def _empty_metrics(total: int, basis: str, excluded) -> dict:
    """Counts-only metrics with every MONETARY/classified field withheld — used when the realized figures cannot be
    trusted as a single USD denomination (fail-closed: never emit a mixed-currency money number)."""
    return {
        "total_trades": total, "wins": None, "losses": None, "breakeven": None,
        "win_rate_pct": None, "profit_factor": None, "profit_factor_infinite": False,
        "expectancy": None, "net_pnl_total": None, "gross_profit": None, "gross_loss": None,
        "max_drawdown_money": None,
        "breakeven_rule": "net>0 win; net<0 loss; net==0 breakeven (in total, not win/loss)",
        "basis": basis, "excluded_accounts": list(excluded),
    }


def portfolio_metrics(accounts, *, stage: str = STAGE_ALL, observed_nonusd_ids=None) -> dict:
    """Portfolio performance computed from the UNDERLYING closed-trade set across ``accounts`` — NOT averaged.

    Definitions (single documented breakeven rule): a closed position's net = profit+commission+swap;
    net>0 = win, net<0 = loss, net==0 = breakeven (counted in the total, but neither win nor loss).
    win_rate = wins/total; profit_factor = Σ gross_profit / Σ |gross_loss| (None if there are no losing trades and
    no profit; flagged infinite if profit but zero loss); expectancy = Σ net / total; max_drawdown = peak-to-trough
    on the chronological cumulative-net curve.

    CURRENCY-HONEST (H1): non-USD accounts are excluded and surfaced under ``excluded_accounts`` (their realized
    P&L cannot be converted — no vetted FX feed), and if any included trade carries a non-USD currency the whole
    money block degrades to ``basis="PARTIAL"`` with the monetary fields withheld rather than summed across
    denominations. ``basis`` is ``"USD"`` only when every account is USD and no trade currency deviates.
    """
    usd_accounts, non_usd = _partition_usd(accounts, observed_nonusd_ids)
    excluded = [a.id for a in non_usd]
    ids = [a.id for a in usd_accounts]

    if not _trade_currencies_all_usd(ids, stage=stage):
        # Mixed trade currencies within the (USD-account) set: cannot classify or sum honestly -> counts only.
        cqs = Trade.objects.filter(account_id__in=ids, close_time__isnull=False)
        if stage in ("LIVE", "TEST"):
            cqs = cqs.filter(source_stage=stage)
        return _empty_metrics(cqs.count(), "PARTIAL", excluded)

    qs = Trade.objects.filter(account_id__in=ids, close_time__isnull=False)
    if stage in ("LIVE", "TEST"):
        qs = qs.filter(source_stage=stage)
    # Deterministic order: close_time has 1-second precision, so a same-second cluster (e.g. a multi-leg signal's
    # legs) needs a stable secondary key or max_drawdown_money becomes non-reproducible across identical data.
    rows = list(qs.order_by("close_time", "id").only("profit", "commission", "swap", "close_time", "account_id"))

    total = len(rows)
    wins = losses = breakeven = 0
    gross_profit = gross_loss = 0.0
    net_total = 0.0
    peak = cum = max_dd = 0.0
    for t in rows:
        n = _net(t)
        net_total += n
        if n > 0:
            wins += 1
            gross_profit += n
        elif n < 0:
            losses += 1
            gross_loss += -n
        else:
            breakeven += 1
        cum += n
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    if gross_loss > 0:
        pf, pf_inf = round(gross_profit / gross_loss, 3), False
    elif gross_profit > 0:
        pf, pf_inf = None, True                      # winners, no losers -> "infinite"
    else:
        pf, pf_inf = None, False                     # no completed trades either way
    return {
        "total_trades": total, "wins": wins, "losses": losses, "breakeven": breakeven,
        "win_rate_pct": round((wins / total * 100.0), 2) if total else 0.0,
        "profit_factor": pf, "profit_factor_infinite": pf_inf,
        "expectancy": round(net_total / total, 2) if total else 0.0,
        "net_pnl_total": round(net_total, 2),
        "gross_profit": round(gross_profit, 2), "gross_loss": round(gross_loss, 2),
        "max_drawdown_money": round(max_dd, 2),
        "breakeven_rule": "net>0 win; net<0 loss; net==0 breakeven (in total, not win/loss)",
        # basis is USD only when the whole scope is a single USD denomination; excluded non-USD accounts surfaced.
        "basis": ("USD" if not excluded else "PARTIAL"), "excluded_accounts": excluded,
    }


def daily_realized_pnl(accounts, *, stage: str = STAGE_ALL, observed_nonusd_ids=None):
    """Today's REALIZED net P&L across accounts (closed positions closed on today's calendar date). Distinct from
    floating P/L.

    ``today`` is ``timezone.now().date()``; with TIME_ZONE=UTC that is the UTC calendar date. NOTE (documented, not
    changed here): ``close_time`` is stored from the MT5 deal timestamp which is the broker SERVER time, so this
    window is effectively a broker-server calendar day; a true-UTC or member-local frame is a platform-wide
    follow-up requiring an ingest-time correction + backfill (see KNOWN_ISSUES). Mid-day trades (the norm) are
    unaffected; only trades within the broker/UTC offset of midnight could bucket a day off.

    Returns ``(value|None, basis)`` — currency-honest like ``portfolio_metrics``: non-USD accounts are excluded
    (``basis="PARTIAL"``), and a mixed trade currency withholds the number entirely (``None``, ``"PARTIAL"``)
    rather than summing across denominations (H1)."""
    usd_accounts, non_usd = _partition_usd(accounts, observed_nonusd_ids)
    excluded = [a.id for a in non_usd]
    ids = [a.id for a in usd_accounts]
    if not _trade_currencies_all_usd(ids, stage=stage):
        return None, "PARTIAL"
    today = timezone.now().date()
    qs = Trade.objects.filter(account_id__in=ids, close_time__date=today)
    if stage in ("LIVE", "TEST"):
        qs = qs.filter(source_stage=stage)
    val = round(sum(_net(t) for t in qs.only("profit", "commission", "swap")), 2)
    return val, ("USD" if not excluded else "PARTIAL")


def account_net_pnl(account, *, stage: str = STAGE_ALL, observed_usd=None):
    """A single account's realized net P&L, or ``None`` when it cannot be reported as a trustworthy USD figure
    (non-USD account, or a trade with a non-USD currency component) — never a mixed-denomination sum.

    ``observed_usd`` (from the live balance snapshot currency) overrides the never-populated ``account_currency``
    when the caller knows it: False -> None (excluded, consistent with the balance path); True -> USD."""
    is_usd = observed_usd if observed_usd is not None else _account_is_usd(account)
    if not is_usd or not _trade_currencies_all_usd([account.id], stage=stage):
        return None
    qs = Trade.objects.filter(account_id=account.id, close_time__isnull=False)
    if stage in ("LIVE", "TEST"):
        qs = qs.filter(source_stage=stage)
    return round(sum(_net(t) for t in qs.only("profit", "commission", "swap")), 2)


def _strategy_for_magic(account, magic) -> str:
    """Map an MT5 position magic (base + StrategyAssignment.id) to a strategy name for THIS account only. Returns ''
    when unknown/foreign (never guesses across accounts)."""
    try:
        m = int(magic)
    except (TypeError, ValueError):
        return ""
    if m <= _MAGIC_BASE:
        return ""
    from strategies.models import StrategyAssignment
    asn = StrategyAssignment.objects.filter(id=m - _MAGIC_BASE, account_id=account.id).select_related("strategy").first()
    return getattr(getattr(asn, "strategy", None), "name", "") or ""


def open_positions(accounts, *, fetch_positions: Callable, rate_source: FX.FxRateSource):
    """Live open positions across ``accounts`` with account attribution. ``fetch_positions(account)`` returns the
    broker position list (or None on failure). Position identity is ACCOUNT-SCOPED (``<account_id>:<ticket>``) so
    ticket 123 on two brokers never collides. Floating P/L is normalised to USD (PARTIAL if a currency can't
    convert). Returns ``(rows, per_account_open)`` where per_account_open maps account_id -> {open_count,
    open_pl_usd, open_pl_basis, stale}."""
    rows: List[dict] = []
    per_account: dict = {}
    for a in accounts:
        cur = FX.normalise_currency(getattr(a, "account_currency", None))
        res = None
        try:
            res = fetch_positions(a)
        except Exception:  # noqa: BLE001 — a broker read must never break the dashboard
            logger.warning("portfolio: open-positions fetch raised for account %s", a.id)
            res = None
        if res is None:
            per_account[a.id] = {"open_count": 0, "open_pl_usd": None, "open_pl_basis": "STALE",
                                 "stale": True, "pl_items": []}
            continue
        acct_rows = []
        pl_items = []
        for p in res:
            try:
                magic = p.get("magic")
                native_pl = float(p.get("profit")) if p.get("profit") is not None else None
            except (TypeError, ValueError):
                native_pl = None
                magic = p.get("magic")
            usd = FX.to_usd(native_pl, cur, rate_source)
            acct_rows.append({
                "position_id": f"{a.id}:{p.get('ticket')}",
                "ticket": p.get("ticket"),
                "account_id": a.id,
                "broker": a.broker_name,
                "account_masked": mask_account(a.account_number),
                "symbol": p.get("symbol"),
                "side": p.get("side") or ("BUY" if p.get("type") == 0 else "SELL"),
                "volume": p.get("volume"),
                "open_price": p.get("price_open"),
                "current_price": p.get("price_current"),
                "pl_native": native_pl,
                "pl_native_currency": cur,
                "pl_usd": (round(usd["usd"], 2) if usd["ok"] else None),
                "strategy": _strategy_for_magic(a, magic),
            })
            pl_items.append({"account_id": a.id, "amount": native_pl, "currency": cur})
        rows.extend(acct_rows)
        agg = FX.aggregate_usd(pl_items, rate_source)
        per_account[a.id] = {"open_count": len(acct_rows),
                             "open_pl_usd": round(agg.total_usd, 2),
                             "open_pl_basis": agg.basis, "stale": False,
                             # native per-position items kept so the TOP-LEVEL aggregate can preserve PARTIAL
                             # rather than re-summing an already-USD number (which would always read "USD"). (M1)
                             "pl_items": pl_items}
    return rows, per_account


def portfolio_open_pl(per_account, rate_source: FX.FxRateSource):
    """Top-level floating-P/L aggregate that PRESERVES per-account PARTIAL and STALE (M1).

    Aggregates the NATIVE per-position items across all non-stale accounts (so a non-convertible currency degrades
    the whole total to PARTIAL, instead of the previous re-aggregation of pre-summed USD which always reported
    "USD"), then forces PARTIAL and surfaces every STALE account whose positions could not be read at all — a
    partial floating-P/L total is never presented as complete. Returns ``(UsdAggregate, stale_ids)``."""
    items: List[dict] = []
    stale_ids: List = []
    for aid, v in per_account.items():
        if v.get("stale"):
            stale_ids.append(aid)
            continue
        items.extend(v.get("pl_items") or [])
    agg = FX.aggregate_usd(items, rate_source)
    if stale_ids:
        agg.basis = "PARTIAL"
        for aid in stale_ids:
            agg.unconverted.append({"account_id": aid, "native": None, "native_currency": "",
                                    "reason": "stale_read"})
    return agg, stale_ids


def build_summary(user, scope: Optional[str], *, fetch_balance: Callable, fetch_positions: Callable,
                  rate_source: FX.FxRateSource, stage: str = STAGE_ALL):
    """Assemble the portfolio summary for ``scope``. Returns ``(payload, error_status)``."""
    from trading.trading_state import resolve_trading_state
    accounts, err = resolve_scope(user, scope)
    if err is not None:
        return None, err

    open_rows, per_open = open_positions(accounts, fetch_positions=fetch_positions, rate_source=rate_source)

    bal_items, eq_items = [], []
    acct_payloads = []
    trading_count = 0
    observed_nonusd_ids = set()   # accounts whose OBSERVED (snapshot) currency is not USD — drives the realized
    #                               path's currency exclusion so metrics.basis agrees with balance_usd.basis (H1).
    for a in accounts:
        snap = None
        try:
            snap = fetch_balance(a)
        except Exception:  # noqa: BLE001
            logger.warning("portfolio: balance fetch raised for account %s", a.id)
            snap = None
        cur = FX.normalise_currency((snap or {}).get("currency") or getattr(a, "account_currency", None))
        if cur != FX.USD:
            observed_nonusd_ids.add(a.id)
        balance = (snap or {}).get("balance")
        equity = (snap or {}).get("equity")
        stale = snap is None
        try:
            ts = resolve_trading_state(a)
            state = getattr(ts, "state", None) or (ts.get("state") if isinstance(ts, dict) else None)
            state_label = getattr(ts, "label", None) or (ts.get("label") if isinstance(ts, dict) else None)
        except Exception:  # noqa: BLE001
            state, state_label = "PREPARING", "Preparing"
        if state == "TRADING":
            trading_count += 1
        op = per_open.get(a.id, {})
        acct_payloads.append({
            "account_id": a.id, "broker": a.broker_name, "account_masked": mask_account(a.account_number),
            "currency": cur, "is_active": a.is_active,
            "trading_state": {"state": state, "label": state_label},
            "balance_native": balance, "equity_native": equity,
            "net_pnl": account_net_pnl(a, stage=stage, observed_usd=(cur == FX.USD)),
            "open_count": op.get("open_count", 0), "open_pl_usd": op.get("open_pl_usd"),
            "stale": stale or op.get("stale", False),
        })
        bal_items.append({"account_id": a.id, "amount": balance, "currency": cur})
        eq_items.append({"account_id": a.id, "amount": equity, "currency": cur})

    bal_agg = FX.aggregate_usd(bal_items, rate_source)
    eq_agg = FX.aggregate_usd(eq_items, rate_source)
    # Floating P/L: aggregate NATIVE items and carry through PARTIAL/STALE (M1), not a re-sum of per-account USD.
    open_pl_agg, open_stale = portfolio_open_pl(per_open, rate_source)
    metrics = portfolio_metrics(accounts, stage=stage, observed_nonusd_ids=observed_nonusd_ids)
    daily_val, daily_basis = daily_realized_pnl(accounts, stage=stage, observed_nonusd_ids=observed_nonusd_ids)

    payload = {
        "reporting_currency": FX.USD,
        "scope": (scope or "ALL"),
        "generated_at": timezone.now().isoformat(),
        "account_count": len(accounts),
        "trading_count": trading_count,
        "stale_accounts": open_stale,
        "accounts": acct_payloads,
        "aggregate": {
            "balance_usd": bal_agg.as_dict(),
            "equity_usd": eq_agg.as_dict(),
            "open_pl_usd": open_pl_agg.as_dict(),
            "daily_realized_pnl_usd": daily_val,            # None when non-USD/mixed; basis states it (H1)
            "daily_realized_pnl_basis": daily_basis,
            "metrics": metrics,
        },
    }
    return payload, None
