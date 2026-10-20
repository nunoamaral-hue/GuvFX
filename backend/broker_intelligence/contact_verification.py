"""broker_intelligence.contact_verification — the reference, fail-closed writer for ``TradingAccountBrokerContact``
verification (MULTI_ACCOUNT §A/C, corrected per the PD review of #511).

Mailbox control is EVIDENCE, not proof of ownership. A binding reaches VERIFIED only on composed ownership proof:
the connected mailbox is owned by the trading account's owner, AND the broker-email identity belongs to that owner,
AND the identity's broker name is consistent with the account's broker. Ownership is ALWAYS the ACCOUNT owner, never
the shared email address (the same address may be a registered contact for two unrelated members). Fail-closed:
anything missing/mismatched sets UNRESOLVED and writes a REJECT audit row — never a silent pass.

This is the intended VERIFIED writer; the SAME ownership invariant is INDEPENDENTLY enforced at the DB layer
(migration 0015 ``bi_tabc_verified_consistency``) so a bulk/raw/``QuerySet.update`` write cannot forge a cross-user
VERIFIED behind this service. Broker-name consistency uses the existing token match; canonical-catalogue-slug
matching is a documented deferred TODO (no name->slug resolver exists yet) and is NOT claimed as enforced.

Pure + DB-only: no HTTP, no credential, no execution/strategy/MT5 authority, never a financial action. DARK — no live
caller yet; the member binding/verify HTTP surface lands with the account-level broker-email UI (C4).
"""
from __future__ import annotations

from typing import Optional, Tuple

from django.db import transaction
from django.utils import timezone

from .flags import broker_routing_evidence_required
from .identity_verification import _broker_matches
from .models import ConnectedMailbox as Mailbox
from .models import Provenance
from .models import TradingAccountBrokerContact as Contact
from .models import TradingAccountBrokerContactAudit as Audit
from .resolver import canonical_address


def check_consistency(contact: Contact) -> Tuple[bool, str]:
    """Return ``(ok, reason_code)`` for whether ``contact`` may be VERIFIED. Pure read; fail-closed. ``reason_code``
    is a short, secret-free code (never an address/credential)."""
    acct = contact.trading_account
    acct_owner = getattr(acct, "user_id", None)
    if acct_owner is None:
        return False, "account_has_no_owner"
    mbx = contact.connected_mailbox
    if mbx is None:
        return False, "no_connected_mailbox"
    if mbx.user_id != acct_owner:                                  # mailbox owner must be the account owner
        return False, "mailbox_owner_mismatch"
    if mbx.status != Mailbox.Status.CONNECTED:                     # revocable: a revoked/error mailbox is not proof
        return False, "mailbox_not_connected"
    idn = contact.broker_email_identity
    idn_owner = idn.user_id if idn.user_id else (idn.trading_account.user_id if idn.trading_account_id else None)
    if idn_owner != acct_owner:                                    # identity must belong to the account owner
        return False, "identity_owner_mismatch"
    if not _broker_matches(getattr(idn, "broker_name", "") or "", getattr(acct, "broker_name", "") or ""):
        return False, "broker_mismatch"
    # §2 ROUTING EVIDENCE (DARK): a same-owner CONNECTED mailbox is not, by itself, proof that THIS broker-registered
    # address's mail actually routes to it. When armed, require independently-supportable routing evidence.
    if broker_routing_evidence_required() and not _routing_proven(contact)[0]:
        return False, "routing_unproven"
    return True, "ok"


def _routing_proven(contact: Contact) -> Tuple[bool, str]:
    """Independently-supportable ROUTING evidence that the bound mailbox actually receives THIS ``(email, broker)``
    identity's mail — not merely that it is a same-owner mailbox (§2). Two supported kinds, fail-closed:
      * ``direct_address`` — the bound CONNECTED mailbox's gmail-canonical address IS the identity's broker-registered
        address (the member connected the very inbox the broker sends to); needs no ingestion history.
      * ``observed_delivery`` — an AUTHENTICATED (``provenance=REAL``) broker delivery was observed THROUGH the bound
        mailbox for its owner (``BrokerMessageObservation`` → a real ``BrokerEvent``), i.e. the broker's mail
        demonstrably arrived in that inbox (covers an approved forwarding/alias where the connected mailbox differs
        from the registered address). This evidence exists only once the delivery plane is populated (cross-inbox
        dedup armed) — see the arming note below.
    Returns ``(ok, kind)``; neither ⇒ ``(False, "unproven")``."""
    mbx = contact.connected_mailbox
    idn = contact.broker_email_identity
    if mbx is None:
        return False, "none"
    if (idn.email and mbx.primary_email
            and canonical_address(mbx.primary_email) == canonical_address(idn.email)):
        return True, "direct_address"
    from .models import BrokerMessageObservation
    if BrokerMessageObservation.objects.filter(
            connected_mailbox=mbx, owner_id=mbx.user_id, event__provenance=Provenance.REAL).exists():
        return True, "observed_delivery"
    return False, "unproven"


def verify_contact(contact: Contact, *, actor=None, proofs: Optional[dict] = None) -> Contact:
    """Attempt to VERIFY ``contact``. On composed ownership + broker proof: ``status=VERIFIED`` (+ ``verified_at``) and
    a VERIFY audit row. Otherwise: ``status=UNRESOLVED`` and a REJECT audit row carrying the secret-free reason code.
    Never raises on a failed proof (fail-closed to UNRESOLVED, audited); the DB trigger is the hard backstop. Returns
    the mutated ``contact``."""
    before = contact.status
    ok, reason = check_consistency(contact)
    # Atomic: the status change and its audit row commit together or not at all — a status transition must never
    # persist without the audit record that justifies it (and vice versa).
    with transaction.atomic():
        if ok:
            contact.status = Contact.Status.VERIFIED
            contact.verified_at = timezone.now()
            contact.save(update_fields=["status", "verified_at", "updated_at"])
            action, outcome, note = Audit.Action.VERIFY, "OK", "ownership + broker consistency proven"
        else:
            contact.status = Contact.Status.UNRESOLVED
            contact.verified_at = None
            contact.save(update_fields=["status", "verified_at", "updated_at"])
            action, outcome, note = Audit.Action.REJECT, reason, "verification refused (fail-closed)"
        Audit.objects.create(
            contact=contact, action=action, subject_user=actor,
            trading_account=contact.trading_account, broker_email_identity=contact.broker_email_identity,
            connected_mailbox=contact.connected_mailbox, outcome=outcome, reason=note,
            status_before=before, status_after=contact.status, proofs=proofs or {})
    return contact
