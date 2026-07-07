"""
Delivery + ingestion (Phase 7A).

This is the ADR-009 boundary crossing: GuvFX *delivers* a Signal Intelligence
Envelope and WIMS *consumes* it by creating a ConsumptionContract via its
existing service. The dependency direction is GuvFX (intelligence) -> WIMS;
WIMS never imports intelligence.

Audit reuses the existing WIMS audit capability (``wims.services``) — no new
audit framework, no ``IntelligenceAuditRecord``. The four verifiable lifecycle
events are recorded here:

    SIGNAL_RECEIVED -> ENVELOPE_CREATED -> ENVELOPE_DELIVERED -> ENVELOPE_CONSUMED
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from django.db import transaction

from wims.models import AuditEvent, ConsumptionContract
from wims.services import create_contract, record_audit_ref

from .envelope import SignalIntelligenceEnvelope, TradeResultIntelligenceEnvelope
from .producer import SignalIntelligenceProducer
from .trade_result_producer import TradeResultProducer

_ENVELOPE_TYPE = "SignalIntelligenceEnvelope"
_SIGNAL_TYPE = "WayondSignal"
_TR_ENVELOPE_TYPE = "TradeResultIntelligenceEnvelope"
_TRADE_TYPE = "ClosedTrade"


def _dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_dt(value):
    from django.utils.dateparse import parse_datetime
    if not value:
        return None
    return parse_datetime(value) if isinstance(value, str) else value


@transaction.atomic
def ingest_wayond_signal(signal: Mapping, actor=None):
    """Full producer + delivery path for one Wayond signal.

    Returns ``(envelope, contract)``. The whole lifecycle is recorded to the
    existing WIMS audit log and the consumption (ConsumptionContract) is created
    through the unchanged WIMS service. Atomic: audit and consumption commit
    together or not at all.
    """
    # Produce first (pure transform), so every lifecycle audit row can carry the
    # envelope's intelligence_id for correlation.
    envelope = SignalIntelligenceProducer().produce(signal)

    # 1 — signal received (GuvFX has the Wayond signal in hand)
    record_audit_ref(
        actor, AuditEvent.Event.SIGNAL_RECEIVED,
        object_type=_SIGNAL_TYPE, object_id=0,
        intelligence_id=envelope.intelligence_id,
        signal_id=envelope.structured_payload.signal_id,
        source=envelope.source,
    )

    # 2 — envelope created (immutable intelligence artefact)
    record_audit_ref(
        actor, AuditEvent.Event.ENVELOPE_CREATED,
        object_type=_ENVELOPE_TYPE, object_id=0,
        intelligence_id=envelope.intelligence_id, version=envelope.version,
        intelligence_type=envelope.intelligence_type,
    )

    # 3 + 4 — deliver and consume
    contract = deliver(envelope, actor=actor)
    return envelope, contract


@transaction.atomic
def deliver(envelope: SignalIntelligenceEnvelope, actor=None) -> ConsumptionContract:
    """Deliver an envelope to WIMS and consume it as a ConsumptionContract."""
    p = envelope.structured_payload

    # 3 — envelope delivered (handed across the GuvFX -> WIMS boundary)
    record_audit_ref(
        actor, AuditEvent.Event.ENVELOPE_DELIVERED,
        object_type=_ENVELOPE_TYPE, object_id=0,
        intelligence_id=envelope.intelligence_id, signal_id=p.signal_id,
    )

    # WIMS consumes via the existing, unchanged service (emits CONTRACT_CREATED).
    contract = create_contract(
        actor=actor,
        source_type=ConsumptionContract.SourceType.WAYOND,
        source_reference=f"intelligence:{envelope.intelligence_id}",
        signal_type=ConsumptionContract.SignalType.ENTRY,
        symbol=p.market,
        direction=p.direction,
        entry_price=_dec(p.entry),
        stop_loss=_dec(p.stop_loss),
        take_profit=_dec(p.take_profit),
        confidence=_dec(p.confidence),
        raw_signal=json.dumps(envelope.to_dict()),
    )

    # 4 — envelope consumed (now a first-class persisted WIMS object)
    record_audit_ref(
        actor, AuditEvent.Event.ENVELOPE_CONSUMED,
        object_type="ConsumptionContract", object_id=contract.pk,
        intelligence_id=envelope.intelligence_id,
    )
    return contract


# ---------------------------------------------------------------------------
# Phase 7B — Trade Result Intelligence (closed trade -> envelope -> WIMS)
# ---------------------------------------------------------------------------
@transaction.atomic
def ingest_trade_result(trade, actor=None):
    """Full producer + delivery path for one closed trade.

    ``trade`` is a ``trading.models.Trade`` instance (authoritative source) or an
    equivalent mapping. Returns ``(envelope, contract)``. Atomic.
    """
    envelope = TradeResultProducer().produce(trade)

    # 1 — trade detected (a closed trade was found in GuvFX trade history)
    record_audit_ref(
        actor, AuditEvent.Event.TRADE_DETECTED,
        object_type=_TRADE_TYPE, object_id=0,
        intelligence_id=envelope.intelligence_id,
        trade_id=envelope.structured_payload.trade_id,
        source=envelope.source,
    )
    # 2 — envelope created (immutable intelligence artefact)
    record_audit_ref(
        actor, AuditEvent.Event.ENVELOPE_CREATED,
        object_type=_TR_ENVELOPE_TYPE, object_id=0,
        intelligence_id=envelope.intelligence_id, version=envelope.version,
        intelligence_type=envelope.intelligence_type,
    )
    # 3 + 4 — deliver to WIMS ONLY for a winning trade. LOSS/BREAKEVEN are recorded
    # internally (the envelope + the TRADE_DETECTED / ENVELOPE_CREATED audits above) but
    # NEVER become a WIMS ConsumptionContract / public story (LOSS-PATH-WIMS-REROUTE).
    if not is_winning_trade(trade):
        return envelope, None
    contract = deliver_trade_result(envelope, actor=actor)
    return envelope, contract


@transaction.atomic
def deliver_trade_result(envelope: TradeResultIntelligenceEnvelope,
                         actor=None, media=None) -> ConsumptionContract:
    """Deliver a trade-result envelope to WIMS and consume it as a contract.

    WIN-ONLY: a trade-result ConsumptionContract is a public artefact. This refuses a
    non-winning envelope (net pnl <= 0) even on a direct call, so losers/breakeven can never
    become WIMS content (LOSS-PATH-WIMS-REROUTE). Callers guard upstream via ``is_winning_trade``.
    """
    p = envelope.structured_payload

    if _dec(p.pnl) <= 0:
        raise ValueError(
            "deliver_trade_result refuses a non-winning trade (pnl <= 0); losers and "
            "breakeven are internal-only and never become WIMS content."
        )

    # 3 — envelope delivered (handed across the GuvFX -> WIMS boundary)
    record_audit_ref(
        actor, AuditEvent.Event.ENVELOPE_DELIVERED,
        object_type=_TR_ENVELOPE_TYPE, object_id=0,
        intelligence_id=envelope.intelligence_id, trade_id=p.trade_id,
    )

    # WIMS consumes via the existing WP-3 TRADE_RESULT path (unchanged service).
    contract = create_contract(
        actor=actor,
        source_type=ConsumptionContract.SourceType.TRADE_RESULT,
        source_reference=f"intelligence:{envelope.intelligence_id}",
        symbol=p.market,
        direction=p.direction,
        # entry/exit price are not part of the trade-result envelope payload
        # (it carries pnl/pips/outcome); the full envelope is kept in raw_signal.
        result_type=p.outcome,
        profit_loss=_dec(p.pnl),
        pips=_dec(p.pips),
        close_time=_parse_dt(p.close_time),
        commentary=p.summary,
        tags=["trade-result", p.outcome.lower()] if p.outcome else ["trade-result"],
        raw_signal=json.dumps(envelope.to_dict()),
        media=media,
    )

    # 4 — envelope consumed
    record_audit_ref(
        actor, AuditEvent.Event.ENVELOPE_CONSUMED,
        object_type="ConsumptionContract", object_id=contract.pk,
        intelligence_id=envelope.intelligence_id,
    )
    return contract


# ---------------------------------------------------------------------------
# Wayond content: Telegram signal -> WIMS content (NO execution)
# ---------------------------------------------------------------------------
def ingest_wayond_telegram_signal(parsed, actor=None, timestamp=""):
    """Content-only: a parsed Wayond Telegram SIGNAL -> WIMS ConsumptionContract.

    Reuses the unchanged Phase 7A signal path (``ingest_wayond_signal``). This
    feeds *educational content* only and never touches execution. ``parsed`` is a
    ``telegram_source.ParsedSignal`` (must be a tradeable SIGNAL shape).
    """
    from .telegram_source import to_producer_signal  # local import: avoid cycle
    signal = to_producer_signal(parsed, timestamp=timestamp)
    return ingest_wayond_signal(signal, actor=actor)


# ---------------------------------------------------------------------------
# Winning trade -> results card -> WIMS packet (WIN-ONLY; losers never enter)
# ---------------------------------------------------------------------------
def _net_pnl(trade) -> Decimal:
    def g(k):
        return trade.get(k, 0) if isinstance(trade, Mapping) else getattr(trade, k, 0)
    return (
        Decimal(str(g("profit") or 0))
        + Decimal(str(g("commission") or 0))
        + Decimal(str(g("swap") or 0))
    )


def is_winning_trade(trade) -> bool:
    """True only when net pnl is strictly positive (zero/breakeven is not a win)."""
    return _net_pnl(trade) > 0


def _g(t, k):
    return t.get(k) if isinstance(t, Mapping) else getattr(t, k, None)


def _aggregate_trade(trades, total_net):
    """Synthesise a single aggregate trade dict from a partial-close sequence."""
    if len(trades) == 1:
        return trades[0]
    first, last = trades[0], trades[-1]
    return {
        "ticket": str(_g(first, "ticket") or ""),
        "symbol": _g(first, "symbol"),
        "side": _g(first, "side"),
        "open_time": _g(first, "open_time"),
        "close_time": _g(last, "close_time"),
        "open_price": _g(first, "open_price"),
        "close_price": _g(last, "close_price"),
        "profit": str(total_net),
        "commission": "0",
        "swap": "0",
    }


@transaction.atomic
def ingest_winning_trade(trade, actor=None, account_label="GuvFX", currency="$"):
    """WIN-ONLY: a closed winning trade (or partial-close sequence) -> WIMS packet.

    ``trade`` is a single ``trading.models.Trade``-like object/mapping, or a list
    of them (partial closes of one order sequence). Rejects (``ValueError``) when
    total net pnl is <= 0, so losers AND breakeven/zero never enter the publish
    pipeline. Attaches a mobile trade-result card (PNG + internal SVG) and a
    social caption as content-side ``media`` and rides WP-3 -> Context -> Content
    -> human Review -> Publish. Returns ``(envelope, contract)``.
    """
    from .canonical import build_canonical_trade_result  # local import: avoid cycles
    from .renderers import WIMSRenderer

    trades = list(trade) if isinstance(trade, (list, tuple)) else [trade]
    total_net = sum((_net_pnl(t) for t in trades), Decimal("0"))
    if total_net <= 0:
        raise ValueError(
            "Not a net-winning trade (total pnl <= 0); losers and breakeven are not published."
        )

    envelope = TradeResultProducer().produce(_aggregate_trade(trades, total_net))

    record_audit_ref(
        actor, AuditEvent.Event.TRADE_DETECTED,
        object_type=_TRADE_TYPE, object_id=0,
        intelligence_id=envelope.intelligence_id,
        trade_id=envelope.structured_payload.trade_id, source=envelope.source,
    )
    record_audit_ref(
        actor, AuditEvent.Event.ENVELOPE_CREATED,
        object_type=_TR_ENVELOPE_TYPE, object_id=0,
        intelligence_id=envelope.intelligence_id, version=envelope.version,
        intelligence_type=envelope.intelligence_type,
    )

    # The result card + caption are produced from the ONE canonical trade result via the WIMS
    # renderer — the same shared results_card/caption factories, no duplicated formatting.
    canonical = build_canonical_trade_result(
        trades, account_label=account_label, currency=currency, with_media=True,
    )
    media = WIMSRenderer().render(canonical).media

    contract = deliver_trade_result(envelope, actor=actor, media=media)
    return envelope, contract
