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
from .erasure_lock import acquire_owner_erasure_lock
from .evidence import EvidenceStore
from .flags import broker_cross_inbox_dedup_enabled
from .mail_source import MailMessage
from .models import BrokerEvent, BrokerMessageObservation, ConnectedMailbox, Provenance
from .parsers import select_parser
from .resolver import resolve_account, resolve_owner_identity, resolve_transaction_account

logger = logging.getLogger("guvfx.broker_intelligence.ingestion")


def _acquire_delivery_dedup_lock(connected_mailbox_id, provider_message_id: str) -> None:
    """Per ``(connected_mailbox, provider_message_id)`` Postgres TRANSACTION advisory lock, so the per-delivery
    decide-and-create is atomic even under concurrent re-fetch of the SAME message from the SAME inbox: a second
    fetch blocks here until the first commits its event, then observes the delivery row and reuses it. Must be called
    INSIDE ``transaction.atomic()`` (released on commit/rollback). No-op on a non-Postgres backend (prod is Postgres)."""
    from django.db import connection
    if connection.vendor != "postgresql":
        return
    import hashlib
    digest = hashlib.blake2b(f"bi-delivery-dedup:{connected_mailbox_id}:{provider_message_id}".encode("utf-8"),
                             digest_size=8).digest()
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

    ``connected_mailbox`` + per-delivery de-dup (§D, corrected per the PD review of #511): the event is always
    OWNER-scoped (keyed on ``owner_key`` below), so byte-identical bytes reaching two owners never cross-attribute.
    When ``broker_cross_inbox_dedup_enabled()`` is ON, a genuine RE-FETCH of the SAME message from the SAME inbox —
    identified by the TRUSTED ``(connected_mailbox, provider_message_id)`` — reuses the existing event rather than
    creating a second. The SAME logical email arriving through TWO different inboxes is TWO preserved deliveries (never
    suppressed here); they collapse to ONE withdrawal only at the correlation layer. While OFF this is byte-identical
    to today and the ``BrokerMessageObservation`` table is never touched (unmigrated-safe). The RFC822 Message-ID is
    NEVER a suppression key — it is non-authoritative metadata only."""
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
    alias, identity, resolved_owner = resolve_owner_identity(message.to_addresses, owner_user=owner_user)

    # The OWNER a BrokerEvent is attributed to and keyed on. Prefer the TRUSTED mailbox owner (non-null on every live
    # mailbox_ingest delivery) over the recipient-resolved owner, which is fail-closed NULL on the common
    # unverified/PENDING/ambiguous path — keying idempotency on a NULL owner would collapse to "WHERE owner IS NULL"
    # and could hand one owner another owner's event. The transaction trading-account below stays scoped to
    # resolved_owner (verified attribution); owner (who received it) and trading_account (whose account it concerns)
    # are deliberately distinct.
    owner_key = owner_user if owner_user is not None else resolved_owner

    # P0 (ADR-0054) — serialise the ENTIRE owner-scoped write against governed erasure. Acquire the per-owner
    # erasure advisory lock and re-validate the mailbox BEFORE storing any evidence or creating any owner-scoped
    # row, so a refused stale ingestion leaves NO orphan blob. All writes below run inside this one locked
    # transaction. (A None owner — legacy/non-mailbox callers — takes no lock and keeps the prior behaviour.)
    owner_lock_id = getattr(owner_key, "id", None)
    with transaction.atomic():
        acquire_owner_erasure_lock(owner_lock_id)
        if owner_lock_id is not None and getattr(connected_mailbox, "id", None) is not None:
            fresh = ConnectedMailbox.objects.filter(pk=connected_mailbox.id).first()
            if (fresh is None or fresh.status == ConnectedMailbox.Status.REVOKED
                    or fresh.user_id != owner_lock_id
                    or fresh.generation != getattr(connected_mailbox, "generation", -1)):
                logger.info("broker_intelligence ingest: mailbox removed/disconnected/regenerated for this owner "
                            "under the erasure lock — stale write refused (no evidence stored)")
                return None

        # 2) store raw bytes write-once (allowlisted broker mail, or any synthetic fixture; quarantine even
        #    unparseable broker mail — never drop raw broker evidence)
        blob, _created = store.put(message.raw_bytes, content_type="message/rfc822",
                                   source="EMAIL", received_at=message.received_at)

        # 3) idempotency: one (owner, blob) -> at most one event. OWNER-scoped so two DIFFERENT owners who receive
        #    byte-identical bytes (ONE shared content-addressed blob) each keep their OWN event (no cross-owner leak,
        #    no dropped delivery); a re-ingest by the SAME owner returns the SAME event.
        existing = BrokerEvent.objects.filter(evidence=blob, owner=owner_key).first()
        if existing is not None:
            return existing

        # R3b SENDER-AUTHENTICITY GATE: for REAL broker mail the From header is NOT trusted on its own — require a
        # passing Authentication-Results verdict (DMARC pass, or SPF+DKIM) before creating ANY event. A spoofed
        # broker-domain email (DMARC fail / no verdict) is retained as quarantined evidence but yields NO event.
        if provenance == Provenance.REAL and getattr(message, "auth_verdict", None) != "pass":
            logger.info("broker_intelligence ingest: unauthenticated broker sender (evidence %s retained, no event)",
                        blob.sha256[:12])
            return None

        def _parse_and_create():
            """Steps 4-9: select the parser, parse, resolve the transaction account, and create ONE append-only
            BrokerEvent — or return None (unparseable / declined / raising parser), evidence retained. Runs inside
            the owner-locked transaction established above. BrokerEvent.create kwargs add no new column — the
            delivery metadata lives in a SEPARATE table — so this write stays unmigrated-safe apart from the
            owner-scoped uniqueness relaxation (migration 0014)."""
            # 4) deterministic parser selection; no match -> unparseable -> retain evidence, emit NO event
            parser = select_parser(subject=message.subject, body=message.body, from_address=message.from_address)
            if parser is None:
                logger.info("broker_intelligence ingest: unparseable message (evidence %s retained, no event)",
                            blob.sha256[:12])
                return None
            # 5) parse (a RAISING parser is treated as 'declined'; evidence retained, no event, never crash ingest)
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
            # recipient email alone). No unambiguous single match ⇒ trading_account stays None (UNRESOLVED).
            if alias is not None:
                trading_account = resolve_account(alias)
            else:
                trading_account = resolve_transaction_account(
                    resolved_owner, parsed.broker, [parsed.source_account, parsed.destination_account],
                    identity=identity)
            # Create in its OWN savepoint so a concurrent-ingest IntegrityError (the uniq_brokerevent_evidence_owner
            # backstop) is isolated and recovered idempotently (one (owner, blob) -> one event).
            try:
                with transaction.atomic():
                    return BrokerEvent.objects.create(
                        alias=alias, trading_account=trading_account, owner=owner_key, broker=parsed.broker,
                        source=BrokerEvent._meta.get_field("source").default, event_type=parsed.event_type,
                        transaction_category=parsed.transaction_category,
                        occurred_at=parsed.occurred_at, received_at=message.received_at,
                        broker_reference_id=parsed.broker_reference_id or "", amount=parsed.amount,
                        currency=parsed.currency or "",
                        source_account=parsed.source_account or "",
                        destination_account=parsed.destination_account or "",
                        fee=parsed.fee, fee_currency=parsed.fee_currency or "",
                        evidence=blob, evidence_hash=blob.sha256,
                        parser_name=getattr(parser, "name", ""), parser_version=getattr(parser, "version", ""),
                        confidence=parsed.confidence,
                        correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED, provenance=provenance)
            except IntegrityError:
                # Lost the race -> an event already exists FOR THIS OWNER (uniq_brokerevent_evidence_owner backstop).
                # Re-query scoped to the SAME owner_key used on create, so a concurrent different-owner insert is
                # NEVER reused as this owner's event (that would re-open the cross-owner leak / drop a delivery).
                dup = BrokerEvent.objects.filter(evidence=blob, owner=owner_key).first()
                if dup is not None:
                    return dup
                raise

        # 3b) PER-DELIVERY DE-DUP (§D, DARK — corrected per the PD review of #511). A genuine RE-FETCH of the SAME
        #     message from the SAME inbox, identified by the TRUSTED mailbox-native (connected_mailbox,
        #     provider_message_id), must reuse the existing event, not create a second. The SAME logical email
        #     arriving through TWO different inboxes is TWO different connected_mailboxes = TWO preserved
        #     owner-scoped deliveries (collapsed only at correlation) — NEVER suppressed here, and NEVER on the
        #     sender-controlled RFC822 Message-ID (a non-authoritative hint). A per-(mailbox, provider_message_id)
        #     advisory lock serialises decide-and-create so two concurrent re-fetches can never both create an
        #     event. The delivery row is recorded ONLY AFTER a successful event (never a tombstone). OFF (or no
        #     provider_message_id, or no mailbox) ⇒ skipped; the table is never touched (unmigrated-safe).
        pmid = (getattr(message, "provider_message_id", "") or "").strip() if broker_cross_inbox_dedup_enabled() else ""
        if pmid and connected_mailbox is not None:
            mid = (getattr(message, "rfc822_message_id", "") or "").strip()   # non-authoritative correlation hint
            provider = getattr(connected_mailbox, "provider", "") or ""
            with transaction.atomic():
                _acquire_delivery_dedup_lock(connected_mailbox.id, pmid)
                prior = BrokerMessageObservation.objects.filter(
                    connected_mailbox=connected_mailbox, provider_message_id=pmid).first()
                if prior is not None and prior.event_id is not None:
                    BrokerMessageObservation.objects.filter(pk=prior.pk).update(
                        duplicate_count=F("duplicate_count") + 1, updated_at=timezone.now())
                    logger.info("broker_intelligence ingest: same-inbox re-delivery reused (provider message-id seen)")
                    return prior.event   # genuine re-fetch of an ALREADY-ingested delivery; NEVER a second event
                event = _parse_and_create()
                if event is not None:
                    # Record/refresh the delivery only now that a real event exists (no tombstone).
                    BrokerMessageObservation.objects.update_or_create(
                        connected_mailbox=connected_mailbox, provider_message_id=pmid,
                        defaults={"event": event, "owner": owner_key, "provider": provider,
                                  "rfc822_message_id": mid, "evidence": blob})
                return event
        return _parse_and_create()
