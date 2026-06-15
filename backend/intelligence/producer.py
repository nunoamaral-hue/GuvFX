"""
Signal Intelligence Producer (Phase 7A).

Packages an already-existing Wayond signal into an immutable Signal Intelligence
Envelope. This phase is packaging + delivery, **not signal generation** — the
producer never invents a signal, it only wraps one it is given.

Wayond is the only authorised source for this phase.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from .envelope import SignalIntelligenceEnvelope, SignalPayload

WAYOND_SOURCE = "WAYOND"

# Fields a Wayond signal must provide to be packaged.
REQUIRED_SIGNAL_FIELDS = (
    "signal_id", "market", "direction", "entry", "stop_loss", "take_profit",
    "timestamp", "confidence", "summary",
)


class SignalIntelligenceProducer:
    """Builds immutable envelopes from Wayond signals (Wayond only)."""

    source = WAYOND_SOURCE

    def produce(self, signal: Mapping) -> SignalIntelligenceEnvelope:
        """Wrap a Wayond ``signal`` mapping in an immutable envelope.

        Pure: no I/O, no persistence, no audit. The caller (delivery) persists
        and audits. Raises ``ValueError`` if required fields are missing.
        """
        missing = [f for f in REQUIRED_SIGNAL_FIELDS if signal.get(f) in (None, "")]
        if missing:
            raise ValueError(f"Wayond signal missing required field(s): {missing}")

        payload = SignalPayload(
            signal_id=str(signal["signal_id"]),
            market=str(signal["market"]),
            direction=str(signal["direction"]),
            entry=str(signal["entry"]),
            stop_loss=str(signal["stop_loss"]),
            take_profit=str(signal["take_profit"]),
            timestamp=str(signal["timestamp"]),
            confidence=str(signal["confidence"]),
            summary=str(signal["summary"]),
        )
        return SignalIntelligenceEnvelope(
            intelligence_id=uuid.uuid4().hex,
            source=self.source,
            timestamp=payload.timestamp,
            confidence=payload.confidence,
            summary=payload.summary,
            structured_payload=payload,
        )
