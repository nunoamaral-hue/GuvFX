"""WP3 — ingestion pipeline: raw message -> stored evidence -> parse -> append-only BrokerEvent.

The single, auditable path from an inbound broker message to a ``BrokerEvent``. Order (all for ONE message):
  1. resolve the To:-alias -> (alias, account)  [read-only]
  2. store the RAW bytes write-once in the evidence store (ALWAYS — even if unparseable; raw evidence is immutable
     and quarantined, never dropped)
  3. idempotency: if a BrokerEvent already references that evidence blob, return it (re-ingest is a no-op)
  4. select a deterministic parser; if none matches -> UNPARSEABLE -> return None (evidence retained, NO fabricated
     event)
  5. parse; if it yields nothing -> None; else create ONE append-only BrokerEvent with the evidence pointer + hash +
     parser name/version + provenance.

ISOLATION: imports ONLY broker_intelligence + (read-only) its own models — NEVER execution/strategies/trading
mutation, no order, no credential. ``provenance`` defaults SYNTHETIC: a real-ingestion caller must pass REAL/SANITISED
explicitly, so a fixture can never be mistaken for a genuine broker observation.

DARK: no live caller in WP3 (the standalone worker + real MailSource are WP3b, gated by
``BROKER_INTELLIGENCE_INGEST_ENABLED``). WP3 exercises this only via unit tests with a FixtureMailSource.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.db import IntegrityError, transaction

from .evidence import EvidenceStore
from .mail_source import MailMessage
from .models import BrokerEvent, Provenance
from .parsers import select_parser
from .resolver import resolve

logger = logging.getLogger("guvfx.broker_intelligence.ingestion")


def ingest_message(message: MailMessage, *, store: Optional[EvidenceStore] = None,
                   provenance: str = Provenance.SYNTHETIC) -> Optional[BrokerEvent]:
    """Ingest ONE message. Returns the created (or pre-existing, on re-ingest) BrokerEvent, or None when the message
    is unparseable (evidence is still stored). Idempotent by evidence content-hash."""
    store = store or EvidenceStore()

    # 1) resolve recipient -> alias/account (read-only; unknown -> (None, None))
    alias, account = resolve(message.to_addresses)

    # 2) store raw bytes write-once (always — quarantine even unparseable mail; never drop raw evidence)
    blob, _created = store.put(message.raw_bytes, content_type="message/rfc822",
                               source="EMAIL", received_at=message.received_at)

    # 3) idempotency: one message (one blob) -> at most one event
    existing = BrokerEvent.objects.filter(evidence=blob).first()
    if existing is not None:
        return existing

    # 4) deterministic parser selection; no match -> unparseable -> retain evidence, emit NO event
    parser = select_parser(subject=message.subject, body=message.body, from_address=message.from_address)
    if parser is None:
        logger.info("broker_intelligence ingest: unparseable message (evidence %s retained, no event)",
                    blob.sha256[:12])
        return None

    # 5) parse -> create ONE append-only BrokerEvent (never fabricate absent fields). A parser that RAISES is
    # treated as 'declined' (symmetry with select_parser's can_parse sandbox): evidence is retained, no event,
    # never crash ingestion.
    try:
        parsed = parser.parse(subject=message.subject, body=message.body, from_address=message.from_address)
    except Exception:  # noqa: BLE001 — a raising parser must never crash ingestion (contract); treat as declined
        logger.warning("broker_intelligence ingest: parser %s raised (evidence %s retained, no event)",
                       getattr(parser, "name", "?"), blob.sha256[:12])
        return None
    if parsed is None:
        logger.info("broker_intelligence ingest: parser %s declined (evidence %s retained, no event)",
                    getattr(parser, "name", "?"), blob.sha256[:12])
        return None

    # Create in its OWN savepoint so a concurrent-ingest IntegrityError (the DB uniq_brokerevent_evidence backstop)
    # is isolated from any enclosing worker transaction and recovered idempotently (one blob -> one event).
    try:
        with transaction.atomic():
            return BrokerEvent.objects.create(
                alias=alias, trading_account=account, broker=parsed.broker,
                source=BrokerEvent._meta.get_field("source").default, event_type=parsed.event_type,
                transaction_category=parsed.transaction_category,
                occurred_at=parsed.occurred_at, received_at=message.received_at,
                broker_reference_id=parsed.broker_reference_id or "", amount=parsed.amount,
                currency=parsed.currency or "", evidence=blob, evidence_hash=blob.sha256,
                parser_name=getattr(parser, "name", ""), parser_version=getattr(parser, "version", ""),
                confidence=parsed.confidence,
                correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED, provenance=provenance)
    except IntegrityError:
        dup = BrokerEvent.objects.filter(evidence=blob).first()   # lost the race -> the event already exists
        if dup is not None:
            return dup
        raise
