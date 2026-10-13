"""WP6 — withdrawal metrics projection (read-only, owner-scoped, never-fabricated).

Aggregates the correlated ``Withdrawal`` records (populated by ``broker_intelligence.correlation``) into
member-facing statistics. Every ``Withdrawal`` is already EXTERNAL_WITHDRAWAL-only by construction (the correlation
engine projects only external withdrawals), so these figures can never include an internal transfer / deposit.

Truthfulness rules (Sponsor direction):
* **PENDING is NOT a failure.** A withdrawal that is REQUESTED / PROCESSING / PENDING (requested, completion not yet
  observed) is counted as *in flight* — never folded into the failed count. Failed = REJECTED / CANCELLED only.
* **Never fabricate.** A missing amount is OMITTED (not summed as 0); processing-duration stats are ``None`` when no
  completed withdrawal has BOTH a requested and completed anchor (never a fabricated 0). Currencies are never mixed —
  amounts are summed per-currency.

Pure + DB-read only. No execution/credential/financial authority; it only reads the withdrawal projection.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from decimal import Decimal

from .models import Withdrawal

# In-flight (requested, completion not yet observed) — explicitly NOT failed.
_PENDING = (Withdrawal.Status.REQUESTED, Withdrawal.Status.PROCESSING, Withdrawal.Status.PENDING)
# Terminal non-completion — distinct from pending.
_FAILED = (Withdrawal.Status.REJECTED, Withdrawal.Status.CANCELLED)

# Member-facing lifecycle STATE (coarse, truthful): in-flight is NEVER folded into failed; UNRESOLVED is its own
# visible state (never hidden, never counted as failed or completed).
_STATE = {s: "pending" for s in _PENDING}
_STATE[Withdrawal.Status.COMPLETED] = "completed"
_STATE.update({s: "failed" for s in _FAILED})
_STATE[Withdrawal.Status.UNRESOLVED] = "unresolved"


def _masked_account(acct) -> str:
    """Owner-private last-4 rendering of an account number (never the full number)."""
    num = str(getattr(acct, "account_number", "") or "")
    return ("••••" + num[-4:]) if len(num) >= 4 else (num or "—")


def withdrawal_history(accounts, *, broker: str = "", account_id=None) -> dict:
    """Per-withdrawal history for ``accounts`` (owner-scoped; the caller passes ONLY the member's own accounts),
    optionally filtered by broker name and/or a single trading-account id. Read-only; never fabricates.

    Returns the withdrawal rows (newest first) PLUS the distinct brokers and accounts present in the member's
    withdrawal history, so the UI can offer broker/account selectors without a second query. Each row carries an
    evidence-backed lifecycle ``state`` (pending / completed / failed / unresolved — PENDING is in-flight, NOT a
    failure), amount+currency AS OBSERVED (never summed, never fabricated), the processing ``duration_seconds``
    (None when both anchors are not present) and whether the broker reference id resolved the correlation."""
    ids = [getattr(a, "id", a) for a in accounts]
    base = (Withdrawal.objects.filter(trading_account_id__in=ids)
            .select_related("trading_account").order_by("-requested_at", "-id"))

    # Selector lists from the member's FULL withdrawal set — NEVER narrowed by the active filters — so the broker/
    # account dropdowns stay complete and stable while filtering.
    brokers, accts = set(), {}
    for w in base:
        acct = w.trading_account
        bname = getattr(acct, "broker_name", "") or ""
        brokers.add(bname)
        accts.setdefault(acct.id, {"id": acct.id, "broker": bname, "account_masked": _masked_account(acct)})

    # Rows = the filtered view.
    qs = base.filter(trading_account_id=account_id) if account_id not in (None, "") else base
    rows = []
    for w in qs:
        acct = w.trading_account
        bname = getattr(acct, "broker_name", "") or ""
        if broker and bname != broker:
            continue
        rows.append({
            "id": w.id,
            "broker": bname,
            "trading_account_id": acct.id,
            "account_masked": _masked_account(acct),
            "state": _STATE.get(w.status, "unresolved"),
            "status": w.status,
            "amount": str(w.amount) if w.amount is not None else None,   # never fabricated / never summed here
            "currency": w.currency or "",
            "requested_at": w.requested_at.isoformat() if w.requested_at else None,
            "completed_at": w.completed_at.isoformat() if w.completed_at else None,
            "duration_seconds": w.duration_seconds(),    # None unless BOTH anchors observed (never a fabricated 0)
            "broker_reference_id": w.broker_reference_id or "",
            "reference_correlated": w.correlation_method == Withdrawal.CorrelationMethod.REFERENCE_ID,
        })
    return {
        "withdrawals": rows,
        "brokers": sorted(b for b in brokers if b),
        "accounts": sorted(accts.values(), key=lambda a: (a["broker"], a["id"])),
    }


def withdrawal_metrics(accounts) -> dict:
    """Withdrawal statistics for ``accounts`` (an iterable of TradingAccount or ids). Read-only; never fabricates."""
    ids = [getattr(a, "id", a) for a in accounts]
    rows = list(Withdrawal.objects.filter(trading_account_id__in=ids))

    by_status = dict(Counter(w.status for w in rows))
    pending = [w for w in rows if w.status in _PENDING]
    completed = [w for w in rows if w.status == Withdrawal.Status.COMPLETED]
    failed = [w for w in rows if w.status in _FAILED]
    unresolved = [w for w in rows if w.status == Withdrawal.Status.UNRESOLVED]

    # Completed amounts per currency — never mix currencies; a completed withdrawal with an unknown amount is
    # counted in completed_count but omitted from the summed total (surfaced via completed_amount_known_count).
    amt: dict = defaultdict(lambda: Decimal("0"))
    completed_amount_known = 0
    for w in completed:
        if w.amount is not None and w.currency:
            amt[w.currency] += w.amount
            completed_amount_known += 1

    # Processing duration — ONLY completed withdrawals that carry BOTH anchors (requested_at + completed_at).
    # None when there are none (never a fabricated zero).
    durs = [d for d in (w.duration_seconds() for w in completed) if d is not None]
    duration = None
    if durs:
        duration = {
            "count": len(durs),
            "avg_seconds": round(statistics.mean(durs)),
            "median_seconds": int(statistics.median(durs)),
            "min_seconds": min(durs),
            "max_seconds": max(durs),
        }

    return {
        "total": len(rows),
        "by_status": by_status,
        "pending_count": len(pending),            # in flight (requested/processing/pending) — NOT failed
        "completed_count": len(completed),
        "failed_count": len(failed),              # rejected/cancelled — distinct from pending
        "unresolved_count": len(unresolved),
        "completed_amount_by_currency": {k: str(v) for k, v in sorted(amt.items())},
        "completed_amount_known_count": completed_amount_known,
        "processing_duration": duration,          # None when no completed-with-both-anchors (never fabricated)
    }
