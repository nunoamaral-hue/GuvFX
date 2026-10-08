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
