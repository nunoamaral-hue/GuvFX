"""Controlled, owner-scoped ERASURE for Withdrawal Intelligence (ADR-0054; GDPR/UK-GDPR Art 17).

POST-ADVERSARIAL-REVIEW state (findings 2/3/4/6/7). This module provides the GOVERNED, fail-closed control
plane for erasure: the dry-run PLAN (pure) and the validated, concurrency-safe EXECUTE gate. The actual
destructive deletion runs ONLY through a privileged database backend that is **provisioned separately under
Sponsor authorization** (a dedicated NOLOGIN role `guvfx_erasure` + a SECURITY DEFINER procedure), because:

- **Finding 2 (privilege boundary).** Immutability is enforced by the DB triggers checking that the deleting
  session's ``current_user`` is ``guvfx_erasure`` (migration 0017) — NOT a forgeable session GUC. The ordinary
  application role is not a member of ``guvfx_erasure`` and cannot ``SET ROLE`` to it, so it can never delete
  evidence/events, even with arbitrary ``SET``/raw SQL/ORM. Until the privileged backend is provisioned the
  role does not exist and deletion is impossible for everyone — fail-closed.

This control plane adds: concurrency safety (``select_for_update`` + per-subject advisory lock + re-validation
under the lock), identity/hold/flag/token gates, and a hard readiness check. The durable deletion-outbox +
idempotent file-deletion worker (findings 3/4), the full observation-evidence union (finding 5) and the
identity-verification separation of duties (finding 7) are implemented in the privileged backend under the
provisioning gate — see ADR-0054 and docs/WV1_ERASURE_REWORK_PACKET.md. **No destructive deletion occurs in
this build; execute fails closed.**
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.db import connection, transaction
from django.utils import timezone

from . import flags
from .models import (
    BrokerEmailIdentity, BrokerEvent, BrokerMessageObservation, ConnectedMailbox, ErasureHold,
    ErasureRequest, Withdrawal,
)

logger = logging.getLogger("guvfx.broker_intelligence.erasure")

_PRIVILEGED_ROLE = "guvfx_erasure"
_ADVISORY_NS = 0x6775  # fixed namespace key for pg_advisory_xact_lock(ns, owner_id)


class ErasureRefused(Exception):
    """Raised (fail-closed) when a governed erasure must not proceed. The message is sanitised (no content)."""


def _ids(qs) -> list[int]:
    return sorted(qs.values_list("id", flat=True))


def plan_owner_erasure(owner_id: int) -> dict:
    """PURE dry-run: enumerate exactly what an erasure of ``owner_id`` would delete/retain. NO mutation.
    The candidate evidence set is the UNION of blobs referenced by this owner's BrokerEvents AND this owner's
    BrokerMessageObservations (finding 5). A blob is classified DELETE only if NO reference remains once this
    owner's events + observations are gone (NULL-safe: other = total - this_owner). Returns a plan dict incl.
    a deterministic confirmation ``token``."""
    wd = _ids(Withdrawal.objects.filter(owner_id=owner_id))
    ev = _ids(BrokerEvent.objects.filter(owner_id=owner_id))
    obs = _ids(BrokerMessageObservation.objects.filter(owner_id=owner_id))
    mbx = _ids(ConnectedMailbox.objects.filter(user_id=owner_id))
    idn = _ids(BrokerEmailIdentity.objects.filter(user_id=owner_id))
    cred_refs = (ConnectedMailbox.objects.filter(user_id=owner_id)
                 .exclude(credential_ref="").values_list("credential_ref", flat=True))

    candidate_blob_ids = sorted(set(
        list(BrokerEvent.objects.filter(owner_id=owner_id, evidence__isnull=False)
             .values_list("evidence_id", flat=True))
        + list(BrokerMessageObservation.objects.filter(owner_id=owner_id, evidence__isnull=False)
               .values_list("evidence_id", flat=True))
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
        "credential_refs_count": len(list(cred_refs)),
        "identities": idn,
        "evidence_blobs_delete": to_delete,
        "evidence_blobs_retain_shared": to_retain,
    }
    plan["token"] = _plan_token(plan)
    return plan


def _plan_token(plan: dict) -> str:
    """Deterministic digest over the owner-scoped object set — the second-confirmation token + TOCTOU guard."""
    basis = json.dumps({k: plan[k] for k in (
        "subject_user_id", "withdrawals", "broker_events", "observations", "connected_mailboxes",
        "identities", "evidence_blobs_delete", "evidence_blobs_retain_shared")}, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def privileged_backend_ready() -> bool:
    """True iff the separately-provisioned privileged erasure backend exists (the NOLOGIN role AND the
    SECURITY DEFINER procedure). Until then, execute fails closed. Provisioning is superuser + Sponsor-gated."""
    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [_PRIVILEGED_ROLE])
        has_role = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM pg_proc WHERE proname = 'bi_governed_erase_owner'")
        has_proc = cur.fetchone() is not None
    return has_role and has_proc


def assert_erasure_allowed(request: ErasureRequest) -> int:
    """Fail-closed gate. Returns the resolved subject owner id, or raises ErasureRefused. Checks: flag ON;
    request in an executable status; data-subject identity verified; an owner id is present; no ACTIVE hold."""
    if not flags.broker_intelligence_erasure_enabled():
        raise ErasureRefused("controlled erasure is disabled (BROKER_INTELLIGENCE_ERASURE_ENABLED off)")
    if request.status != ErasureRequest.Status.PLANNED:
        raise ErasureRefused(f"request status '{request.status}' is not executable")
    if not request.identity_verified:
        raise ErasureRefused("data-subject identity not verified for this request")
    owner_id = request.subject_user_id_snapshot
    if not owner_id:
        raise ErasureRefused("request has no subject owner id")
    if ErasureHold.objects.filter(subject_user_id_snapshot=owner_id, active=True).exists():
        raise ErasureRefused("an active legal/security hold blocks erasure for this subject")
    return owner_id


def execute_owner_erasure(request: ErasureRequest, confirm_token: str) -> dict:
    """GOVERNED erasure entry point — concurrency-safe + fail-closed. Acquires a row lock on the request and a
    per-subject advisory lock, RE-VALIDATES every gate under the lock, verifies the TOCTOU token against the
    live plan, and then requires the separately-provisioned privileged backend. **In this build the privileged
    backend is not provisioned, so this always refuses** — no destructive deletion occurs. The actual deletion
    (durable outbox + idempotent file worker) runs only through the provisioned SECURITY DEFINER backend."""
    assert_erasure_allowed(request)   # cheap pre-check before taking locks
    with transaction.atomic():
        # Finding 6 — concurrency: lock the request row; only one active execution per subject via an advisory
        # lock on (namespace, owner_id); re-validate everything under the lock.
        req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
        owner_id = req.subject_user_id_snapshot
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_ADVISORY_NS, owner_id])
        owner_id = assert_erasure_allowed(req)          # re-check status/identity/hold under the lock
        plan = plan_owner_erasure(owner_id)
        if not confirm_token or confirm_token != plan["token"]:
            raise ErasureRefused("confirmation token does not match the current plan (state changed or wrong token)")
        if not privileged_backend_ready():
            raise ErasureRefused(
                "privileged erasure backend not provisioned (ADR-0054): the guvfx_erasure role + governed "
                "SECURITY DEFINER procedure must be provisioned by a superuser under Sponsor authorization "
                "(ops/erasure/provision_erasure_privilege.sql) before any destructive erasure can run. Fail-closed.")
        # When the privileged backend is provisioned, the destructive deletion + durable outbox run here via the
        # SECURITY DEFINER procedure (findings 3/4/5). Guarded so this build never deletes:
        raise ErasureRefused("governed destructive deletion is not enabled in this build (review packet gate)")


@transaction.atomic
def mark_request_held(request: ErasureRequest, reason: str) -> None:
    """Explicit status transition when a hold blocks a request (sanitised reason; no content)."""
    req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
    req.status = ErasureRequest.Status.HELD
    req.error = reason[:300]
    req.save(update_fields=["status", "error"])
    logger.info("erasure request %s HELD: %s", req.id, reason[:80])
