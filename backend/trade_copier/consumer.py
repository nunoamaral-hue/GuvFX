"""trade_copier.consumer — dedicated DEMO partial-close claim/complete consumer (ADR-0056).

A SEPARATE dispatch path from the generic ``next_job``. It claims ONLY ``PARTIAL_CLOSE_TRADE`` for a dedicated
``partial_close_worker`` identity, derives the follower identity pin SERVER-side, and marks the job RUNNING +
leased durably so a lost response can never make it claimable again (no double-send). Completion drives the
ADR-0056 result state machine + authoritative reconciliation.

Hard rails (ADR-0056): DARK — nothing here is reachable until a Sponsor grants a ``partial_close_worker``
identity AND ``COPIER_EXECUTE_DEMO_ARMED`` is on; the two gates are re-checked UNDER the row lock. The worker
supplies NO authority: ticket / volume / symbol / login / server / account / replication all come from the
claimed job + the authoritative ``CopierReplication`` + account binding, never the request. The bridge
``partial_close_position`` re-verifies the pin immediately before the mutation.
"""
from __future__ import annotations

import logging
import os
import uuid

from django.db import transaction
from django.utils import timezone

from core.audit import log_event
from execution.models import ExecutionJob
from trade_copier import reconcile
from trade_copier.flags import copier_enabled, copier_execute_demo_armed, copier_execute_enabled
from trade_copier.models import CopierReplication, ReplicationStatus
from trade_copier.replication import _broker_constraints

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 120
# A generic-mutation grant that must NOT co-exist with partial_close_worker (mutually-exclusive classes).
_GENERIC_MUTATION_PERMS = ("shadow_worker",)

# ── dispatch-lifecycle phases (recorded on ExecutionJob.result; free-form JSON, no migration) ─────────
# claim -> DISPATCHED; authorize_send -> AUTHORIZED (single-use, carries the send nonce); complete claims
# COMPLETING while it drives reconciliation; then a TERMINAL phase. The phase is the authorization state;
# a completion is refused unless a send was AUTHORIZED, and a second authorize is refused once AUTHORIZED.
PHASE_DISPATCHED = "dispatched"
PHASE_AUTHORIZED = "authorized"
PHASE_COMPLETING = "completing"
PHASE_RECONCILED = "reconciled"
PHASE_DIVERGENT = "divergent"
PHASE_OPERATOR_REVIEW = "operator_review"
PHASE_DISPATCH_REFUSED = "dispatch_refused"
PHASE_RECON_PENDING = "recon_pending"          # sent; authoritative read not yet available (sweep re-reads)

# ── background reconcile sweep tunables (ADR-0056 F4) ─────────────────────────────────────────────────
# A partial-close job whose worker died after authorize_send / order_send sits RUNNING; the sweep re-reads
# the authoritative remaining volume once the lease is WELL expired (never re-sends), and escalates to
# operator review after a bounded number of unavailable reads.
_SWEEP_GRACE_SECONDS = int(os.getenv("COPIER_PCLOSE_SWEEP_GRACE_SECONDS", "300") or 300)
_SWEEP_MAX_RECON_ATTEMPTS = int(os.getenv("COPIER_PCLOSE_SWEEP_MAX_ATTEMPTS", "12") or 12)

# Enumerated bridge error codes that PROVABLY occur BEFORE ``order_send`` in ``partial_close_position`` — a
# result carrying one of these means the position is untouched (terminal dispatch-refused, no read needed).
# ANY OTHER not-ok result — crucially the bridge's catch-all ``"exception"``, which can be raised AFTER the
# send (IPC loss / terminal crash mid-ACK) — is treated as reconcile-REQUIRED. Fail SAFE: when we cannot
# PROVE nothing was sent, we read the broker authoritatively rather than record "nothing happened".
_PRESEND_REFUSAL_ERRORS = frozenset({
    "mt5_not_installed", "mt5_init_failed", "account_info_failed", "account_not_demo",
    "account_mode_not_certified", "volume_invalid", "volume_non_positive", "expected_symbol_required",
    "identity_pin_required", "positions_get_failed", "position_not_found", "multiple_positions_returned",
    "ticket_mismatch", "symbol_changed", "symbol_info_failed", "symbol_constraints_unavailable",
    "unsupported_filling_mode", "would_be_full_close", "rounds_to_zero", "below_min_reduction",
    "remainder_below_min", "tick_failed", "identity_rejected",
})


def _safe_error_code(value) -> str:
    """A log/audit-safe rendering of a bridge error field: a short code only. Guards the F6 "no secrets in
    audit" guarantee against a value-shaped leak (``_sanitize_metadata`` scrubs by KEY, not value) — anything
    not a bare code-like token is collapsed to ``"unsanitised"``."""
    s = str(value or "unknown")
    return s[:48] if (s and all(c.isalnum() or c in "_-." for c in s)) else "unsanitised"

