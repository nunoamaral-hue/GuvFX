"""Controlled, owner-scoped ERASURE for Withdrawal Intelligence (ADR-0054; GDPR/UK-GDPR Art 17).

POST-ADVERSARIAL-REVIEW state. This module is the GOVERNED, fail-closed CONTROL PLANE: the pure dry-run PLAN, the
two-person separation-of-duties lifecycle, the TOCTOU confirmation token, a comprehensive privileged-backend
READINESS check, the validated+concurrency-safe EXECUTE gate, and the idempotent file-deletion OUTBOX worker.

The actual destructive DELETION runs ONLY through a privileged database backend that is **provisioned separately
under Sponsor authorization** (ops/erasure/provision_erasure_privilege.sql): a dedicated NOLOGIN role
``guvfx_erasure`` owning a SECURITY DEFINER procedure, with table ownership moved to ``guvfx_schema_owner`` and
DELETE/TRUNCATE revoked from the application role. Why role-enforced, not a GUC (Finding 2): the
``EvidenceBlob``/``BrokerEvent`` BEFORE-DELETE triggers (migration 0017) permit a delete only when the session's
``current_user`` is ``guvfx_erasure`` — a forgeable session GUC was rejected. The app role is not a member of any
privileged role and cannot ``SET ROLE``, so EVERY ordinary DML DELETE (ORM/bulk/raw) is blocked; TRUNCATE/DDL
bypasses are closed by the provisioning ownership move + revocations.

**No destructive deletion occurs in this build: ``execute`` fails closed** (the privileged backend is not
provisioned and governed deletion is gated). See ADR-0054 and docs/WV1_ERASURE_REWORK_PACKET.md.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.db import connection, transaction
from django.utils import timezone

from . import flags
from .models import (
    BrokerEmailIdentity, BrokerEvent, BrokerMessageObservation, ConnectedMailbox, DeletionTask, ErasureHold,
    ErasureRequest, Withdrawal,
)

logger = logging.getLogger("guvfx.broker_intelligence.erasure")

# Privileged identities the provisioning script (ops/erasure/provision_erasure_privilege.sql) creates. The
# readiness check re-verifies the whole boundary against these before any execute could proceed.
_PRIVILEGED_ROLE = "guvfx_erasure"          # NOLOGIN; owns the SECURITY DEFINER proc; the trigger identity
_EXECUTOR_ROLE = "guvfx_erasure_exec"       # LOGIN; the ONLY role granted EXECUTE on the proc (separate credential)
_SCHEMA_OWNER_ROLE = "guvfx_schema_owner"   # NOLOGIN; owns the evidence tables + ordinary trigger functions
_PROC_NAME = "bi_governed_erase_owner"
_PROC_SIG = "bigint, bytea"                 # pg_get_function_identity_arguments form (request id, confirmation)
_EVIDENCE_TABLES = ("broker_intelligence_evidenceblob", "broker_intelligence_brokerevent")
_ADVISORY_NS = 0x6775                        # fixed namespace key for pg_advisory_xact_lock(ns, owner_id)
_MAX_DELETION_ATTEMPTS = 5                   # a file-deletion task that fails this many times is dead-lettered


class ErasureRefused(Exception):
    """Raised (fail-closed) when a governed erasure must not proceed. The message is sanitised (no content)."""


def _ids(qs) -> list[int]:
    return sorted(qs.values_list("id", flat=True))


def _ref_digest(value: str) -> str:
    """Stable, NON-secret digest of a credential reference. The raw ``credential_ref`` is a filesystem pointer to
    an encrypted token; we never put it in the plan/token basis in the clear — only its sha256, so the digest
    still changes if the subject re-authenticates to a new credential file (Finding 5: credential-ref drift)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def plan_owner_erasure(owner_id: int) -> dict:
    """PURE dry-run: enumerate exactly what an erasure of ``owner_id`` would delete/retain. NO mutation.
    The candidate evidence set is the UNION of blobs referenced by this owner's BrokerEvents AND this owner's
    BrokerMessageObservations (Finding 5a). A blob is classified DELETE only if NO reference remains once this
    owner's events + observations are gone (NULL-safe: other = total - this_owner). The returned plan carries a
    deterministic confirmation ``token`` over ALL mutable facts, including stable credential-ref digests."""
    wd = _ids(Withdrawal.objects.filter(owner_id=owner_id))
    ev = _ids(BrokerEvent.objects.filter(owner_id=owner_id))
    obs = _ids(BrokerMessageObservation.objects.filter(owner_id=owner_id))
    mbx = _ids(ConnectedMailbox.objects.filter(user_id=owner_id))
    idn = _ids(BrokerEmailIdentity.objects.filter(user_id=owner_id))
    cred_refs = sorted(ConnectedMailbox.objects.filter(user_id=owner_id)
                       .exclude(credential_ref="").values_list("credential_ref", flat=True))
    cred_ref_digests = sorted(_ref_digest(r) for r in cred_refs)

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
        "credential_refs_count": len(cred_refs),
        "credential_ref_digests": cred_ref_digests,
        "identities": idn,
        "evidence_blobs_delete": to_delete,
        "evidence_blobs_retain_shared": to_retain,
    }
    plan["token"] = _plan_token(plan)
    return plan


