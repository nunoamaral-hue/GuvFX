"""TELEGRAM-TRANSPORT-FOUNDATION — the message contract.

Input: a ``NotificationCandidate``. Output: a ``TelegramMessageEnvelope`` (an immutable
dataclass). The rendered wording is a DRAFT — this packet defines the CONTRACT (the fields),
not the final copy. Building an envelope is READ-ONLY: it never mutates a trade / outcome /
candidate and never transmits anything.

The envelope is now a thin Telegram-specific projection of the ONE ``CanonicalTradeResult``
(GFX-PKT-CANONICAL-TRADE-RESULT): ``build_telegram_envelope`` builds the canonical object and
renders it with the ``TelegramRenderer`` — no message-formatting logic lives here any more, so
Telegram / WIMS / future social channels all format from the same source.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

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


def resolve_signal_linkage(correlation_id: str) -> dict:
    """Resolve the plan's signal/parser/execution linkage (read-only, blanks where absent).

    This lives on the EXECUTION side (it owns ``SignalExecutionPlan``) and hands intelligence a
    plain dict, so intelligence never imports execution (ADR-009). Keys match
    ``intelligence.canonical.LINKAGE_KEYS``.
    """
    if not correlation_id:
        return {}
    plan = (
        SignalExecutionPlan.objects.filter(correlation_id=correlation_id)
        .select_related("approval")
        .first()
    )
    if plan is None:
        return {}
    leg = plan.legs.order_by("leg_index").first()
    approval = getattr(plan, "approval", None)
    provider = getattr(approval, "provider", None) if approval is not None else None
    parser = getattr(provider, "parser_profile", None) if provider is not None else None
    exec_job = getattr(leg, "execution_job", None) if leg is not None else None
    exec_mode = ""
    payload = getattr(exec_job, "payload", None)
    if isinstance(payload, dict):
        exec_mode = str(payload.get("execution_mode", "") or "")
    source = getattr(plan, "source", "") or ""
    return {
        "reference_entry": str(getattr(plan, "entry", "") or ""),
        "stop_loss": str(getattr(plan, "stop_loss", "") or ""),
        "take_profit": str(getattr(leg, "take_profit", "") or ""),
        "source": source,
        "signal_id": str(getattr(approval, "message_id", "") or getattr(plan, "message_id", "") or ""),
        "provider": (getattr(provider, "slug", "") or source),
        "parser_profile": getattr(parser, "slug", "") or "",
        "parser_confidence": getattr(parser, "certification_level", "") or "",
        "execution_mode": exec_mode,
    }


def build_telegram_envelope(candidate) -> TelegramMessageEnvelope:
    """Build the (draft-rendered) Telegram envelope from a NotificationCandidate (read-only).

    Sources every field from the canonical trade result + the Telegram renderer — behaviour is
    identical to the previous bespoke assembly, now single-sourced.
    """
    from intelligence.canonical import build_canonical_trade_result
    from intelligence.renderers import TelegramRenderer

    trade = candidate.outcome_record.trade
    result = build_canonical_trade_result(
        trade,
        correlation_id=candidate.correlation_id or "",
        signal_source=candidate.signal_source or "",
        linkage=resolve_signal_linkage(candidate.correlation_id or ""),
    )
    content = TelegramRenderer().render(result)
    return TelegramMessageEnvelope(
        title=content.title,
        summary=result.summary,
        strategy=result.strategy,
        symbol=result.symbol,
        direction=result.direction,
        reference_entry=result.reference_entry,
        actual_fill=result.actual_fill,
        stop_loss=result.stop_loss,
        take_profit=result.take_profit,
        profit=result.net_pnl,
        pips=result.pips,
        execution_timestamp=result.execution_timestamp,
        correlation_id=result.correlation_id,
        rendered_message=content.text,
    )
