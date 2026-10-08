"""WP3 — mailbox abstraction (MailSource) for the isolated ingestion service.

The ingestion worker reads messages through this abstraction, so the pilot mailbox (a GuvFX-controlled catch-all) and
the eventual ``*@accounts.guvfx.com`` MX are a config swap, not a code change. WP3 ships the interface + a
``FixtureMailSource`` (synthetic messages, for tests/demo); the real IMAP source + its credential (via
``core.credentials.resolve_secret``) land in WP3b with the standalone worker. Nothing here touches execution/strategy/
MT5 credentials or trading state.
"""
from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, List, Tuple


@dataclass(frozen=True)
class MailMessage:
    """One inbound message, normalised. ``raw_bytes`` is the immutable payload stored in the evidence store;
    everything else is parsed-out convenience (the raw bytes remain the source of truth)."""
    raw_bytes: bytes
    to_addresses: Tuple[str, ...]
    from_address: str
    subject: str
    body: str
    received_at: datetime.datetime
    provider_message_id: str = ""   # the mailbox's own id (for ack/dedup at the source), NOT a GuvFX id


class MailSource(ABC):
    """Fetch inbound messages and acknowledge handled ones. Implementations own ONLY mailbox access — never a broker
    login, strategy, or order path."""

    @abstractmethod
    def fetch(self) -> Iterable[MailMessage]:
        """Yield not-yet-acknowledged messages. May be empty. Must be side-effect-free w.r.t. trading state."""

    def ack(self, message: MailMessage) -> None:  # noqa: B027 — optional hook; default no-op
        """Mark a message handled at the source (e.g. move/flag). Default no-op; the durable record is the evidence
        store + BrokerEvent, so re-fetching an un-acked message is safe (ingestion is idempotent by content hash)."""
        return None


@dataclass
class FixtureMailSource(MailSource):
    """A synthetic, in-memory source for tests/demo. Holds a fixed list of MailMessages (which the caller labels
    SYNTHETIC at ingest). ``ack`` removes from the pending list so a second ``fetch`` is empty."""
    messages: List[MailMessage] = field(default_factory=list)

    def fetch(self) -> Iterable[MailMessage]:
        return list(self.messages)

    def ack(self, message: MailMessage) -> None:
        self.messages = [m for m in self.messages if m is not message]
