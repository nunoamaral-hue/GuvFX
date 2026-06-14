"""
Signal Intelligence Envelope (Phase 7A).

The envelope is the GuvFX-side intelligence artefact (ADR-009: *GuvFX creates
intelligence*). It is **immutable after creation** (frozen dataclasses) and
carries its own ``version`` in metadata — no registry, no persistence model.

Structure (per the Phase 7A packet):

    Header:  intelligence_id, intelligence_type=SIGNAL, version="1.0", source,
             timestamp, confidence, summary, structured_payload
    Payload: signal_id, market, direction, entry, stop_loss, take_profit,
             timestamp, confidence, summary
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

INTELLIGENCE_TYPE_SIGNAL = "SIGNAL"
ENVELOPE_VERSION = "1.0"


@dataclass(frozen=True)
class SignalPayload:
    """Structured signal payload (immutable)."""

    signal_id: str
    market: str
    direction: str
    entry: str
    stop_loss: str
    take_profit: str
    timestamp: str
    confidence: str
    summary: str


@dataclass(frozen=True)
class SignalIntelligenceEnvelope:
    """Immutable intelligence envelope wrapping a single Wayond signal.

    Frozen: attempting to mutate any field raises ``dataclasses.FrozenInstanceError``.
    """

    intelligence_id: str
    source: str
    timestamp: str
    confidence: str
    summary: str
    structured_payload: SignalPayload
    intelligence_type: str = INTELLIGENCE_TYPE_SIGNAL
    version: str = ENVELOPE_VERSION

    def to_dict(self) -> dict:
        """Serialise the full envelope (header + structured payload)."""
        return asdict(self)
