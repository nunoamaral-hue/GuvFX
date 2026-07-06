"""
PROFIT-NOTIFICATION-FOUNDATION — the Outcome Router (internal routing abstraction).

Sits immediately after ``TradeOutcomeRecord`` (the close-monitor's classification). For each
not-yet-routed outcome it makes the WIN / LOSS / BREAKEVEN routing decision:

    WIN (delivery candidate)  → create a PENDING ``NotificationCandidate`` (internal)
    LOSS / BREAKEVEN          → internal record only (NO candidate) → end

HARD BOUNDARY — this module is a pure internal router. It NEVER:
  * calls the Telegram API / sends any message,
  * calls WIMS / creates a ConsumptionContract / publishes anything,
  * places an order (no order_send / ExecutionJob),
  * mutates a trade or any execution/trading behaviour.

It only creates internal ``NotificationCandidate`` rows and marks each outcome ``routed``.
Idempotent: a routed outcome is never re-routed (``routed=False`` filter), and the candidate
OneToOne prevents a duplicate even under a race. Correlation is preserved onto the candidate.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from execution.models import NotificationCandidate, TradeOutcomeRecord

logger = logging.getLogger("guvfx.execution.outcome_router")

DEFAULT_LIMIT = 500


def route_outcomes(*, limit: int = DEFAULT_LIMIT) -> dict:
    """Route up to ``limit`` not-yet-routed outcomes. Returns a counts dict.

    WIN → a PENDING NotificationCandidate; LOSS/BREAKEVEN → internal only. Creates NO
    Telegram/WIMS/order — internal candidate rows only.
    """
    counts = {"routed": 0, "candidates": 0, "internal_only": 0}

    records = (
        TradeOutcomeRecord.objects.filter(routed=False)
        .order_by("created_at", "id")[:limit]
    )
    for rec in records:
        try:
            with transaction.atomic():
                is_win = (
                    rec.outcome == TradeOutcomeRecord.Outcome.WIN and rec.is_delivery_candidate
                )
                if is_win:
                    NotificationCandidate.objects.get_or_create(
                        outcome_record=rec,
                        defaults={
                            "correlation_id": rec.correlation_id,
                            "signal_source": rec.signal_source,
                            "net_pnl": rec.net_pnl,
                        },
                    )
                # Mark routed regardless of outcome (the decision has been made).
                updated = TradeOutcomeRecord.objects.filter(pk=rec.pk, routed=False).update(
                    routed=True
                )
        except IntegrityError:
            # A concurrent router won the candidate race — idempotent, never duplicated.
            continue

        if not updated:
            continue  # another run routed it first — don't double-count
        counts["routed"] += 1
        counts["candidates" if is_win else "internal_only"] += 1

    return counts
