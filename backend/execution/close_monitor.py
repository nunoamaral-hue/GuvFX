"""
AUTO-SHADOW-CLOSE-MONITOR — classify CLOSED trades into idempotent outcome records.

Builds the post-trade foundation for the (future) profit-only notification + WIMS handoff.
It reads closed, not-yet-processed ``trading.Trade`` rows, classifies each with the pure
``intelligence.TradeResultProducer`` (which fail-closes on open/corrupt trades), and writes
one internal ``TradeOutcomeRecord`` per trade.

HARD BOUNDARY — this module creates INTERNAL records ONLY. It NEVER:
  * places or closes an order (no order_send / order_check / ExecutionJob create),
  * sends a Telegram notification,
  * publishes to WIMS (no ConsumptionContract, no deliver_trade_result),
  * mutates the trade or any execution/trading behaviour.

A WIN becomes an internal *delivery candidate* (``is_delivery_candidate=True``, still
``delivered=False``) for a future, separately-gated notification packet. LOSS/BREAKEVEN are
recorded internally and are never candidates. Open or corrupt/incomplete trades are skipped
(fail-closed) and left for a later run. Idempotent: a trade already recorded is never
reprocessed (the OneToOne on the record enforces it, even under a race).
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction

from intelligence.trade_result_producer import TradeResultProducer
from trading.models import Trade

from execution.models import SignalExecutionPlan, TradeOutcomeRecord

logger = logging.getLogger("guvfx.execution.close_monitor")

DEFAULT_LIMIT = 500


def _resolve_linkage(trade):
    """Best-effort signal linkage from the trade's correlation_id (blank/None where absent)."""
    cid = (getattr(trade, "correlation_id", "") or "").strip()
    if not cid:
        return "", "", None
    plan = SignalExecutionPlan.objects.filter(correlation_id=cid).first()
    if plan is None:
        return cid, "", None
    leg = plan.legs.filter(execution_job__isnull=False).order_by("leg_index").first()
    return cid, plan.source, (leg.execution_job if leg else None)


def process_closed_trades(*, limit: int = DEFAULT_LIMIT) -> dict:
    """Classify up to ``limit`` closed, not-yet-recorded trades. Returns a counts dict.

    Creates only internal ``TradeOutcomeRecord`` rows; never an order/Telegram/WIMS.
    """
    producer = TradeResultProducer()
    counts = {"processed": 0, "win": 0, "loss": 0, "breakeven": 0, "skipped": 0}

    trades = (
        Trade.objects.filter(close_time__isnull=False, outcome_record__isnull=True)
        .order_by("close_time", "id")[:limit]
    )
    for trade in trades:
        try:
            payload = producer.produce(trade).structured_payload  # ValueError if open/corrupt
            outcome = payload.outcome
            net_pnl = Decimal(str(payload.pnl))
        except Exception as exc:  # open / incomplete / corrupt → fail-closed, leave it
            logger.info("close_monitor: skipped trade %s (%s)", getattr(trade, "id", "?"), exc)
            counts["skipped"] += 1
            continue

        cid, source, job = _resolve_linkage(trade)
        try:
            with transaction.atomic():
                TradeOutcomeRecord.objects.create(
                    trade=trade,
                    outcome=outcome,
                    net_pnl=net_pnl,
                    is_delivery_candidate=(outcome == TradeOutcomeRecord.Outcome.WIN),
                    correlation_id=cid,
                    signal_source=source,
                    execution_job=job,
                )
        except IntegrityError:
            # A concurrent run created the record first — idempotent, never duplicated.
            continue

        counts["processed"] += 1
        counts[str(outcome).lower()] = counts.get(str(outcome).lower(), 0) + 1

    return counts