# ── audit event family (ADR-0056 F6) — sanitised; safe identifiers + reason/status codes ONLY ─────────
AUDIT_CLAIM_ACCEPTED = "COPIER_PCLOSE_CLAIM_ACCEPTED"
AUDIT_CLAIM_REFUSED = "COPIER_PCLOSE_CLAIM_REFUSED"
AUDIT_SEND_AUTHORIZED = "COPIER_PCLOSE_SEND_AUTHORIZED"
AUDIT_SEND_AUTH_REFUSED = "COPIER_PCLOSE_SEND_AUTH_REFUSED"
AUDIT_DISPATCH_ATTEMPTED = "COPIER_PCLOSE_DISPATCH_ATTEMPTED"
AUDIT_OUTCOME_UNKNOWN = "COPIER_PCLOSE_OUTCOME_UNKNOWN"
AUDIT_RECONCILED = "COPIER_PCLOSE_RECONCILED"
AUDIT_DIVERGENCE = "COPIER_PCLOSE_DIVERGENCE"
AUDIT_OPERATOR_REVIEW = "COPIER_PCLOSE_OPERATOR_REVIEW"
AUDIT_DISPATCH_REFUSED_EVT = "COPIER_PCLOSE_DISPATCH_REFUSED"


def _audit(event, *, severity="INFO", replication=None, job=None, worker_id=None, reason=None,
           status=None, extra=None) -> None:
    """Emit a sanitised ADR-0056 partial-close audit event via the fail-open ``core.audit.log_event``.

    Records ONLY safe identifiers + reason/status codes (replication / job / account ids, worker id, reason
    code, recon status). NEVER a login, server, secret, ticket price or raw customer payload
    (``core.audit._sanitize_metadata`` scrubs known secret keys as a further backstop). An audit failure
    never affects the claim / authorize / complete decision."""
    try:
        meta: dict = {"worker_id": worker_id}
        if replication is not None:
            meta["replication_id"] = replication.id
            meta["account_id"] = getattr(getattr(replication, "follower", None), "follower_account_id", None)
        if job is not None:
            meta["job_id"] = job.id
            meta.setdefault("account_id", job.account_id)
        if reason is not None:
            meta["reason"] = reason
        if status is not None:
            meta["recon_status"] = status
        if extra:
            meta.update(extra)
        entity_id = str(replication.id if replication is not None
                        else (job.id if job is not None else ""))
        log_event(request=None, event_type=event, severity=severity,
                  entity_type="copier_replication", entity_id=entity_id, metadata=meta)
    except Exception:  # noqa: BLE001 — audit must never break the operation
        logger.debug("copier pclose audit emit failed for %s", event, exc_info=True)


