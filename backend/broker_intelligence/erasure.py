"""Controlled, owner-scoped ERASURE for Withdrawal Intelligence (ADR-0051; GDPR/UK-GDPR Art 17).

This is the ONLY path permitted to remove a data subject's broker-email evidence, events and withdrawals. It is
DARK by default (``broker_intelligence_erasure_enabled``), staff/governance-only, and fails closed. Design:

- Ordinary immutability is NEVER weakened. ``EvidenceBlob``/``BrokerEvent`` stay undeletable everywhere — the DB
  BEFORE-DELETE triggers (migration 0017) raise UNLESS the per-session GUC ``guvfx.erasure_authorized`` is ``on``,
  and that GUC is set ONLY inside ``execute_owner_erasure``'s transaction (and reset immediately after).
- A deduplicated/shared evidence blob is physically removed ONLY when no lawful reference remains: the delete is
  attempted and a Django ``ProtectedError`` (another owner's event/observation still PROTECTs it) means RETAIN.
- Two-step: ``plan_owner_erasure`` is a pure dry-run (no mutation) that produces the object list + a confirmation
  token; ``execute_owner_erasure`` requires that token (TOCTOU guard) plus a verified request and runs atomically.
- Audit: counts/ids only, NEVER deleted message content.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.db import connection, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from . import flags
from .evidence import EvidenceStore
from . import credential_store
from .models import (
    BrokerEmailIdentity, BrokerEvent, BrokerMessageObservation, ConnectedMailbox, EvidenceBlob,
    ErasureHold, ErasureRequest, Withdrawal,
)

logger = logging.getLogger("guvfx.broker_intelligence.erasure")

_GUC = "guvfx.erasure_authorized"


class ErasureRefused(Exception):
    """Raised (fail-closed) when a governed erasure must not proceed. The message is sanitised (no content)."""


def _ids(qs) -> list[int]:
    return sorted(qs.values_list("id", flat=True))


def plan_owner_erasure(owner_id: int) -> dict:
    """PURE dry-run: enumerate exactly what an erasure of ``owner_id`` would delete/retain. NO mutation, no GUC.
    Classifies each candidate evidence blob as DELETE (referenced only by this owner) or RETAIN-SHARED (another
    owner/holder references it). Returns a plan dict incl. a deterministic confirmation ``token``."""
    wd = _ids(Withdrawal.objects.filter(owner_id=owner_id))
    ev = _ids(BrokerEvent.objects.filter(owner_id=owner_id))
    obs = _ids(BrokerMessageObservation.objects.filter(owner_id=owner_id))
    mbx = _ids(ConnectedMailbox.objects.filter(user_id=owner_id))
    idn = _ids(BrokerEmailIdentity.objects.filter(user_id=owner_id))
    cred_refs = list(ConnectedMailbox.objects.filter(user_id=owner_id)
                     .exclude(credential_ref="").values_list("credential_ref", flat=True))

    # Candidate blobs = blobs referenced by THIS owner's events. A blob is deletable only if NO reference remains
    # once this owner's events + observations are gone. NULL-safe: other = total - this_owner.
    candidate_blob_ids = sorted(set(
        BrokerEvent.objects.filter(owner_id=owner_id, evidence__isnull=False).values_list("evidence_id", flat=True)
    ))
    to_delete, to_retain = [], []
    for bid in candidate_blob_ids:
        total_ev = BrokerEvent.objects.filter(evidence_id=bid).count()
        mine_ev = BrokerEvent.objects.filter(evidence_id=bid, owner_id=owner_id).count()
        total_obs = BrokerMessageObservation.objects.filter(evidence_id=bid).count()
        mine_obs = BrokerMessageObservation.objects.filter(evidence_id=bid, owner_id=owner_id).count()
        other_refs = (total_ev - mine_ev) + (total_obs - mine_obs)
        (to_delete if other_refs == 0 else to_retain).append(bid)

    plan = {
        "subject_user_id": owner_id,
        "withdrawals": wd,
        "broker_events": ev,
        "observations": obs,
        "connected_mailboxes": mbx,
        "credential_refs_count": len(cred_refs),      # count only — the ref is an opaque pointer, not content
        "identities": idn,
        "evidence_blobs_delete": to_delete,            # referenced only by this owner -> physical deletion
        "evidence_blobs_retain_shared": to_retain,     # another owner/holder references it -> retained
    }
    plan["token"] = _plan_token(plan)
    return plan


def _plan_token(plan: dict) -> str:
    """Deterministic digest over the owner-scoped object set — the second-confirmation token + TOCTOU guard. If the
    DB state changes between plan and execute, the recomputed token differs and execute refuses."""
    basis = json.dumps({k: plan[k] for k in (
        "subject_user_id", "withdrawals", "broker_events", "observations", "connected_mailboxes",
        "identities", "evidence_blobs_delete", "evidence_blobs_retain_shared")}, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def assert_erasure_allowed(request: ErasureRequest) -> int:
    """Fail-closed gate. Returns the resolved subject owner id, or raises ErasureRefused. Checks: flag ON; request
    not already executed; data-subject identity verified; an owner id is present; no ACTIVE legal/security hold."""
    if not flags.broker_intelligence_erasure_enabled():
        raise ErasureRefused("controlled erasure is disabled (BROKER_INTELLIGENCE_ERASURE_ENABLED off)")
    if request.status == ErasureRequest.Status.EXECUTED:
        raise ErasureRefused("request already executed")
    if not request.identity_verified:
        raise ErasureRefused("data-subject identity not verified for this request")
    owner_id = request.subject_user_id_snapshot
    if not owner_id:
        raise ErasureRefused("request has no subject owner id")
    if ErasureHold.objects.filter(subject_user_id_snapshot=owner_id, active=True).exists():
        raise ErasureRefused("an active legal/security hold blocks erasure for this subject")
    return owner_id


def execute_owner_erasure(request: ErasureRequest, confirm_token: str) -> dict:
    """GOVERNED erasure of one owner's Withdrawal-Intelligence personal data. The mutation is atomic (all-or-nothing)
    + fail-closed + idempotent. Requires a confirmation token recomputed from the CURRENT state (TOCTOU guard).
    Ordinary immutability is untouched: the GUC that lets the DB triggers permit deletion is set only inside the
    mutation transaction and reset immediately. The FAILED-status record is saved OUTSIDE the mutation transaction
    so it survives the rollback."""
    owner_id = assert_erasure_allowed(request)

    # TOCTOU: recompute the plan from the LIVE state; the caller-supplied token must match it (so the operator is
    # confirming exactly what will be deleted now, not a stale plan).
    plan = plan_owner_erasure(owner_id)
    if not confirm_token or confirm_token != plan["token"]:
        raise ErasureRefused("confirmation token does not match the current plan (state changed or wrong token)")

    store = EvidenceStore()
    outcome = {"subject_user_id": owner_id, "withdrawals_deleted": 0, "broker_events_deleted": 0,
               "observations_deleted": 0, "connected_mailboxes_deleted": 0, "credentials_destroyed": 0,
               "identities_deleted": 0, "evidence_blobs_deleted": 0, "evidence_blobs_retained_shared": 0,
               "evidence_files_unlinked": 0}
    unlink_after_commit = []   # physical file removal is IRREVERSIBLE -> defer until the DB mutation has committed
    try:
        with transaction.atomic():
            # Authorise the DB immutability triggers for THIS transaction only, reset in the finally.
            with connection.cursor() as cur:
                cur.execute(f"SET LOCAL \"{_GUC}\" = 'on'")
            try:
                # 1) withdrawals (projection; also releases PROTECT refs to events)
                outcome["withdrawals_deleted"] = Withdrawal.objects.filter(owner_id=owner_id).delete()[0]
                # 2) observations (release PROTECT refs to blobs; SET_NULL event refs)
                outcome["observations_deleted"] = BrokerMessageObservation.objects.filter(owner_id=owner_id).delete()[0]
                # 3) events (now unreferenced by withdrawals/observations) — bulk delete fires the GUC-gated trigger
                outcome["broker_events_deleted"] = BrokerEvent.objects.filter(owner_id=owner_id).delete()[0]
                # 4) candidate blobs: delete the ROW ONLY when no lawful reference remains (ProtectedError => retain).
                #    Per-blob savepoint so one retained (shared) blob never aborts the whole erasure. The physical
                #    FILE is NOT touched here — unlinked only AFTER commit (irreversible), so a rollback keeps DB+file
                #    consistent.
                for bid in plan["evidence_blobs_delete"] + plan["evidence_blobs_retain_shared"]:
                    blob = EvidenceBlob.objects.filter(pk=bid).first()
                    if blob is None:
                        continue
                    try:
                        with transaction.atomic():
                            blob.__class__.objects.filter(pk=bid).delete()   # bulk path -> GUC-gated DB trigger
                    except ProtectedError:
                        outcome["evidence_blobs_retained_shared"] += 1       # another owner/holder still references it
                        continue
                    outcome["evidence_blobs_deleted"] += 1
                    unlink_after_commit.append(blob)
                # 5) mailboxes + their encrypted credentials
                for mb in ConnectedMailbox.objects.filter(user_id=owner_id):
                    if mb.credential_ref and credential_store.delete_token(mb.credential_ref):
                        outcome["credentials_destroyed"] += 1
                outcome["connected_mailboxes_deleted"] = ConnectedMailbox.objects.filter(user_id=owner_id).delete()[0]
                # 6) verified broker-email identities (their append-only audit survives via SET_NULL back-refs)
                outcome["identities_deleted"] = BrokerEmailIdentity.objects.filter(user_id=owner_id).delete()[0]
            finally:
                with connection.cursor() as cur:
                    cur.execute(f"SET LOCAL \"{_GUC}\" = 'off'")
    except Exception as exc:  # noqa: BLE001 — fail closed: the mutation rolled back; record a SANITISED reason OUTSIDE it
        request.status = ErasureRequest.Status.FAILED
        request.error = f"{type(exc).__name__}"[:300]
        request.save(update_fields=["status", "error"])
        logger.warning("erasure FAILED subject=%s: %s", owner_id, type(exc).__name__)
        raise

    # Post-commit: the DB mutation is durable, so now remove the physical files whose rows we deleted. A crash here
    # leaves at most an orphan file (no DB row) — benign, re-cleanable — never a dangling pointer.
    for blob in unlink_after_commit:
        if store.delete_blob_file(blob):
            outcome["evidence_files_unlinked"] += 1

    request.status = ErasureRequest.Status.EXECUTED
    request.outcome = outcome
    request.executed_at = timezone.now()
    request.error = ""
    request.save(update_fields=["status", "outcome", "executed_at", "error"])
    logger.info("erasure EXECUTED subject=%s outcome=%s", owner_id, json.dumps(outcome))
    return outcome
