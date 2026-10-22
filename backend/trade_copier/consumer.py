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
import uuid

from django.db import transaction
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import reconcile
from trade_copier.flags import copier_enabled, copier_execute_demo_armed, copier_execute_enabled
from trade_copier.models import CopierReplication, ReplicationStatus
from trade_copier.replication import _broker_constraints

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 120
# A generic-mutation grant that must NOT co-exist with partial_close_worker (mutually-exclusive classes).
_GENERIC_MUTATION_PERMS = ("shadow_worker",)


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
    """Claim (at most) one PARTIAL_CLOSE_TRADE for the dedicated worker. The worker supplies NO routing
    authority — ``account_id`` is the worker's own authorized account and ``authorized_node_hostnames`` its
    authorized node(s); every fact is re-derived from the job + replication + account binding under the lock.
    Durable RUNNING + lease before the job leaves the server (no double-send)."""
    assert_dedicated_worker(worker_identity)
    if not _flags_ok():
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
            return {"ok": False, "reason": reason}

        acct = replication.follower.follower_account
        pin = derive_identity_pin(acct)
        if pin is None:
            return {"ok": False, "reason": "identity_pin_unresolved"}

        dispatch_token = uuid.uuid4().hex
        job.status = ExecutionJob.Status.RUNNING
        job.worker_id = worker_identity.worker_id
        job.lease_expires_at = timezone.now() + timezone.timedelta(seconds=_LEASE_SECONDS)
        # Persist that the job entered the mutation-dispatch lifecycle (never re-claimable on a lost response).
        job.result = {"dispatch_token": dispatch_token, "worker_id": worker_identity.worker_id,
                      "phase": "dispatched"}
        job.save(update_fields=["status", "worker_id", "lease_expires_at", "result"])
        replication.reconciliation_status = reconcile.PARTIAL_RECON_PENDING
        replication.save(update_fields=["reconciliation_status", "updated_at"])

    return {"ok": True, "job_id": job.id, "dispatch_token": dispatch_token,
            # the ONLY instruction the worker may act on — all server-derived:
            "bridge_call": "partial_close_position",
            "ticket": int(replication.follower_ticket),
            "volume": str(replication.target_volume),
            "expected_symbol": replication.follower_symbol,
            "identity": pin,
            "replication_id": replication.id}


def _worker_lease_ok(job, worker_identity, dispatch_token) -> bool:
    res = job.result or {}
    return (job.worker_id == worker_identity.worker_id
            and res.get("dispatch_token") == dispatch_token
            and res.get("dispatch_token") is not None)


def complete_partial_close(worker_identity, *, job_id: int, dispatch_token: str, bridge_result: dict,
                           fetch=None) -> dict:
    """Record a bridge result for a claimed partial close and drive the result state machine + authoritative
    reconciliation. ONLY the claiming worker + its dispatch token, for the same job, may complete it;
    idempotent (a repeat completion makes no second transition/send). No post-send state returns to a
    retryable PENDING; a lost ACK / DONE / DONE_PARTIAL / reject all progress to reconciliation."""
    assert_dedicated_worker(worker_identity)
    with transaction.atomic():
        job = (ExecutionJob.objects.select_for_update()
               .filter(id=job_id, job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE).first())
        if job is None:
            raise ClaimRefused("job_not_found")
        if not _worker_lease_ok(job, worker_identity, dispatch_token):
            raise ClaimRefused("completion_not_authorized")
        replication = _replication_for_job(job)
        if replication is None:
            raise ClaimRefused("replication_missing")
        # Idempotent: a terminal reconciliation already recorded ⇒ return it, make no second transition.
        if replication.reconciliation_status in reconcile.PARTIAL_TERMINAL_STATES:
            return {"ok": True, "idempotent": True, "status": replication.reconciliation_status}

        br = bridge_result or {}
        # Classify the bridge return into the post-send state machine. NONE goes back to PENDING.
        if br.get("error") == "order_send_none_unknown" or br.get("need_reconcile") and not br.get("ok"):
            post = reconcile.PARTIAL_OUTCOME_UNKNOWN if br.get("error") == "order_send_none_unknown" \
                else reconcile.PARTIAL_SENT
        elif br.get("mutated") or br.get("ok"):
            post = reconcile.PARTIAL_SENT
        else:
            # A reject still requires an authoritative read before any terminal decision.
            post = reconcile.PARTIAL_SENT
        replication.reconciliation_status = post
        replication.save(update_fields=["reconciliation_status", "updated_at"])
        # Capture the SERVER-derived reconciliation inputs UNDER the lock — the worker supplies NO authority
        # over the comparison. Intended remaining is the producer-stamped payload value, never ``br``.
        acct = replication.follower.follower_account
        symbol = replication.follower_symbol
        job_intended_remaining = (job.payload or {}).get("expected_remaining")

    # Reconcile OUTSIDE the lock (authoritative read); the terminal state is written by reconcile_*. The
    # tolerance step comes from the follower's AUTHORITATIVE broker instrument (never ``br``, never a default)
    # and the intended remaining from the server-stamped producer payload. Any worker-supplied
    # ``volume_step`` / ``estimated_remaining`` in ``bridge_result`` is deliberately IGNORED.
    constraints = _broker_constraints(acct, symbol)
    if not constraints.authoritative or not constraints.lot_step or float(constraints.lot_step) <= 0:
        reconcile._set_recon(replication, reconcile.PARTIAL_OPERATOR_REVIEW)
        return {"ok": False, "status": reconcile.PARTIAL_OPERATOR_REVIEW,
                "reason": "authoritative_step_unavailable"}
    if job_intended_remaining is None:
        reconcile._set_recon(replication, reconcile.PARTIAL_OPERATOR_REVIEW)
        return {"ok": False, "status": reconcile.PARTIAL_OPERATOR_REVIEW,
                "reason": "intended_remaining_unavailable"}
    step = float(constraints.lot_step)
    rec = (reconcile.reconcile_partial_close(replication, job_intended_remaining, step=step, fetch=fetch)
           if fetch is not None
           else reconcile.reconcile_partial_close(replication, job_intended_remaining, step=step))
    return {"ok": True, "post_send_state": post, "reconciliation": rec}
