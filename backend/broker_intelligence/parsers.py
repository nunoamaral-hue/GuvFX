"""WP3 — deterministic broker-email parser contract + registry (NO LLM in the critical evidence path).

A ``BrokerEmailParser`` turns ONE raw broker message into AT MOST ONE ``ParsedBrokerEvent``, deterministically and
auditably (anchored regex / structured-field extraction, pinned by ``name``+``version``). Unknown/ambiguous input
yields ``None`` — never a fabricated event (data/research rules). Concrete per-broker parsers (e.g. TradersWay) arrive
in WP4 and register here; WP3 ships only the contract + registry so the ingestion pipeline is testable DARK with a
fixture parser. An LLM may later *suggest* a template offline, but must never decide an event here.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ParsedBrokerEvent:
    """The deterministic extraction from one message. Absent fields stay None/'' — NEVER coerced to 0/placeholder
    (never fabricated). ``event_type`` is the LIFECYCLE (open ``BROKER_EVENT_TYPES`` registry); ``transaction_category``
    is the orthogonal kind-of-movement (``TransactionCategory``) and DEFAULTS to ``UNKNOWN`` — a parser must set
    ``EXTERNAL_WITHDRAWAL`` only on positive evidence money left the broker, never from the word 'withdrawal' alone.
    Parsers MUST NOT place a confirmation URL / authorization token in any field here (sensitive; evidence-only)."""
    event_type: str
    transaction_category: str = "UNKNOWN"
    broker: str = ""
    amount: Optional[Decimal] = None
    currency: str = ""
    occurred_at: Optional[datetime.datetime] = None
    broker_reference_id: str = ""
    confidence: Optional[Decimal] = None


@runtime_checkable
class BrokerEmailParser(Protocol):
    """Contract: ``can_parse`` is a cheap pin to a broker+template; ``parse`` does the extraction (or returns None
    when it cannot confidently extract a tradeable-irrelevant withdrawal event). Both are pure — no I/O, no DB."""
    name: str
    version: str

    def can_parse(self, *, subject: str, body: str, from_address: str) -> bool: ...

    def parse(self, *, subject: str, body: str, from_address: str) -> Optional[ParsedBrokerEvent]: ...


_REGISTRY: List[BrokerEmailParser] = []


def register_parser(parser: BrokerEmailParser) -> None:
    """Register a parser (idempotent by (name, version)). Called at import time by concrete WP4 parsers."""
    for existing in _REGISTRY:
        if getattr(existing, "name", None) == getattr(parser, "name", None) and \
           getattr(existing, "version", None) == getattr(parser, "version", None):
            return
    _REGISTRY.append(parser)


def clear_registry() -> None:
    """Test-only: empty the registry so a test's fixture parsers don't leak across tests."""
    _REGISTRY.clear()


def get_parsers() -> tuple:
    return tuple(_REGISTRY)


def select_parser(*, subject: str, body: str, from_address: str) -> Optional[BrokerEmailParser]:
    """The FIRST registered parser whose ``can_parse`` matches, or None (→ unparseable → quarantine, no event).
    Deterministic in registration order; a parser must claim only its own broker+template."""
    for p in _REGISTRY:
        try:
            if p.can_parse(subject=subject, body=body, from_address=from_address):
                return p
        except Exception:  # noqa: BLE001 — a misbehaving parser must never crash ingestion; it just doesn't match
            continue
    return None
