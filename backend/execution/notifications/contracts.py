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

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

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


def _leg_net(trade) -> str:
    """Net profit for one trade = profit + commission + swap, formatted; '' if unknown."""
    try:
        total = (Decimal(str(getattr(trade, "profit", 0) or 0))
                 + Decimal(str(getattr(trade, "commission", 0) or 0))
                 + Decimal(str(getattr(trade, "swap", 0) or 0)))
        return str(total)
    except (InvalidOperation, TypeError):
        return ""


def _resolve_strategy_display_name(plan) -> str:
    """Best-effort human strategy name for the plan's account (e.g. 'Wayond Auto Demo').

    Resolves the account's active AUTO_DEMO assignment SOURCE-AWARE: when more than one auto-copy
    strategy shares an account (e.g. Wayond + Wayond WIM on the demo account), the card must be
    labelled with the strategy bound to THIS plan's source. Falls back to any AUTO_DEMO assignment
    (single-assignment/unbound account) and then a title-cased source. Read-only and defensive — a
    lookup failure never blocks a notification."""
    try:
        from strategies.models import StrategyAssignment

        base = StrategyAssignment.objects.filter(
            account=plan.account, is_active=True,
            execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
        ).select_related("strategy")
        src = getattr(plan, "source", "") or ""
        a = base.filter(signal_source=src).order_by("-id").first() if src else None
        if a is None:  # back-compat: unbound / single-assignment account
            a = base.order_by("-id").first()
        if a is not None and a.strategy_id:
            return a.strategy.name
    except Exception:  # pragma: no cover - defensive; never block a notification on this
        pass
    return (getattr(plan, "source", "") or "").replace("_", " ").title()


def resolve_leg_evidence(correlation_id: str, current_trade=None) -> dict:
    """Gather a plan's per-leg evidence (closed + open + pending) for progressive rendering.

    READ-ONLY. Lives on the execution side (owns SignalExecutionPlan + Trade) and hands intelligence
    a plain dict (ADR-009). For each leg of the plan it reports the leg's TP target and — via the
    leg's order comment ``WAY{plan}L{leg}`` — whether its trade is CLOSED (hit TP: exit + profit),
    OPEN (filled, running) or PENDING (not yet filled). ``current_trade`` (the trade whose close
    triggered this notification) sets the progress label ("TP{n}"). Returns {} when there is no
    resolvable plan (e.g. a non-Wayond / strategy-path trade) so the renderer degrades gracefully.
    """
    if not correlation_id:
        return {}
    plan = (
        SignalExecutionPlan.objects.filter(correlation_id=correlation_id)
        .select_related("account")
        .first()
    )
    if plan is None:
        return {}
    from trading.models import Trade

    leg_dicts = []
    take_profits = []
    closed = 0
    for leg in plan.legs.order_by("leg_index"):
        tp = str(leg.take_profit or "")
        take_profits.append(tp)
        comment = f"WAY{plan.id}L{leg.leg_index}"
        # Prefer the AUTHORITATIVE position row (one with a real close_price) over any
        # stale deal-keyed row that shares this comment (a historical mis-ingested
        # duplicate has close_price=None) — else the latest open row. Without this, a
        # price-less duplicate can shadow the real fill and zero out the card's profit.
        _legq = Trade.objects.filter(account=plan.account, comment=comment)
        trade = (
            _legq.filter(close_price__isnull=False).order_by("-close_time", "-open_time").first()
            or _legq.order_by("-open_time").first()
        )
        vol = f"{leg.lot_size:.2f}" if leg.lot_size is not None else ""
        # A leg counts CLOSED *for this card* only if it closed at/before the card's own
        # trade closed. Live, later legs are not yet closed so this is a no-op; when a card
        # is rendered retroactively (e.g. a recovered result) after later legs also closed,
        # it still shows the honest progressive state at this leg's moment.
        _cur_close = getattr(current_trade, "close_time", None)
        if (trade is not None and trade.close_time is not None
                and (_cur_close is None or trade.close_time <= _cur_close)):
            closed += 1
            status, exit_, profit = "CLOSED", str(trade.close_price or ""), _leg_net(trade)
            entry = str(trade.open_price or plan.entry or "")
            direction = str(trade.side or plan.direction)
            close_time = trade.close_time.isoformat()
            if trade.volume is not None:
                vol = f"{trade.volume:.2f}"
        elif trade is not None:
            status, exit_, profit, close_time = "OPEN", "", "", ""
            entry = str(trade.open_price or plan.entry or "")
            direction = str(trade.side or plan.direction)
            if trade.volume is not None:
                vol = f"{trade.volume:.2f}"
        else:
            status, exit_, profit, close_time = "PENDING", "", "", ""
            entry = str(plan.entry or "")
            direction = str(plan.direction)
        leg_dicts.append({
            "index": leg.leg_index, "tp_label": f"TP{leg.leg_index}", "direction": direction,
            "volume": vol, "entry": entry, "target": tp, "exit": exit_, "profit": profit,
            "status": status, "close_time": close_time,
        })

    total = len(leg_dicts)
    current_idx = None
    cc = str(getattr(current_trade, "comment", "") or "")
    m = re.search(r"L(\d+)$", cc)
    if m:
        current_idx = int(m.group(1))
    label = f"TP{current_idx}" if current_idx else (f"TP{closed}" if closed else "")
    return {
        "legs": leg_dicts,
        "take_profits": take_profits,
        "strategy_display_name": _resolve_strategy_display_name(plan),
        "progress": {"closed": closed, "total": total, "label": label,
                     "final": bool(total and closed >= total)},
    }


def _canonical_for(candidate):
    """Build the CanonicalTradeResult for a candidate (read-only; execution resolves the linkage +
    per-TP leg evidence and hands intelligence plain dicts — ADR-009)."""
    from intelligence.canonical import build_canonical_trade_result

    trade = candidate.outcome_record.trade
    cid = candidate.correlation_id or ""
    return build_canonical_trade_result(
        trade,
        correlation_id=cid,
        signal_source=candidate.signal_source or "",
        linkage=resolve_signal_linkage(cid),
        leg_evidence=resolve_leg_evidence(cid, trade),
    )


def build_telegram_envelope(candidate) -> TelegramMessageEnvelope:
    """Build the (draft-rendered) Telegram TEXT envelope from a NotificationCandidate (read-only).

    Sources every field from the canonical trade result + the Telegram renderer. This is the text
    projection (audit + text fallback); the primary stakeholder output is the card image."""
    from intelligence.renderers import TelegramRenderer

    result = _canonical_for(candidate)
    content = TelegramRenderer().render(result)
    return _envelope_from(content, result)


def build_stakeholder_card(candidate):
    """Render the stakeholder result CARD (PNG bytes) + short caption for a WIN candidate.

    Read-only — builds the same canonical the text envelope uses, then the visual card + caption.
    Pulls Pillow (only invoked from the real transport's card send). Returns ``(png_bytes, caption)``."""
    import base64

    from intelligence.caption import build_short_caption
    from intelligence.results_card import render_result_card

    result = _canonical_for(candidate)
    card = render_result_card(result)
    return base64.b64decode(card["png_base64"]), build_short_caption(result)


def _envelope_from(content, result) -> TelegramMessageEnvelope:
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
