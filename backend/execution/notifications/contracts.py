"""TELEGRAM-TRANSPORT-FOUNDATION — the message contract.

Input: a ``NotificationCandidate``. Output: a ``TelegramMessageEnvelope`` (an immutable
dataclass). The rendered wording is a DRAFT — this packet defines the CONTRACT (the fields),
not the final copy. Building an envelope is READ-ONLY: it never mutates a trade / outcome /
candidate and never transmits anything.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from intelligence.trade_result_producer import TradeResultProducer

from execution.models import SignalExecutionPlan


@dataclass(frozen=True)
class TelegramMessageEnvelope:
    """The Telegram message contract (dry-run). Wording is intentionally NOT finalised."""

    title: str
    summary: str
    strategy: str
    symbol: str
    direction: str
    reference_entry: str   # the provider's advisory signal entry (reference only)
    actual_fill: str       # the real market fill price
    stop_loss: str
    take_profit: str
    profit: str
    pips: str
    execution_timestamp: str
    correlation_id: str
    rendered_message: str

    def as_dict(self) -> dict:
        return asdict(self)


def _plan_for(correlation_id):
    if not correlation_id:
        return None
    return SignalExecutionPlan.objects.filter(correlation_id=correlation_id).first()


def _iso(value):
    try:
        return value.isoformat() if value else ""
    except Exception:
        return ""


def build_telegram_envelope(candidate) -> TelegramMessageEnvelope:
    """Build the (draft-rendered) Telegram envelope from a NotificationCandidate (read-only)."""
    outcome = candidate.outcome_record
    trade = outcome.trade
    # pips + summary via the pure, canonical producer (raises only on an open trade, which
    # cannot reach here — candidates come from closed WIN outcomes).
    payload = TradeResultProducer().produce(trade).structured_payload

    plan = _plan_for(candidate.correlation_id)
    reference_entry = str(getattr(plan, "entry", "") or "")   # advisory, per the entry-price policy
    stop_loss = str(getattr(plan, "stop_loss", "") or "")
    take_profit = ""
    if plan is not None:
        leg = plan.legs.order_by("leg_index").first()
        take_profit = str(getattr(leg, "take_profit", "") or "")

    symbol = trade.symbol
    direction = trade.side
    actual_fill = str(trade.open_price)                        # the real market fill
    profit = str(outcome.net_pnl)
    pips = str(payload.pips)
    strategy = candidate.signal_source or (plan.source if plan else "")
    ts = _iso(trade.close_time)
    title = f"GuvFX — winning trade: {symbol} {direction}"      # DRAFT wording
    summary = payload.summary

    rendered = _render(
        title, summary, strategy, symbol, direction, reference_entry, actual_fill,
        stop_loss, take_profit, profit, pips, ts, candidate.correlation_id,
    )
    return TelegramMessageEnvelope(
        title=title, summary=summary, strategy=strategy, symbol=symbol, direction=direction,
        reference_entry=reference_entry, actual_fill=actual_fill, stop_loss=stop_loss,
        take_profit=take_profit, profit=profit, pips=pips, execution_timestamp=ts,
        correlation_id=candidate.correlation_id, rendered_message=rendered,
    )


def _render(title, summary, strategy, symbol, direction, ref_entry, actual_fill, sl, tp,
            profit, pips, ts, cid) -> str:
    # DRAFT template — wording intentionally NOT finalised (contract only). Shows BOTH the
    # provider reference entry and the actual fill (entry-price policy §6A).
    return "\n".join([
        title,
        summary,
        f"Strategy: {strategy or 'n/a'}",
        f"{symbol} {direction}",
        f"Signal entry (ref): {ref_entry or 'n/a'}  |  Filled: {actual_fill}",
        f"SL: {sl or 'n/a'}  |  TP: {tp or 'n/a'}",
        f"Profit: {profit}  ({pips} pips)",
        f"Closed: {ts or 'n/a'}",
        f"ref: {cid or 'n/a'}",
    ])
