"""ADR-0088 — DB-aware GENERIC-detection service: gate, load patterns, run the pure detector, persist a redacted
review candidate and (for GENERIC_HIGH_CONFIDENCE, Tier 1) create the owner-scoped BrokerEvent.

Invoked ONLY from ``ingest_message`` at the ``select_parser → None`` hook (so it never touches a specific
parser's result), ONLY when generic detection is enabled for the owner (canary). Runs inside the caller's
owner-locked transaction. Fail-closed: a missing owner, an OFF gate, or any ambiguity yields no event.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from django.db import IntegrityError, transaction

from . import generic_detector as gdx
from .broker_senders import _domain_of, is_allowlisted_broker_sender
from .flags import broker_generic_detection_enabled_for
from .models import (BrokerEvent, BrokerFingerprint, GenericWithdrawalPattern, Provenance,
                     WithdrawalReviewCandidate)

logger = logging.getLogger("guvfx.broker_intelligence.generic_detection")

_TAG = re.compile(r"<[^>]+>")


def _html_to_text(html_body: str) -> str:
    """Cheap, dependency-free HTML→text projection for signal detection (NOT stored). The authoritative source
    stays the immutable raw bytes; this only feeds the deterministic phrase matcher."""
    if not html_body:
        return ""
    return _TAG.sub(" ", html_body)


def run_generic_detection(*, message, blob, owner, provenance=Provenance.SYNTHETIC) -> Optional[BrokerEvent]:
    """Returns a created GENERIC_HIGH_CONFIDENCE ``BrokerEvent`` (Tier 1), or None for review-candidate /
    quarantine / not-a-withdrawal / gate-OFF. Persists a redacted ``WithdrawalReviewCandidate`` for every credible
    or ambiguous outcome. NEVER raises into ingestion (a failure degrades to no-event)."""
    try:
        if owner is None or not broker_generic_detection_enabled_for(owner):
            return None   # DARK / not a canary owner / fail-closed on anon -> no table touched beyond this read

        from_domain = _domain_of(getattr(message, "from_address", ""))
        sender_allowlisted = is_allowlisted_broker_sender(getattr(message, "from_address", ""))
        auth_verdict = getattr(message, "auth_verdict", None)
        html_text = _html_to_text(getattr(message, "html_body", "") or "")
        patterns = list(GenericWithdrawalPattern.objects.filter(enabled=True))
        # ADR-0099: the ACTIVE+CERTIFIED broker fingerprints. The model manager returns rows; the detector applies its
        # OWN independent eligibility gate too (``_fp_eligible``), so a mis-scoped query can never unlock certified
        # authority. DKIM/envelope/reply-to alignment is not yet surfaced by MailMessage (only an aggregate
        # ``auth_verdict``) — a fingerprint that DECLARES those constraints therefore fails closed until the ingestion
        # layer populates them; a domain+indicator fingerprint still recognises on domain + ``auth_verdict == pass``.
        fingerprints = list(BrokerFingerprint.objects.filter(
            is_active=True, certified_capability=True,
            governance_status=BrokerFingerprint.GovernanceStatus.CERTIFIED))

        gd = gdx.detect(subject=getattr(message, "subject", "") or "", body=getattr(message, "body", "") or "",
                        from_domain=from_domain, auth_verdict=auth_verdict,
                        sender_allowlisted=sender_allowlisted, patterns=patterns, html_text=html_text,
                        fingerprints=fingerprints)

        if gd.disposition == gdx.NOT_WITHDRAWAL:
            return None   # not a withdrawal -> no candidate, no event (unparseable-as-today)

        event = None
        if gd.disposition == gdx.GENERIC_HIGH_CONFIDENCE and gd.parsed_event is not None:
            event = _create_generic_event(gd, message=message, blob=blob, owner=owner, provenance=provenance)

        _upsert_candidate(gd, owner=owner, blob=blob, message=message, from_domain=from_domain,
                          auth_verdict=auth_verdict, sender_allowlisted=sender_allowlisted, event=event)
        return event
    except Exception:  # noqa: BLE001 — generic detection must NEVER crash ingestion; a failure = no event
        logger.warning("broker_intelligence generic detection failed (no event, evidence retained)", exc_info=False)
        return None


def _create_generic_event(gd, *, message, blob, owner, provenance) -> Optional[BrokerEvent]:
    """Create ONE owner-scoped BrokerEvent from a GENERIC_HIGH_CONFIDENCE detection. The category comes from the
    detector: UNKNOWN for a legacy ADR-0088 pattern match (generic never asserts EXTERNAL_WITHDRAWAL without certified
    evidence), and the CERTIFIED EXTERNAL_WITHDRAWAL only when an active+certified ADR-0099 fingerprint matched AND the
    message carried positive external-money-out evidence (the detector already enforced this). trading_account stays
    NULL (generic has only a masked suffix, never a full account to resolve) — owner-attributed, account-UNRESOLVED.
    Idempotent per (owner, evidence)."""
    pe = gd.parsed_event
    # Fail-closed: only the detector's own TransactionCategory values are ever written here; anything unexpected
    # degrades to UNKNOWN so a surprise value can never mint a counted withdrawal.
    category = pe.transaction_category if pe.transaction_category in (
        gdx.CAT_EXTERNAL_WITHDRAWAL, gdx.CAT_UNKNOWN) else gdx.CAT_UNKNOWN
    try:
        with transaction.atomic():
            return BrokerEvent.objects.create(
                owner=owner, trading_account=None, broker=pe.broker,
                source=BrokerEvent._meta.get_field("source").default,
                event_type=pe.event_type, transaction_category=category,
                occurred_at=None, received_at=message.received_at,
                broker_reference_id=pe.broker_reference_id or "", amount=pe.amount, currency=pe.currency or "",
                source_account="", destination_account="", fee=None, fee_currency="",
                evidence=blob, evidence_hash=blob.sha256,
                parser_name="generic", parser_version=(gd.matched_pattern or ""),
                confidence=gd.confidence,
                correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED, provenance=provenance)
    except IntegrityError:
        return BrokerEvent.objects.filter(evidence=blob, owner=owner).first()


def _upsert_candidate(gd, *, owner, blob, message, from_domain, auth_verdict, sender_allowlisted, event):
    """Idempotent redacted candidate per (owner, evidence_sha256). Stores ONLY redacted structural signals — never a
    raw body/name/full account/email/URL/token."""
    corr = "none"
    if event is not None:
        corr = f"event:{event.id}"
    WithdrawalReviewCandidate.objects.update_or_create(
        owner=owner, evidence_sha256=blob.sha256,
        defaults=dict(
            evidence=blob, provider_message_id=(getattr(message, "provider_message_id", "") or "")[:190],
            tier=1 if sender_allowlisted else 2, disposition=gd.disposition,
            proposed_state=gd.proposed_state, authenticated_sender_domain=(from_domain or "")[:190],
            auth_verdict=((auth_verdict or "")[:8]), candidate_broker_name=(gd.candidate_broker_name or "")[:64],
            masked_account_suffix=(gd.masked_account_suffix or "")[:12], amount=gd.amount, currency=gd.currency or "",
            reason_codes=list(gd.reason_codes), confidence=gd.confidence, correlation_result=corr[:48],
            matched_pattern=(gd.matched_pattern or "")[:96], created_event=event,
            observed_at=getattr(message, "received_at", None)))