def _plan_token(plan: dict) -> str:
    """Deterministic digest over the FULL owner-scoped mutable fact set — the second-confirmation token + TOCTOU
    guard. Includes credential-ref digests so a re-auth (new credential file) between plan and execute invalidates
    the token even when every id set is unchanged (Finding 5b)."""
    basis = json.dumps({k: plan[k] for k in (
        "subject_user_id", "withdrawals", "broker_events", "observations", "connected_mailboxes",
        "credential_ref_digests", "identities", "evidence_blobs_delete", "evidence_blobs_retain_shared")},
        sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def privileged_backend_readiness() -> tuple[bool, list[str]]:
    """Comprehensive, fail-closed readiness probe for the separately-provisioned privileged backend (Finding 4).
    Returns ``(ready, reasons)``; ``ready`` is True ONLY when every invariant holds. Any missing object, wrong
    owner/ACL, app-role membership/privilege, or probe error => NOT READY with a sanitised reason. Every query is
    guarded so it cannot raise in an unprovisioned database (it must not poison the caller's transaction)."""
    reasons: list[str] = []
    try:
        with connection.cursor() as cur:
            def _exists(sql, params):
                cur.execute(sql, params)
                return cur.fetchone() is not None

            cur.execute("SELECT current_user")
            app_role = cur.fetchone()[0]

            # (1) Required roles exist.
            for role in (_PRIVILEGED_ROLE, _EXECUTOR_ROLE, _SCHEMA_OWNER_ROLE):
                if not _exists("SELECT 1 FROM pg_roles WHERE rolname = %s", [role]):
                    reasons.append(f"role '{role}' absent")
            roles_ok = not reasons

            # (2) Procedure present, with the expected signature / SECURITY DEFINER / owner / pinned search_path.
            cur.execute(
                "SELECT p.oid, p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig, "
                "pg_get_function_identity_arguments(p.oid) "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE p.proname = %s", [_PROC_NAME])
            prow = cur.fetchone()
            if prow is None:
                reasons.append(f"procedure '{_PROC_NAME}' absent")
            else:
                proc_oid, prosecdef, proc_owner, proconfig, proc_args = prow
                if (proc_args or "").replace(" ", "") != _PROC_SIG.replace(" ", ""):
                    reasons.append(f"procedure signature mismatch (got '{proc_args}')")
                if not prosecdef:
                    reasons.append("procedure is not SECURITY DEFINER")
                if proc_owner != _PRIVILEGED_ROLE:
                    reasons.append(f"procedure not owned by '{_PRIVILEGED_ROLE}'")
                if not proconfig or not any(c.lower().startswith("search_path=") for c in proconfig):
                    reasons.append("procedure search_path not pinned")

                # (3) EXECUTE ACL: executor yes; PUBLIC and the app role NO. (The executor check is guarded by
                # roles_ok because has_function_privilege raises on a nonexistent role.)
                if roles_ok:
                    cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", [_EXECUTOR_ROLE, proc_oid])
                    if not cur.fetchone()[0]:
                        reasons.append(f"'{_EXECUTOR_ROLE}' lacks EXECUTE on the procedure")
                cur.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", [proc_oid])
                if cur.fetchone()[0]:
                    reasons.append("PUBLIC must not have EXECUTE on the procedure")
                cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", [app_role, proc_oid])
                if cur.fetchone()[0]:
                    reasons.append("application role must not have EXECUTE on the procedure")

            # (4) Evidence tables owned by the schema owner (not the app role) — closes TRUNCATE/DDL.
            for tbl in _EVIDENCE_TABLES:
                cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = %s", [tbl])
                orow = cur.fetchone()
                if orow and orow[0] != _SCHEMA_OWNER_ROLE:
                    reasons.append(f"table '{tbl}' not owned by '{_SCHEMA_OWNER_ROLE}'")

            # (5) The app role must NOT be a member of any privileged role (cannot SET ROLE to the trigger identity).
            if roles_ok:
                for role in (_PRIVILEGED_ROLE, _EXECUTOR_ROLE, _SCHEMA_OWNER_ROLE):
                    cur.execute("SELECT pg_has_role(%s, %s, 'USAGE')", [app_role, role])
                    if cur.fetchone()[0]:
                        reasons.append(f"application role must not be a member of '{role}'")

            # (6) The app role must lack DELETE and TRUNCATE on the evidence tables.
            for tbl in _EVIDENCE_TABLES:
                for priv in ("DELETE", "TRUNCATE"):
                    cur.execute("SELECT has_table_privilege(%s, %s, %s)", [app_role, tbl, priv])
                    if cur.fetchone()[0]:
                        reasons.append(f"application role must not have {priv} on '{tbl}'")
    except Exception:  # noqa: BLE001 — any probe error is treated as NOT READY (fail-closed), never as ready.
        logger.warning("privileged backend readiness probe errored; treating as NOT READY", exc_info=True)
        return False, ["readiness probe error (treated as not ready)"]
    return (len(reasons) == 0), reasons


def privileged_backend_ready() -> bool:
    """True iff the separately-provisioned privileged erasure backend satisfies EVERY readiness invariant
    (Finding 4). Until then, execute fails closed. Provisioning is superuser + Sponsor-gated."""
    ready, _ = privileged_backend_readiness()
    return ready


@transaction.atomic
def verify_identity(request: ErasureRequest, verifier, method: str) -> ErasureRequest:
    """Record out-of-band data-subject identity verification (lifecycle step 1; Finding 7). Stores the verifier,
    timestamp and a METHOD CATEGORY only (never ID documents). Separate invocation from approve/execute."""
    req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
    # Finding (round-3 adversarial): verification is only meaningful before approval; refuse to overwrite the
    # recorded verifier once a plan has been approved or executed.
    if req.status not in (ErasureRequest.Status.REQUESTED, ErasureRequest.Status.PLANNED):
        raise ErasureRefused(f"cannot verify identity once request status is '{req.status}'")
    if getattr(verifier, "pk", None) is None:
        raise ErasureRefused("a verifier identity is required")
    req.identity_verified = True
    req.verified_by = verifier
    req.verified_at = timezone.now()
    req.verification_method = (method or "")[:60]
    req.save(update_fields=["identity_verified", "verified_by", "verified_at", "verification_method"])
    logger.info("erasure request %s identity verified (method=%s)", req.id, req.verification_method)
    return req


@transaction.atomic
def approve_request(request: ErasureRequest, approver, *, approval_ttl_minutes: int = 60) -> ErasureRequest:
    """Independent two-person APPROVAL (lifecycle step 2; Finding 7). Requires a produced plan (PLANNED),
    verified identity, and an approver who is a DIFFERENT person from the requester. Sets an approval expiry; a
    separate invocation from execute. Fail-closed via ErasureRefused on any violation."""
    req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
    if req.status != ErasureRequest.Status.PLANNED:
        raise ErasureRefused(f"request status '{req.status}' is not approvable (need a produced plan)")
    if not req.identity_verified:
        raise ErasureRefused("cannot approve: data-subject identity not verified")
    approver_id = getattr(approver, "pk", None)
    if approver_id is None:
        raise ErasureRefused("an approver identity is required")
    if req.requested_by_id is None:
        # Finding (round-3 adversarial HIGH): without a recorded requester the two-person rule cannot be
        # enforced, so approval must fail closed rather than vacuously pass.
        raise ErasureRefused("two-person rule: no requester recorded; cannot enforce separation of duties")
    if approver_id == req.requested_by_id:
        raise ErasureRefused("two-person rule: the approver must differ from the requester")
    now = timezone.now()
    req.approved_by = approver
    req.approved_at = now
    req.approval_revoked = False
    req.approval_expires_at = now + timezone.timedelta(minutes=max(1, approval_ttl_minutes))
    req.status = ErasureRequest.Status.APPROVED
    req.save(update_fields=["approved_by", "approved_at", "approval_revoked", "approval_expires_at", "status"])
    logger.info("erasure request %s APPROVED (two-person)", req.id)
    return req


def assert_erasure_allowed(request: ErasureRequest) -> int:
    """Fail-closed gate. Returns the resolved subject owner id, or raises ErasureRefused. Checks: flag ON; request
    APPROVED; identity verified; a valid, non-revoked, non-expired two-person approval (approver != requester);
    an owner id is present; and no ACTIVE hold (Findings 6/7)."""
    if not flags.broker_intelligence_erasure_enabled():
        raise ErasureRefused("controlled erasure is disabled (BROKER_INTELLIGENCE_ERASURE_ENABLED off)")
    if request.status != ErasureRequest.Status.APPROVED:
        raise ErasureRefused(f"request status '{request.status}' is not executable (needs two-person approval)")
    if not request.identity_verified:
        raise ErasureRefused("data-subject identity not verified for this request")
    if request.approved_by_id is None or request.approved_at is None:
        raise ErasureRefused("request has no recorded approval")
    if request.approval_revoked:
        raise ErasureRefused("the approval for this request has been revoked")
    if request.approval_expires_at is not None and request.approval_expires_at < timezone.now():
        raise ErasureRefused("the approval for this request has expired")
    if request.requested_by_id is None:
        # Finding (round-3 adversarial HIGH): a NULL requester makes the two-person check vacuous — fail closed.
        raise ErasureRefused("two-person rule: no requester recorded; cannot enforce separation of duties")
    if request.approved_by_id == request.requested_by_id:
        raise ErasureRefused("two-person rule violated: approver equals requester")
    owner_id = request.subject_user_id_snapshot
    if not owner_id:
        raise ErasureRefused("request has no subject owner id")
    if ErasureHold.objects.filter(subject_user_id_snapshot=owner_id, active=True).exists():
        raise ErasureRefused("an active legal/security hold blocks erasure for this subject")
    return owner_id


def execute_owner_erasure(request: ErasureRequest, confirm_token: str, executor=None) -> dict:
    """GOVERNED erasure entry point — concurrency-safe + fail-closed. Locks the request row + a per-subject
    advisory lock, RE-VALIDATES every gate under the lock, verifies the TOCTOU token against the live plan, and
    then requires the fully-provisioned privileged backend (Finding 4). **In this build the privileged backend is
    not provisioned and governed deletion is gated, so this always refuses** — no destructive deletion occurs. The
    actual deletion + durable outbox writes run only inside the provisioned SECURITY DEFINER procedure."""
    assert_erasure_allowed(request)   # cheap pre-check before taking locks
    with transaction.atomic():
        # Finding 6 — concurrency: lock the request row; only one active execution per subject via an advisory
        # lock on (namespace, owner_id); re-validate everything under the lock.
        req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
        owner_id = req.subject_user_id_snapshot
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_ADVISORY_NS, owner_id])
        owner_id = assert_erasure_allowed(req)          # re-check status/identity/approval/hold under the lock
        plan = plan_owner_erasure(owner_id)
        if not confirm_token or confirm_token != plan["token"]:
            raise ErasureRefused("confirmation token does not match the current plan (state changed or wrong token)")
        ready, reasons = privileged_backend_readiness()
        if not ready:
            raise ErasureRefused(
                "privileged erasure backend not ready (ADR-0054): the guvfx_erasure / guvfx_erasure_exec / "
                "guvfx_schema_owner roles + the governed SECURITY DEFINER procedure must be provisioned by a "
                "superuser under Sponsor authorization (ops/erasure/provision_erasure_privilege.sql) before any "
                f"destructive erasure can run. Fail-closed. Unmet: {'; '.join(reasons[:4])}")
        # When provisioned, the destructive deletion + durable DeletionTask outbox writes + audit anonymisation
        # run here inside the SECURITY DEFINER procedure (Findings 3/4/5). Guarded so this build never deletes:
        raise ErasureRefused("governed destructive deletion is not enabled in this build (review packet gate)")


