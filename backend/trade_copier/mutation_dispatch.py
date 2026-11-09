"""trade_copier.mutation_dispatch — DARK guarded MODIFY / FULL-CLOSE lifecycle dispatch (ADR-0086).

The missing consumer for the typed ``MODIFY_POSITION`` / ``CLOSE_TRADE`` ExecutionJobs the producer already
creates (ADR-0083 ``create_live_modify_job`` / ``create_live_full_close_job``). For each such job it:

 1. CLAIMS it single-flight (``PENDING`` → ``RUNNING`` under a row lock) so a job is sent at most once;
 2. re-checks the GOVERNING live authorisation at send time (gate parity with the producer — a revoke / expiry /
    kill-switch committed after the job was minted fails closed here too);
 3. derives the per-ticket revision WATERMARK (migration-free — the max applied master sequence, see
    ``follower_mutation_hwm``) and stamps it onto the command so the bridge guard can reject a stale revision;
 4. SENDS the typed command (the job payload = ADR-0083 ``_live_command_envelope``) through an INJECTABLE
    ``transport``; and
 5. records the bridge outcome onto the EXISTING canonical records via
    ``lifecycle_outcome_record.record_lifecycle_outcome`` (reconciliation_status + governed operator alert).

It is DARK and fail-closed:

* Gated by ``copier_mutation_dispatch_enabled`` (default OFF, ANDed with the LIVE-execute arm stack).
* The default ``transport`` is INERT — it performs NO network call and contacts NO broker, returning a
  ``mutation_transport_disabled`` result; the job is then reverted to ``PENDING`` (nothing was sent). A real
  per-tenant transport is a SEPARATE provisioning/arming step, so this module cannot reach a broker on its own.
* It OPENS no position, creates NO ExecutionJob, and never recovers a late entry — it only sends an existing
  MODIFY/CLOSE on a bound follower ticket, or fails closed. An UNKNOWN (indeterminate) outcome leaves the job
  ``RUNNING`` for the authoritative reconciler; it is NEVER inferred as success.

PARTIAL_CLOSE is intentionally EXCLUDED — it keeps its dedicated least-privilege worker path (ADR-0056).
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Inert default transport result — NO network, NO broker contact, NO lifecycle_outcome (recorder no-op).
INERT_TRANSPORT_RESULT = {"ok": False, "error": "mutation_transport_disabled", "need_reconcile": False}

# ExecutionJob.JobType -> the guard operation vocabulary (PARTIAL_CLOSE excluded: dedicated worker path).
_JOBTYPE_TO_OP = {"MODIFY_POSITION": "MODIFY_PROTECTION", "CLOSE_TRADE": "FULL_CLOSE"}
# The requested_op values a lifecycle MODIFY/CLOSE replication carries (for the watermark scan).
_MUTATION_REQUESTED_OPS = ("MODIFY_SLTP", "CLOSE")

_SUCCESS_OUTCOMES = ("APPLIED", "ALREADY_APPLIED")
# Terminal-failure guard outcomes (job -> FAILED). UNKNOWN_OUTCOME is NOT terminal (leave RUNNING to reconcile).
_TERMINAL_FAILURES = ("PRE_STATE_MISMATCH", "DEADLINE_EXCEEDED", "STALE_REVISION", "POSITION_NOT_FOUND",
                      "BROKER_REJECTED", "CONTAINED_FAILURE")


def _inert_transport(command: dict) -> dict:
    """The default transport: contacts NO broker. A real per-tenant push transport is a separate arming step."""
    return dict(INERT_TRANSPORT_RESULT)


def follower_mutation_hwm(follower, ticket) -> int:
    """The per-ticket revision WATERMARK, derived WITHOUT a migration: the highest master-event sequence whose
    follower MUTATION has been applied (an ``EXECUTION_VERIFIED`` MODIFY/CLOSE replication for this exact follower
    ticket). The bridge guard rejects a command whose ``master_revision`` is <= this (an out-of-order mutation).
    Returns -1 when no applied mutation exists yet (a first mutation is never falsely rejected). Read-only."""
    from trade_copier.models import CopierReplication, ReplicationStatus
    try:
        rows = (CopierReplication.objects
                .filter(follower=follower, follower_ticket=ticket,
                        requested_op__in=list(_MUTATION_REQUESTED_OPS),
                        status=ReplicationStatus.EXECUTION_VERIFIED)
                .select_related("master_event").values_list("master_event__sequence", flat=True))
        seqs = [int(s) for s in rows if s is not None]
        return max(seqs) if seqs else -1
    except Exception:  # noqa: BLE001 — a watermark read must never raise into the dispatch; fail to -1 (no reject)
        logger.exception("copier mutation watermark read failed follower=%s ticket=%s",
                         getattr(follower, "id", "?"), ticket)
        return -1


def _build_command(job, replication) -> dict:
    """The typed guard command for the bridge = the job payload (ADR-0083 envelope) + the per-ticket watermark.
    Secret-free (the payload carries ids / reason codes / the server-derived identity pin, no credential)."""
    command = dict(job.payload or {})
    command["last_applied_revision"] = follower_mutation_hwm(replication.follower, replication.follower_ticket)
    return command


def _governing_authority_ok(replication, op, *, now) -> tuple[bool, str]:
    """Gate parity with the producer: re-resolve + lock the GOVERNING live authorisation (the one this position
    was opened under) and re-apply the window / kill-switch / authority policy, so a revoke/expiry/kill-switch
    committed after the job was minted fails closed at send time too. ``(ok, reason)``."""
    try:
        from trade_copier import live_execution as le
        authz, reason = le._relock_live_authz_for_lifecycle(replication, op, now=now)
        return (authz is not None), reason
    except Exception:  # noqa: BLE001 — a gate error fails CLOSED (never send on an unverifiable authority)
        logger.exception("copier mutation gate re-check failed replication=%s", getattr(replication, "id", "?"))
        return False, "gate_recheck_error"


def dispatch_one(job_id: int, *, transport=_inert_transport, gate_check=_governing_authority_ok) -> dict:
    """Dispatch exactly one MODIFY/CLOSE job: claim single-flight, re-check authority, send, record outcome.

    Returns a small secret-free summary. Idempotent: a job already RUNNING/terminal makes no second send. The
    broker call is attempted at most once; an UNKNOWN result leaves the job RUNNING for the authoritative
    reconciler and is NEVER inferred as success. ``gate_check(replication, op, now)`` is the injectable
    producer-parity authority re-check (default: the live-authorisation relock)."""
    from execution.models import ExecutionJob
    from trade_copier.models import CopierReplication, ReplicationStatus
    from trade_copier import live_execution as le

    now = timezone.now()
    with transaction.atomic():
        job = (ExecutionJob.objects.select_for_update()
               .filter(id=job_id, job_type__in=list(_JOBTYPE_TO_OP.keys())).first())
        if job is None:
            return {"dispatched": False, "reason": "job_not_found"}
        if job.status != ExecutionJob.Status.PENDING:
            return {"dispatched": False, "reason": "not_pending", "status": job.status}
        op_guard = _JOBTYPE_TO_OP[job.job_type]
        op_le = le.OP_MODIFY if job.job_type == "MODIFY_POSITION" else le.OP_FULL_CLOSE
        replication = (CopierReplication.objects.select_for_update()
                       .filter(execution_job_id=job.id).select_related("follower", "master_event").first())
        if replication is None:
            return {"dispatched": False, "reason": "replication_missing"}
        if replication.follower_ticket is None:
            return {"dispatched": False, "reason": "no_follower_ticket"}
        # Gate parity — refuse (leave PENDING) if the governing authority is no longer valid.
        ok, reason = gate_check(replication, op_le, now=now)
        if not ok:
            return {"dispatched": False, "reason": reason, "gate": "refused"}
        command = _build_command(job, replication)
        # Claim single-flight.
        job.status = ExecutionJob.Status.RUNNING
        job.save(update_fields=["status"])

    # SEND outside the lock — at most once.
    try:
        bridge_result = transport(command)
    except Exception as e:  # noqa: BLE001 — a transport exception is an indeterminate send (never success)
        logger.warning("copier mutation dispatch transport error job=%s: %s", job_id, type(e).__name__)
        bridge_result = {"ok": False, "error": "transport_exception", "need_reconcile": True,
                         "lifecycle_outcome": "UNKNOWN_OUTCOME", "lifecycle_reason": "transport_exception"}

    br = bridge_result or {}
    # The INERT transport (or any transport that did NOT send) -> revert to PENDING; record nothing.
    if br.get("error") == "mutation_transport_disabled":
        with transaction.atomic():
            j = ExecutionJob.objects.select_for_update().filter(id=job_id).first()
            if j is not None and j.status == ExecutionJob.Status.RUNNING:
                j.status = ExecutionJob.Status.PENDING
                j.save(update_fields=["status"])
        return {"dispatched": False, "reason": "transport_disabled"}

    # Record the outcome onto the canonical records (reconciliation_status + governed alert).
    from trade_copier.lifecycle_outcome_record import record_lifecycle_outcome
    rec = record_lifecycle_outcome(replication, br, op=op_guard, set_reconciliation=True)
    outcome = str(br.get("lifecycle_outcome") or "").strip()

    # Finalise the job + replication status from the authoritative outcome.
    with transaction.atomic():
        j = ExecutionJob.objects.select_for_update().filter(id=job_id).first()
        rep = (CopierReplication.objects.select_for_update().filter(execution_job_id=job_id).first())
        if j is None:
            return {"dispatched": True, "reason": "job_vanished", "outcome": outcome}
        if outcome in _SUCCESS_OUTCOMES:
            j.status, rep_status = ExecutionJob.Status.SUCCESS, ReplicationStatus.EXECUTION_VERIFIED
            j.finished_at = timezone.now()
        elif outcome in _TERMINAL_FAILURES:
            j.status, rep_status = ExecutionJob.Status.FAILED, ReplicationStatus.EXECUTION_FAILED
            j.finished_at = timezone.now()
            j.error_message = f"copier_mutation_{outcome}"[:500]
        else:
            # UNKNOWN_OUTCOME / unverifiable — leave RUNNING for the authoritative reconciler (never success).
            j.status, rep_status = ExecutionJob.Status.RUNNING, None
        fields = ["status", "finished_at", "error_message"] if rep_status else ["status"]
        j.save(update_fields=[f for f in fields if hasattr(j, f)])
        if rep is not None and rep_status is not None:
            rep.status = rep_status
            rep.save(update_fields=["status", "updated_at"])
    return {"dispatched": True, "outcome": outcome or "UNKNOWN_OUTCOME", "recorded": rec.get("recorded", False),
            "alerted": rec.get("alerted", False), "job_status": j.status}


def run_mutation_dispatch_pass(*, transport=_inert_transport, gate_check=_governing_authority_ok,
                               limit: int = 200) -> dict:
    """One DARK MODIFY/CLOSE dispatch pass. Inert unless ``copier_mutation_dispatch_enabled`` (default OFF, ANDed
    with the LIVE-execute arm stack). With the default inert transport it contacts NO broker. Returns a
    secret-free summary."""
    from trade_copier.flags import copier_mutation_dispatch_enabled
    if not copier_mutation_dispatch_enabled():
        return {"enabled": False, "scanned": 0, "dispatched": 0}

    from execution.models import ExecutionJob
    job_ids = list(ExecutionJob.objects
                   .filter(job_type__in=list(_JOBTYPE_TO_OP.keys()), status=ExecutionJob.Status.PENDING)
                   .order_by("id").values_list("id", flat=True)[:limit])
    scanned = 0
    dispatched = 0
    for jid in job_ids:
        scanned += 1
        try:
            out = dispatch_one(jid, transport=transport, gate_check=gate_check)
            if out.get("dispatched"):
                dispatched += 1
        except Exception as e:  # noqa: BLE001 — one job's error must not sink the pass
            logger.exception("copier mutation dispatch failed job=%s: %s", jid, e)
    return {"enabled": True, "scanned": scanned, "dispatched": dispatched}
