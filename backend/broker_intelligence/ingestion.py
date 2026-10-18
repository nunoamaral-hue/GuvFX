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
from django.db.models import F
from django.utils import timezone

from .broker_senders import is_allowlisted_broker_sender
from .evidence import EvidenceStore
from .flags import broker_cross_inbox_dedup_enabled
from .mail_source import MailMessage
from .models import BrokerEvent, BrokerMessageObservation, Provenance
from .parsers import select_parser
from .resolver import resolve_account, resolve_owner_identity, resolve_transaction_account

logger = logging.getLogger("guvfx.broker_intelligence.ingestion")


def _acquire_message_dedup_lock(owner_id, message_id: str) -> None:
    """Per ``(owner, Message-ID)`` Postgres TRANSACTION advisory lock, so the cross-inbox de-dup decide-and-create is
    atomic even under concurrent ingestion of the same logical email: a second copy blocks here until the first
    commits its event, then observes it and suppresses. Must be called INSIDE ``transaction.atomic()`` (released on
    commit/rollback). No-op on a non-Postgres backend (prod is Postgres)."""
    from django.db import connection
    if connection.vendor != "postgresql":
        return
    import hashlib
    digest = hashlib.blake2b(f"bi-msgdedup:{owner_id}:{message_id}".encode("utf-8"), digest_size=8).digest()
    key = int.from_bytes(digest, "big", signed=True)   # stable signed 64-bit bigint advisory key
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def ingest_message(message: MailMessage, *, store: Optional[EvidenceStore] = None,
                   provenance: str = Provenance.SYNTHETIC, owner_user=None,
                   connected_mailbox=None) -> Optional[BrokerEvent]:
    """Ingest ONE message. Returns the created (or pre-existing, on re-ingest) BrokerEvent, or None when the message
    is unparseable (evidence is still stored). Idempotent by evidence content-hash.

    ``owner_user`` (SECURITY — cross-user attribution firewall): when a message comes from a known mailbox, pass that
    mailbox's owner so recipient-header resolution is scoped to that member. The live mail worker MUST pass it (a
    mailbox always has an owner) — attacker-controllable To/Delivered-To/X-Original-To headers then cannot attribute
    a message to a different member's account. Default None keeps the global lookup for non-mailbox/legacy callers.

    ``connected_mailbox`` + cross-inbox de-dup (§D): when ``broker_cross_inbox_dedup_enabled()`` is ON, the same
    logical broker email (same RFC822 Message-ID) observed through TWO connected inboxes is recognised as ONE logical
    observation and does NOT create a second event. While OFF this is byte-identical to today and the
    ``BrokerMessageObservation`` table is never touched (unmigrated-safe)."""
    store = store or EvidenceStore()

    # R1 RETENTION GATE (privacy — before ANY storage): for REAL mail (the live worker path over a member's PERSONAL
    # inbox), NEVER acquire or retain a message that is not from an allowlisted, verified broker sender. Unrelated
    # personal correspondence must never enter the immutable evidence store. Fail-closed: an empty/unset allowlist
    # stores NOTHING. SYNTHETIC/SANITISED fixtures (controlled test data, never live personal mail) are exempt.
    if provenance == Provenance.REAL and not is_allowlisted_broker_sender(message.from_address):
        logger.info("broker_intelligence ingest: non-broker sender skipped for REAL mail (no evidence stored)")
        return None

    # 1) resolve recipient -> (alias, identity, owner); owner-scoped (spoofed recipient headers can't cross-attribute
    #    to another member). This does NOT pick the transaction trading-account — that is resolved from the broker's
    #    stated source/destination accounts AFTER parsing (an email identifies the owner+broker, not the account).
    alias, _identity, resolved_owner = resolve_owner_identity(message.to_addresses, owner_user=owner_user)

    # 2) store raw bytes write-once (allowlisted broker mail, or any synthetic fixture; quarantine even unparseable
    #    broker mail — never drop raw broker evidence)
    blob, _created = store.put(message.raw_bytes, content_type="message/rfc822",
                               source="EMAIL", received_at=message.received_at)

    # 3) idempotency: one message (one blob) -> at most one event
    existing = BrokerEvent.objects.filter(evidence=blob).first()
    if existing is not None:
        return existing

    # R3b SENDER-AUTHENTICITY GATE: for REAL broker mail the From header is NOT trusted on its own — require a passing
    # Authentication-Results verdict (DMARC pass, or SPF+DKIM) before creating ANY event. A spoofed broker-domain
    # email (DMARC fail / no verdict) is retained as quarantined evidence but yields NO event. Fail-closed.
    if provenance == Provenance.REAL and getattr(message, "auth_verdict", None) != "pass":
        logger.info("broker_intelligence ingest: unauthenticated broker sender (evidence %s retained, no event)",
                    blob.sha256[:12])
        return None

    def _parse_and_create():
        """Steps 4-9: select the parser, parse, resolve the transaction account, and create ONE append-only
        BrokerEvent — or return None (unparseable / declined / raising parser), evidence retained. Extracted so the
        cross-inbox de-dup can run it under a per-message lock WITHOUT duplicating the pipeline. BrokerEvent.create
        kwargs are UNCHANGED (no new column) — the dedup metadata lives in a SEPARATE table — so this write stays
        byte-identical and unmigrated-safe."""
        # 4) deterministic parser selection; no match -> unparseable -> retain evidence, emit NO event
        parser = select_parser(subject=message.subject, body=message.body, from_address=message.from_address)
        if parser is None:
            logger.info("broker_intelligence ingest: unparseable message (evidence %s retained, no event)",
                        blob.sha256[:12])
            return None
        # 5) parse (a RAISING parser is treated as 'declined'; evidence retained, no event, never crash ingestion)
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
        # Resolve the TRANSACTION trading-account (distinct from the recipient owner). Alias -> its one account;
        # otherwise match the broker-STATED source/destination accounts to the OWNER's own accounts (never the
        # recipient email alone). No unambiguous single match ⇒ trading_account stays None (attribution UNRESOLVED).
        if alias is not None:
            trading_account = resolve_account(alias)
        else:
            trading_account = resolve_transaction_account(
                resolved_owner, parsed.broker, [parsed.source_account, parsed.destination_account])
        # Create in its OWN savepoint so a concurrent-ingest IntegrityError (the uniq_brokerevent_evidence backstop)
        # is isolated and recovered idempotently (one blob -> one event).
        try:
            with transaction.atomic():
                return BrokerEvent.objects.create(
                    alias=alias, trading_account=trading_account, owner=resolved_owner, broker=parsed.broker,
                    source=BrokerEvent._meta.get_field("source").default, event_type=parsed.event_type,
                    transaction_category=parsed.transaction_category,
                    occurred_at=parsed.occurred_at, received_at=message.received_at,
                    broker_reference_id=parsed.broker_reference_id or "", amount=parsed.amount,
                    currency=parsed.currency or "",
                    source_account=parsed.source_account or "", destination_account=parsed.destination_account or "",
                    fee=parsed.fee, fee_currency=parsed.fee_currency or "",
                    evidence=blob, evidence_hash=blob.sha256,
                    parser_name=getattr(parser, "name", ""), parser_version=getattr(parser, "version", ""),
                    confidence=parsed.confidence,
                    correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED, provenance=provenance)
        except IntegrityError:
            dup = BrokerEvent.objects.filter(evidence=blob).first()   # lost the race -> the event already exists
            if dup is not None:
                return dup
            raise

    # 3b) CROSS-INBOX DE-DUP (§D, DARK). The same logical email arriving via a SECOND connected inbox is a DIFFERENT
    #     raw blob (different sha256 — content-hashing can't collapse it), so it would otherwise create a second
    #     BrokerEvent/Withdrawal. Keyed on the MAILBOX OWNER (always non-null on the live worker path) — never the
    #     possibly-NULL resolved transaction owner, so there is no NULL-key race. A per-(owner, Message-ID) advisory
    #     lock serialises the decide-and-create, so even two concurrent copies can never both create an event. The
    #     observation is recorded ONLY AFTER a successful event (never a NULL-event tombstone), and suppression fires
    #     ONLY when a prior observation already carries an event — so a crash / parser-decline before the event is
    #     created never permanently suppresses a later genuine copy (the content-hash retry idempotency is preserved).
    #     Only AUTHENTICATED broker observations reach here, so a forged copy can never occupy the slot. OFF (or no
    #     Message-ID, or no owner) ⇒ skipped entirely; the table is never touched (unmigrated-safe).
    owner_key = owner_user if owner_user is not None else resolved_owner
    mid = (getattr(message, "rfc822_message_id", "") or "").strip() if broker_cross_inbox_dedup_enabled() else ""
    if mid and owner_key is not None:
        with transaction.atomic():
            _acquire_message_dedup_lock(owner_key.id, mid)
            prior = BrokerMessageObservation.objects.filter(owner=owner_key, rfc822_message_id=mid).first()
            if prior is not None and prior.event_id is not None:
                BrokerMessageObservation.objects.filter(pk=prior.pk).update(
                    duplicate_count=F("duplicate_count") + 1, updated_at=timezone.now())
                logger.info("broker_intelligence ingest: cross-inbox duplicate suppressed (message-id seen before)")
                return prior.event   # genuine duplicate of an ALREADY-ingested message; NEVER a second event
            event = _parse_and_create()
            if event is not None:
                # Record/refresh the observation only now that a real event exists (no tombstone). update_or_create
                # recovers a prior crash/decline row (event was NULL) by linking it to the event just created.
                BrokerMessageObservation.objects.update_or_create(
                    owner=owner_key, rfc822_message_id=mid,
                    defaults={"event": event, "connected_mailbox": connected_mailbox, "evidence": blob})
            return event
    return _parse_and_create()