def process_deletion_tasks(limit: int = 100) -> dict:
    """Idempotent FILE-deletion OUTBOX worker (ADR-0054 Findings 3/4). Runs AFTER the governed transaction has
    committed its row deletes + enqueued tasks. Drains PENDING/FAILED ``DeletionTask`` rows, removing each target
    file: a missing/already-removed file is SUCCESS (idempotent); a removal error marks the task FAILED (retryable)
    until ``_MAX_DELETION_ATTEMPTS``, after which it is DEAD-lettered (no retry storm). An EXECUTED request whose
    outbox is fully drained is advanced to COMPLETED; if any task dead-lettered it is moved to FAILED instead
    (a real file could not be removed, so erasure is incomplete — operator intervention). A crash after commit
    leaves a durable PENDING task (not an orphan file). ``SELECT ... FOR UPDATE SKIP LOCKED`` lets concurrent
    workers process disjoint tasks; the request row is locked BEFORE the drained-check so the last committer
    observes the true task state (no completion race). In this build execute is fail-closed → normally a no-op."""
    from .credential_store import delete_token
    from .evidence import EvidenceStore

    store = EvidenceStore()
    processed = {"completed": 0, "failed": 0, "dead": 0}
    touched_request_ids: set[int] = set()

    with transaction.atomic():
        tasks = list(DeletionTask.objects
                     .select_for_update(skip_locked=True)
                     .filter(status__in=[DeletionTask.Status.PENDING, DeletionTask.Status.FAILED])
                     .order_by("id")[:limit])
        for task in tasks:
            task.attempts += 1
            try:
                if task.kind == DeletionTask.Kind.EVIDENCE_FILE:
                    removed = store.delete_file_by_sha256(task.target_ref)   # missing => False, never raises
                elif task.kind == DeletionTask.Kind.CREDENTIAL_FILE:
                    removed = delete_token(task.target_ref)                  # missing => False (bad ref => raises)
                else:
                    raise ValueError(f"unknown deletion task kind: {task.kind}")
                task.status = DeletionTask.Status.COMPLETED
                task.completed_at = timezone.now()
                task.last_error = "" if removed else "file already absent at processing time"
                processed["completed"] += 1
            except Exception as exc:  # noqa: BLE001 — a failed unlink is retryable, never fatal to the worker.
                if task.attempts >= _MAX_DELETION_ATTEMPTS:
                    task.status = DeletionTask.Status.DEAD
                    processed["dead"] += 1
                else:
                    task.status = DeletionTask.Status.FAILED
                    processed["failed"] += 1
                task.last_error = str(exc)[:300]
                logger.warning("deletion task %s -> %s (attempt %s): %s",
                               task.id, task.status, task.attempts, task.last_error)
            task.save(update_fields=["status", "attempts", "completed_at", "last_error"])
            touched_request_ids.add(task.request_id)

        # Advance/flag each touched EXECUTED request. Lock the request row BEFORE reading the drained state so two
        # concurrent workers serialize on it and whichever commits last sees every task (round-3 adversarial MED).
        for req_id in touched_request_ids:
            req = ErasureRequest.objects.select_for_update().get(pk=req_id)
            if req.status != ErasureRequest.Status.EXECUTED:
                continue
            if DeletionTask.objects.filter(
                    request_id=req_id,
                    status__in=[DeletionTask.Status.PENDING, DeletionTask.Status.FAILED]).exists():
                continue   # still retryable work to do
            if DeletionTask.objects.filter(request_id=req_id, status=DeletionTask.Status.DEAD).exists():
                req.status = ErasureRequest.Status.FAILED
                req.error = "one or more file deletions dead-lettered; operator intervention required"[:300]
                req.save(update_fields=["status", "error"])
            else:
                req.status = ErasureRequest.Status.COMPLETED
                req.save(update_fields=["status"])
    return processed


@transaction.atomic
def mark_request_held(request: ErasureRequest, reason: str) -> None:
    """Explicit status transition when a hold blocks a request (sanitised reason; no content)."""
    req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
    req.status = ErasureRequest.Status.HELD
    req.error = reason[:300]
    req.save(update_fields=["status", "error"])
    logger.info("erasure request %s HELD: %s", req.id, reason[:80])
