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

from .envelope import SignalIntelligenceEnvelope
from .producer import SignalIntelligenceProducer

_ENVELOPE_TYPE = "SignalIntelligenceEnvelope"
_SIGNAL_TYPE = "WayondSignal"


def _dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


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
