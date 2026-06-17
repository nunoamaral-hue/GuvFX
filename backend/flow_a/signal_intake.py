"""
Flow A — Wayond signal intake path.

Reuses the existing, unchanged ``intelligence`` producer (Phase 7A) to validate
and wrap a Wayond signal into an immutable Signal Intelligence Envelope. Flow A
never invents a signal; it consumes one it is given (Wayond is the only source
for this phase). Dependency direction: ``flow_a`` -> ``intelligence`` (one-way).
"""

from __future__ import annotations

from collections.abc import Mapping

from intelligence.envelope import SignalIntelligenceEnvelope
from intelligence.producer import SignalIntelligenceProducer


def intake_wayond_signal(signal: Mapping) -> SignalIntelligenceEnvelope:
    """Validate + wrap a Wayond signal into an immutable envelope.

    Pure: no I/O, no persistence. Raises ``ValueError`` (from the reused
    producer) if the signal is missing required fields.
    """
    return SignalIntelligenceProducer().produce(signal)