class ClaimRefused(Exception):
    """A sanitised claim/complete refusal (reason is safe to surface; never contains credentials)."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ── worker-class separation (points 1-2) ────────────────────────────────────────────────────────────

def is_partial_close_worker(perms: dict) -> bool:
    return bool((perms or {}).get("partial_close_worker"))


def assert_dedicated_worker(worker_identity) -> dict:
    """The dedicated endpoint accepts ONLY a pure partial_close_worker (modern auth). A worker that ALSO
    carries a generic-mutation grant is refused — the two classes are mutually exclusive by construction."""
    if worker_identity is None:
        raise ClaimRefused("worker_auth_required")
    perms = worker_identity.worker_permissions or {}
    if not is_partial_close_worker(perms):
        raise ClaimRefused("not_partial_close_worker")
    if any(perms.get(p) for p in _GENERIC_MUTATION_PERMS):
        raise ClaimRefused("mixed_worker_class_refused")
    return perms


# ── server-derived identity pin (point 5) ───────────────────────────────────────────────────────────

def derive_identity_pin(account) -> dict | None:
    """Derive the expected (login, server) from the account's AUTHORITATIVE durable binding — the MT5 login
    is ``TradingAccount.account_number`` and the server is the bound ``broker_server.server_name``. Applies to
    hosted (provisioned binding) and non-hosted alike. Returns None (fail closed) on a blank/unresolved
    binding. NEVER sourced from a worker request or customer payload. ``require_identity_pin`` forces the
    bridge to treat it as mandatory (no env fallback)."""
    login = str(getattr(account, "account_number", "") or "").strip()
    bs = getattr(account, "broker_server", None)
    server = str(getattr(bs, "server_name", "") or "").strip() if bs is not None else ""
    if not login or not server:
        return None
    return {"expected_login": login, "expected_server": server, "require_identity_pin": True}


# ── authoritative route (point 3) ────────────────────────────────────────────────────────────────────

def _replication_for_job(job) -> CopierReplication | None:
    return (CopierReplication.objects
            .select_related("follower", "follower__follower_account",
                            "follower__follower_account__broker_server", "master_event")
            .filter(execution_job_id=job.id, requested_op="PARTIAL_CLOSE").first())


# ── in-lock claim gates (point 4) ────────────────────────────────────────────────────────────────────

def _flags_ok() -> bool:
    return copier_enabled() and copier_execute_enabled() and copier_execute_demo_armed()


def _admit_claim_locked(job, replication, *, active_node_ids, authorized_account_id) -> tuple[bool, str]:
    """Re-validate EVERYTHING from authoritative records under the row lock, immediately before RUNNING."""
    if not _flags_ok():
        return False, "flags_off"
    if job.status != ExecutionJob.Status.PENDING:
        return False, "job_not_pending"
    if job.job_type != ExecutionJob.JobType.PARTIAL_CLOSE_TRADE:
        return False, "job_not_partial_close"
    if replication is None:
        return False, "replication_missing"
    if replication.status != ReplicationStatus.EXECUTION_REQUESTED:
        return False, "replication_not_execution_requested"
    # No prior send/outcome/reconciliation terminal marker — never re-enter the dispatch lifecycle.
    if replication.reconciliation_status in reconcile.PARTIAL_DISPATCH_MARKERS:
        return False, "already_dispatched"
    follower = replication.follower
    if not follower.enabled:
        return False, "follower_disabled"
    acct = follower.follower_account
    if not getattr(acct, "is_active", False):
        return False, "account_inactive"
    # Exact route: the job's node + account must match the worker's authorization, derived relationally.
    if job.account_id != acct.id or job.account_id != authorized_account_id:
        return False, "account_route_mismatch"
    if job.terminal_node_id is None or job.terminal_node_id not in (active_node_ids or []):
        return False, "node_route_mismatch"
    # DEMO env resolves positively (never LIVE/unresolved).
    try:
        from trading.account_policy import is_demo_environment
        if not is_demo_environment(acct):
            return False, "not_demo"
    except Exception:  # noqa: BLE001
        return False, "environment_unresolved"
    return True, "ok"


def claim_partial_close(worker_identity, *, account_id: int, authorized_node_hostnames) -> dict:
    """Claim (at most) one PARTIAL_CLOSE_TRADE for the dedicated worker. The worker supplies NO routing or
    identity authority — ``account_id`` is the worker's own authorized account and ``authorized_node_hostnames``
    its authorized node(s); every fact is re-derived from the job + replication + account binding under the
    lock. Durable RUNNING + lease before the job leaves the server (no double-send).

    The claim returns ONLY an acknowledgment (job id + dispatch token). It does NOT return the mutation
    instruction (ticket / volume / symbol / identity pin): those are released solely by ``authorize_send``
    after a fresh in-lock revalidation of the three feature flags (ADR-0056 F1), so arming turned off between
    claim and send can never reach the broker."""
    assert_dedicated_worker(worker_identity)
    wid = getattr(worker_identity, "worker_id", None)
    if not _flags_ok():
        _audit(AUDIT_CLAIM_REFUSED, worker_id=wid, reason="flags_off")
        return {"ok": False, "reason": "flags_off"}
    if account_id is None:
        return {"ok": False, "reason": "no_authorized_account"}
    # Resolve the worker's authorized node HOSTNAMES to ACTIVE node ids (enforces node-active), mirroring the
    # generic claim seam. A NULL-node job never resolves here, so an unstamped job is categorically unclaimable.
    from execution.models import TerminalNode
    active_node_ids = list(TerminalNode.objects.filter(
        hostname__in=list(authorized_node_hostnames or []), status=TerminalNode.Status.ACTIVE
    ).values_list("id", flat=True))
    if not active_node_ids:
        return {"ok": False, "reason": "no_active_authorized_node"}

    with transaction.atomic():
        job = (ExecutionJob.objects
               .select_for_update(skip_locked=True)
               .filter(status=ExecutionJob.Status.PENDING,
                       job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
                       account_id=account_id, terminal_node_id__in=active_node_ids)
               .order_by("created_at").first())
        if job is None:
            return {"ok": False, "reason": "no_jobs"}
        replication = _replication_for_job(job)
        ok, reason = _admit_claim_locked(
            job, replication, active_node_ids=active_node_ids, authorized_account_id=account_id)
        if not ok:
            _audit(AUDIT_CLAIM_REFUSED, replication=replication, job=job, worker_id=wid, reason=reason)
            return {"ok": False, "reason": reason}

        acct = replication.follower.follower_account
        if derive_identity_pin(acct) is None:   # fail closed at claim if no pin can be server-derived
            _audit(AUDIT_CLAIM_REFUSED, replication=replication, job=job, worker_id=wid,
                   reason="identity_pin_unresolved")
            return {"ok": False, "reason": "identity_pin_unresolved"}

        dispatch_token = uuid.uuid4().hex
        job.status = ExecutionJob.Status.RUNNING
        job.worker_id = worker_identity.worker_id
        job.started_at = job.started_at or timezone.now()
        job.lease_expires_at = timezone.now() + timezone.timedelta(seconds=_LEASE_SECONDS)
        # Persist that the job entered the mutation-dispatch lifecycle (never re-claimable on a lost response).
        job.result = {"dispatch_token": dispatch_token, "worker_id": worker_identity.worker_id,
                      "phase": PHASE_DISPATCHED}
        job.save(update_fields=["status", "worker_id", "started_at", "lease_expires_at", "result"])
        replication.reconciliation_status = reconcile.PARTIAL_RECON_PENDING
        replication.save(update_fields=["reconciliation_status", "updated_at"])

    _audit(AUDIT_CLAIM_ACCEPTED, replication=replication, job=job, worker_id=wid)
    # Acknowledgment ONLY — the mutation instruction is released by authorize_send, never here.
    return {"ok": True, "job_id": job.id, "dispatch_token": dispatch_token,
            "replication_id": replication.id, "phase": PHASE_DISPATCHED}


def _completion_authorized(job, worker_identity, dispatch_token) -> bool:
    """Authorize a completion / authorize-send for a dispatched job: the SAME claiming worker AND the SAME
    non-null dispatch token.

    Lease expiry is DELIBERATELY not an authorization boundary here (ADR-0056 F5). The dispatch token + the
    claiming worker id ARE the authority; completion is idempotent and there is NO re-send path, so a worker
    whose lease lapsed mid-flight must still be able to report its single result (resolved authoritatively),
    and a job whose worker died is resolved by the read-only background reconcile sweep — never by a blind
    re-dispatch. The lease governs only orphan DETECTION, and PARTIAL_CLOSE_TRADE is excluded from every
    orphan PENDING-requeue path, so an expired lease can never cause a second send. (Renamed from the earlier
    ``_worker_lease_ok`` to stop implying a lease check it never performed.)"""
    res = job.result or {}
    return (job.worker_id == worker_identity.worker_id
            and res.get("dispatch_token") == dispatch_token
            and res.get("dispatch_token") is not None)


def _admit_authorize_locked(job, replication, worker_identity, dispatch_token, *, active_node_ids,
                            authorized_account_id) -> tuple[bool, str]:
    """Re-validate EVERYTHING under the job row lock immediately before the single-use send authorization is
    released (ADR-0056 F1). The bridge independently re-verifies MT5 identity before ``order_send``; this is
    the backend's authoritative re-check of the three feature flags + full route/replication/identity that the
    bridge cannot see."""
    if not _completion_authorized(job, worker_identity, dispatch_token):
        return False, "not_authorized"
    if job.status != ExecutionJob.Status.RUNNING:
        return False, "job_not_running"
    res = job.result or {}
    # Single-use: never a second authorization per dispatch.
    if res.get("phase") == PHASE_AUTHORIZED or res.get("send_nonce"):
        return False, "already_authorized"
    if res.get("phase") != PHASE_DISPATCHED:
        return False, "not_dispatch_phase"
    if not _flags_ok():                                 # re-read ALL THREE flags IN-LOCK (the F1 fix)
        return False, "flags_off"
    if replication is None:
        return False, "replication_missing"
    if replication.status != ReplicationStatus.EXECUTION_REQUESTED:
        return False, "replication_not_execution_requested"
    if replication.reconciliation_status != reconcile.PARTIAL_RECON_PENDING:
        return False, "not_recon_pending"              # already sent / terminal -> never re-authorize
    if replication.follower_ticket is None:
        return False, "no_follower_ticket"             # explicit: the instruction targets a verified ticket
    follower = replication.follower
    if not follower.enabled:
        return False, "follower_disabled"
    acct = follower.follower_account
    if not getattr(acct, "is_active", False):
        return False, "account_inactive"
    if job.account_id != acct.id or job.account_id != authorized_account_id:
        return False, "account_route_mismatch"
    if job.terminal_node_id is None or job.terminal_node_id not in (active_node_ids or []):
        return False, "node_route_mismatch"
    try:
        from trading.account_policy import is_demo_environment
        if not is_demo_environment(acct):
            return False, "not_demo"
    except Exception:  # noqa: BLE001
        return False, "environment_unresolved"
    return True, "ok"


def authorize_send(worker_identity, *, job_id: int, dispatch_token: str, account_id: int,
                   authorized_node_hostnames) -> dict:
    """Single-use PRE-SEND authorization (ADR-0056 F1). Atomically, under the job row lock: re-authenticate
    the claiming dedicated worker, confirm it still owns the job, confirm the job is RUNNING in the DISPATCHED
    phase, RE-READ all three feature flags, revalidate DEMO / follower-enabled / account-active / node-route /
    account-route / replication-state / ticket / symbol / identity-pin, FAIL CLOSED on any change, persist a
    single-use send nonce + timestamp, and ONLY THEN release the mutation instruction (ticket, volume, symbol,
    server-derived identity pin). Never issues a second authorization for the same dispatch.

    The host worker MUST call this immediately before ``partial_close_position``. If authorization succeeds but
    the worker then crashes before/during the broker call, the job is NOT re-authorized or re-sent — it stays
    RUNNING and the background reconcile sweep resolves it authoritatively (never a blind re-dispatch)."""
    assert_dedicated_worker(worker_identity)
    wid = getattr(worker_identity, "worker_id", None)
    from execution.models import TerminalNode
    active_node_ids = list(TerminalNode.objects.filter(
        hostname__in=list(authorized_node_hostnames or []), status=TerminalNode.Status.ACTIVE
    ).values_list("id", flat=True))

    with transaction.atomic():
        job = (ExecutionJob.objects.select_for_update()
               .filter(id=job_id, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE).first())
        if job is None:
            raise ClaimRefused("job_not_found")
        replication = _replication_for_job(job)
        ok, reason = _admit_authorize_locked(
            job, replication, worker_identity, dispatch_token,
            active_node_ids=active_node_ids, authorized_account_id=account_id)
        if not ok:
            _audit(AUDIT_SEND_AUTH_REFUSED, replication=replication, job=job, worker_id=wid, reason=reason)
            return {"ok": False, "reason": reason}
        acct = replication.follower.follower_account
        pin = derive_identity_pin(acct)
        if pin is None:
            _audit(AUDIT_SEND_AUTH_REFUSED, replication=replication, job=job, worker_id=wid,
                   reason="identity_pin_unresolved")
            return {"ok": False, "reason": "identity_pin_unresolved"}
        send_nonce = uuid.uuid4().hex
        res = dict(job.result or {})
        res["phase"] = PHASE_AUTHORIZED
        res["send_nonce"] = send_nonce
        res["authorized_at"] = timezone.now().isoformat()
        job.result = res
        job.save(update_fields=["result"])

    _audit(AUDIT_SEND_AUTHORIZED, replication=replication, job=job, worker_id=wid)
    # The mutation instruction — released ONLY now, all server-derived. ``send_nonce`` binds the single
    # subsequent completion to THIS authorization.
    return {"ok": True, "job_id": job.id, "send_nonce": send_nonce,
            "bridge_call": "partial_close_position",
            "ticket": int(replication.follower_ticket),
            "volume": str(replication.target_volume),
            "expected_symbol": replication.follower_symbol,
            "identity": pin}


# ── terminal job bookkeeping (ADR-0056 F3) ────────────────────────────────────────────────────────────
# recon status -> (terminal job status, job phase). RECON_UNAVAILABLE stays RUNNING (the sweep re-reads);
# everything mapped here is terminal. NO status returns the job to PENDING or a re-send path.
_RECON_TO_JOB = {
    reconcile.PARTIAL_RECONCILED: (ExecutionJob.Status.SUCCESS, PHASE_RECONCILED),
    reconcile.PARTIAL_DIVERGENT_VOLUME: (ExecutionJob.Status.FAILED, PHASE_DIVERGENT),
    reconcile.PARTIAL_DIVERGENT_TICKET_GONE: (ExecutionJob.Status.FAILED, PHASE_DIVERGENT),
    reconcile.PARTIAL_OPERATOR_REVIEW: (ExecutionJob.Status.FAILED, PHASE_OPERATOR_REVIEW),
    reconcile.PARTIAL_DISPATCH_REFUSED: (ExecutionJob.Status.FAILED, PHASE_DISPATCH_REFUSED),
}


def _finalize_job_terminal(job, recon_status, *, now=None):
    """Apply TERMINAL execution-job bookkeeping from an authoritative reconciliation result (ADR-0056 F3), on
    an already row-locked ``job``. Reconciled -> SUCCESS; divergence / ticket-gone / operator-review -> FAILED;
    read-unavailable stays RUNNING (recon-pending) for the background sweep. The job NEVER returns to PENDING
    or a re-send path. Idempotent: a job already SUCCESS/FAILED is left untouched."""
    now = now or timezone.now()
    res = dict(job.result or {})
    if recon_status == reconcile.PARTIAL_RECONCILE_UNAVAILABLE:
        res["phase"] = PHASE_RECON_PENDING
        job.result = res
        job.save(update_fields=["result"])
        return job.status
    mapped = _RECON_TO_JOB.get(recon_status)
    if mapped is None:
        return job.status
    if job.status in (ExecutionJob.Status.SUCCESS, ExecutionJob.Status.FAILED):
        return job.status                              # already terminal — never re-transition
    new_status, phase = mapped
    res["phase"] = phase
    job.status = new_status
    job.finished_at = now
    job.result = res
    if new_status == ExecutionJob.Status.FAILED:
        job.error_message = f"partial_close_{recon_status}"[:500]
    job.save(update_fields=["status", "finished_at", "result", "error_message"])
    return new_status


def _finalize_reconcile(job_id, worker_identity, dispatch_token, recon_status, *, reason=None) -> dict:
    """Re-lock the job and apply terminal bookkeeping from a reconciliation outcome; emit the matching audit.
    Concurrency-safe: only the first caller transitions a RUNNING job; a job another process already finalized
    is left untouched. Also (re)writes the recon status for the operator-review short-circuits where no read
    ran (step / intended unavailable)."""
    now = timezone.now()
    with transaction.atomic():
        job = (ExecutionJob.objects.select_for_update()
               .filter(id=job_id, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE).first())
        if job is None:
            raise ClaimRefused("job_not_found")
        replication = _replication_for_job(job)
        if (recon_status == reconcile.PARTIAL_OPERATOR_REVIEW and replication is not None
                and replication.reconciliation_status not in reconcile.PARTIAL_TERMINAL_STATES):
            reconcile._set_recon(replication, reconcile.PARTIAL_OPERATOR_REVIEW)
        new_status = _finalize_job_terminal(job, recon_status, now=now)
    if recon_status == reconcile.PARTIAL_RECONCILED:
        _audit(AUDIT_RECONCILED, replication=replication, job=job,
               worker_id=getattr(worker_identity, "worker_id", None), status=recon_status)
    elif recon_status in (reconcile.PARTIAL_DIVERGENT_VOLUME, reconcile.PARTIAL_DIVERGENT_TICKET_GONE):
        _audit(AUDIT_DIVERGENCE, severity="WARN", replication=replication, job=job,
               worker_id=getattr(worker_identity, "worker_id", None), status=recon_status)
    elif recon_status == reconcile.PARTIAL_OPERATOR_REVIEW:
        _audit(AUDIT_OPERATOR_REVIEW, severity="WARN", replication=replication, job=job,
               worker_id=getattr(worker_identity, "worker_id", None), status=recon_status, reason=reason)
    out = {"ok": recon_status in (reconcile.PARTIAL_RECONCILED, reconcile.PARTIAL_DIVERGENT_VOLUME,
                                  reconcile.PARTIAL_DIVERGENT_TICKET_GONE),
           "status": recon_status, "job_status": new_status}
    if reason:
        out["reason"] = reason
    return out


def complete_partial_close(worker_identity, *, job_id: int, dispatch_token: str, bridge_result: dict,
                           send_nonce: str | None = None, fetch=None) -> dict:
    """Record the single bridge result for an AUTHORIZED partial close and drive the result state machine,
    authoritative reconciliation, and TERMINAL job bookkeeping (ADR-0056 F3). ONLY the claiming worker + its
    dispatch token may complete, and ONLY after ``authorize_send`` released a send authorization (phase
    AUTHORIZED). Idempotent + concurrency-safe: a duplicate completion makes no second transition and no second
    send; the single terminal transition is serialised by the COMPLETING phase + the in-lock recon state. No
    post-send state returns to PENDING or a retryable send."""
    assert_dedicated_worker(worker_identity)
    wid = getattr(worker_identity, "worker_id", None)
    now = timezone.now()
    with transaction.atomic():
        job = (ExecutionJob.objects.select_for_update()
               .filter(id=job_id, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE).first())
        if job is None:
            raise ClaimRefused("job_not_found")
        if not _completion_authorized(job, worker_identity, dispatch_token):
            raise ClaimRefused("completion_not_authorized")
        replication = _replication_for_job(job)
        if replication is None:
            raise ClaimRefused("replication_missing")
        res = dict(job.result or {})
        # Idempotent: a terminal reconciliation (or a job already driven terminal — incl. dispatch-refused)
        # ⇒ return it, make no second transition.
        if (replication.reconciliation_status in reconcile.PARTIAL_TERMINAL_STATES
                or job.status in (ExecutionJob.Status.SUCCESS, ExecutionJob.Status.FAILED)):
            return {"ok": True, "idempotent": True, "status": replication.reconciliation_status}
        # A completion REQUIRES a prior single-use send authorization.
        if res.get("phase") != PHASE_AUTHORIZED:
            if res.get("phase") in (PHASE_COMPLETING, PHASE_RECON_PENDING) \
                    or replication.reconciliation_status in (reconcile.PARTIAL_SENT,
                                                             reconcile.PARTIAL_OUTCOME_UNKNOWN,
                                                             reconcile.PARTIAL_RECONCILE_UNAVAILABLE):
                # A completion is already driving (or awaiting) reconciliation — serialise, no second transition.
                return {"ok": True, "in_progress": True, "status": replication.reconciliation_status}
            raise ClaimRefused("completion_requires_authorization")
        # Once a send was authorized, the single-use nonce is MANDATORY on completion (not merely checked when
        # volunteered): a dispatch-token holder cannot complete by omitting it.
        if res.get("send_nonce") and send_nonce != res.get("send_nonce"):
            raise ClaimRefused("send_nonce_mismatch")

        br = bridge_result or {}
        need_reconcile = bool(br.get("need_reconcile"))
        err = br.get("error")
        if not br.get("ok") and not need_reconcile and err in _PRESEND_REFUSAL_ERRORS:
            # A PROVABLY PRE-SEND bridge refusal (identity/demo/volume gate) — NO order_send was attempted, so
            # the position is untouched. Terminal dispatch-refused; no reconciliation read. A not-ok result
            # that is NOT an enumerated pre-send code (e.g. "exception", raisable AFTER the send) falls through
            # to the reconcile path below — we never record "nothing sent" unless we can prove it.
            reconcile._set_recon(replication, reconcile.PARTIAL_DISPATCH_REFUSED)
            res["phase"] = PHASE_DISPATCH_REFUSED
            job.result = res
            job.status = ExecutionJob.Status.FAILED
            job.finished_at = now
            job.error_message = f"partial_close_dispatch_refused:{_safe_error_code(err)}"[:500]
            job.save(update_fields=["result", "status", "finished_at", "error_message"])
            _audit(AUDIT_DISPATCH_REFUSED_EVT, severity="WARN", replication=replication, job=job,
                   worker_id=wid, reason=_safe_error_code(err))
            return {"ok": False, "status": reconcile.PARTIAL_DISPATCH_REFUSED, "reason": "dispatch_refused"}

        # A send WAS attempted ⇒ record the post-send state + claim the reconciliation transition (COMPLETING).
        post = (reconcile.PARTIAL_OUTCOME_UNKNOWN
                if br.get("error") == "order_send_none_unknown" else reconcile.PARTIAL_SENT)
        reconcile._set_recon(replication, post)
        res["phase"] = PHASE_COMPLETING
        job.result = res
        job.save(update_fields=["result"])
        # Capture SERVER-derived reconciliation inputs UNDER the lock — the worker supplies NO authority over
        # the comparison. Intended remaining is the producer-stamped payload value, never ``br``.
        acct = replication.follower.follower_account
        symbol = replication.follower_symbol
        job_intended_remaining = (job.payload or {}).get("expected_remaining")

    _audit(AUDIT_DISPATCH_ATTEMPTED, replication=replication, job=job, worker_id=wid, status=post)
    if post == reconcile.PARTIAL_OUTCOME_UNKNOWN:
        _audit(AUDIT_OUTCOME_UNKNOWN, severity="WARN", replication=replication, job=job, worker_id=wid)

    # Reconcile OUTSIDE the lock (authoritative read). The tolerance step comes from the follower's
    # AUTHORITATIVE broker instrument (never ``br``, never a default) and the intended remaining from the
    # server-stamped producer payload. Any worker-supplied ``volume_step`` / ``estimated_remaining`` is IGNORED.
    constraints = _broker_constraints(acct, symbol)
    if not constraints.authoritative or not constraints.lot_step or float(constraints.lot_step) <= 0:
        out = _finalize_reconcile(job_id, worker_identity, dispatch_token,
                                  reconcile.PARTIAL_OPERATOR_REVIEW, reason="authoritative_step_unavailable")
        return out
    if job_intended_remaining is None:
        out = _finalize_reconcile(job_id, worker_identity, dispatch_token,
                                  reconcile.PARTIAL_OPERATOR_REVIEW, reason="intended_remaining_unavailable")
        return out
    step = float(constraints.lot_step)
    rec = (reconcile.reconcile_partial_close(replication, job_intended_remaining, step=step, fetch=fetch)
           if fetch is not None
           else reconcile.reconcile_partial_close(replication, job_intended_remaining, step=step))
    out = _finalize_reconcile(job_id, worker_identity, dispatch_token, rec.get("status"))
    out["reconciliation"] = rec
    out["post_send_state"] = post
    return out


# ── background reconciliation sweep (ADR-0056 F4) ─────────────────────────────────────────────────────

def sweep_partial_close_reconcile(*, now=None, limit: int = 200, fetch=None) -> dict:
    """Recover partial-close jobs whose worker died after authorize-send / order_send and never completed.

    READ-ONLY: it re-reads the authoritative remaining volume through the identity firewall and finalizes the
    job. It NEVER calls ``order_send``, never re-dispatches, never requeues to PENDING — so no crash boundary
    can cause a second broker send. Concurrency-safe (``skip_locked`` per job) and idempotent. Uses the
    SERVER-derived ticket / intended remaining / authoritative broker ``volume_step`` (never a default).
    Escalates to operator review after ``_SWEEP_MAX_RECON_ATTEMPTS`` unavailable reads or when the broker step
    is non-authoritative. Inert while the copier is DARK (``copier_enabled()`` OFF).

    Gate choice (deliberate): it is gated on ``copier_enabled()`` only, NOT on the execute/arming flags — a
    job dispatched while armed must still be resolvable after the operator DISARMS (otherwise disarming would
    strand an in-flight job forever). This is safe because the sweep is strictly read-only and can never
    ``order_send``; it only drains/reconciles jobs that were already dispatched."""
    if not copier_enabled():
        return {"enabled": False, "scanned": 0, "finalized": 0}
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(seconds=_SWEEP_GRACE_SECONDS)
    candidate_ids = list(ExecutionJob.objects
                         .filter(status=ExecutionJob.Status.RUNNING,
                                 job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
                                 lease_expires_at__lt=cutoff)
                         .order_by("lease_expires_at").values_list("id", flat=True)[:limit])
    scanned = finalized = 0
    for cid in candidate_ids:
        # Phase 1 — claim + capture under a SHORT lock. NO bridge network read is held here (MEDIUM-1): the
        # authoritative read runs OUTSIDE the lock in phase 2, so a concurrent ``complete`` is never blocked
        # across the network. Only quick DB work (the broker-instrument step read) happens under the lock.
        read_plan = None
        with transaction.atomic():
            job = (ExecutionJob.objects.select_for_update(skip_locked=True)
                   .filter(id=cid, status=ExecutionJob.Status.RUNNING).first())
            if job is None:
                continue
            replication = _replication_for_job(job)
            if replication is None:
                continue
            recon = replication.reconciliation_status
            if recon in reconcile.PARTIAL_TERMINAL_STATES:
                # Replication already terminal but the job wasn't finalized (crash between the two) — close it.
                _finalize_job_terminal(job, recon, now=now)
                finalized += 1
                continue
            phase = (job.result or {}).get("phase")
            if phase == PHASE_DISPATCHED:
                # Claimed but a send was NEVER authorized (worker died before authorize-send) ⇒ nothing was
                # sent. Terminal dispatch-refused; NO authoritative read / NO re-dispatch.
                reconcile._set_recon(replication, reconcile.PARTIAL_DISPATCH_REFUSED)
                _finalize_job_terminal(job, reconcile.PARTIAL_DISPATCH_REFUSED, now=now)
                _audit(AUDIT_DISPATCH_REFUSED_EVT, severity="WARN", replication=replication, job=job,
                       worker_id=job.worker_id, reason="sweep_expired_before_send")
                finalized += 1
                continue
            acct = replication.follower.follower_account
            symbol = replication.follower_symbol
            intended = (job.payload or {}).get("expected_remaining")
            res = dict(job.result or {})
            attempts = int(res.get("recon_attempts", 0)) + 1
            res["recon_attempts"] = attempts
            job.result = res
            constraints = _broker_constraints(acct, symbol)
            if (not constraints.authoritative or not constraints.lot_step
                    or float(constraints.lot_step) <= 0 or intended is None
                    or attempts > _SWEEP_MAX_RECON_ATTEMPTS):
                reconcile._set_recon(replication, reconcile.PARTIAL_OPERATOR_REVIEW)
                _finalize_job_terminal(job, reconcile.PARTIAL_OPERATOR_REVIEW, now=now)
                _audit(AUDIT_OPERATOR_REVIEW, severity="WARN", replication=replication, job=job,
                       worker_id=job.worker_id, reason="sweep_unresolved")
                finalized += 1
                continue
            job.save(update_fields=["result"])         # persist the attempt before releasing the lock
            read_plan = (replication, intended, float(constraints.lot_step))
        if read_plan is None:
            continue

        # Phase 2 — authoritative read OUTSIDE the lock. reconcile_partial_close records the recon status.
        replication, intended, step = read_plan
        scanned += 1
        rec = (reconcile.reconcile_partial_close(replication, intended, step=step, fetch=fetch)
               if fetch is not None
               else reconcile.reconcile_partial_close(replication, intended, step=step))
        status = rec.get("status")
        if status == reconcile.PARTIAL_RECONCILE_UNAVAILABLE:
            continue                                   # stays RUNNING; attempt recorded; next sweep retries

        # Phase 3 — finalize under a brief re-lock. Idempotent: if a concurrent ``complete`` already drove the
        # job terminal, the status guard in _finalize_job_terminal leaves it untouched.
        with transaction.atomic():
            job2 = (ExecutionJob.objects.select_for_update()
                    .filter(id=cid, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE).first())
            if job2 is not None and job2.status == ExecutionJob.Status.RUNNING:
                _finalize_job_terminal(job2, status, now=now)
                finalized += 1
    return {"enabled": True, "scanned": scanned, "finalized": finalized}
