"""trade_copier.execution_driver — ADR-0117 governed, supervised copier EXECUTION DRIVER (fallback + reconciliation).

The ADR-0094 immediate-dispatch inline path (fired from the signed ingestion endpoint when armed) remains the
<=1 s PRIMARY. This module is the bounded, single-flight, crash-safe FALLBACK + reconciliation executor that drives
the EXISTING, individually-gated stages for work the immediate path missed (a dropped push, a backlog, a restart):

    replicate -> execute OPEN -> reconcile -> execute MODIFY -> execute FULL_CLOSE -> mutation-dispatch -> lifecycle-dispatch

GOVERNANCE (fail-closed, DARK-by-default):
  * The command no-ops unless ``COPIER_EXECUTION_DRIVER_ENABLED`` (ANDed with the master gate). Each stage ALSO
    re-reads its OWN gate + the full per-pair live admission/arm, so with the execution flags / authorization / arm
    OFF the driver produces nothing (observation/shadow only) — it never ORs past a gate.
  * SINGLE active driver: a file lease (``recovery_lease``, ``KIND_COPIER_DRIVER``, on its OWN lease file, so it is
    independent of the deploy/recovery lease). A second driver fails closed; an expired lease self-reclaims.
  * Idempotent + resilient: each stage keeps its existing per-(master,ticket,revision) + ExecutionJob idempotency
    and single-flight ``select_for_update`` claim; one stage's error is captured and surfaced, never sinking the
    pass.
  * Dead-letter / alert state is the EXISTING durable record (``CopierReplication.EXECUTION_FAILED`` + the governed
    copier alert channel). The driver SURFACES the counts; it recovers no trade on its own.
  * Reconcile runs BEFORE the MODIFY/FULL_CLOSE producers so the verified ``follower_ticket`` exists; a lapsed
    authorization window halts new OPENs (the OPEN producer's own gate) while MODIFY/FULL_CLOSE containment stays
    available for an already-bound position (ADR-0083/0115) — so a window lapse can never strand a follower.
  * Holds/reads NO secret; the status projection exposes none.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("guvfx.trade_copier.execution_driver")


def default_stages():
    """The ordered fallback pipeline: ``(name, callable(limit)->summary, gate_predicate)``. Imports are local so the
    module stays import-light and a test can inject its own stage list."""
    from trade_copier import flags as cf
    from trade_copier.execution_producer import (produce_pending, produce_pending_closes,
                                                  produce_pending_modifies)
    from trade_copier.lifecycle_dispatch import run_lifecycle_dispatch_pass
    from trade_copier.mutation_dispatch import run_mutation_dispatch_pass
    from trade_copier.mutation_transport import mutation_transport
    from trade_copier.reconcile import run_reconcile_pass
    from trade_copier.replication import replicate_pending
    return [
        ("replicate",         lambda limit: replicate_pending(limit=limit),                   cf.copier_replication_enabled),
        ("execute_open",      lambda limit: produce_pending(limit=limit),                     cf.copier_execute_enabled),
        ("reconcile",         lambda limit: run_reconcile_pass(limit=limit),                  cf.copier_reconcile_enabled),
        ("execute_modify",    lambda limit: produce_pending_modifies(limit=limit),            cf.copier_execute_enabled),
        ("execute_close",     lambda limit: produce_pending_closes(limit=limit),              cf.copier_execute_enabled),
        ("mutation_dispatch", lambda limit: run_mutation_dispatch_pass(transport=mutation_transport(), limit=limit),
                                                                                              cf.copier_mutation_dispatch_enabled),
        ("lifecycle_dispatch", lambda limit: run_lifecycle_dispatch_pass(limit=limit),        cf.copier_lifecycle_dispatch_enabled),
    ]


def run_driver_pass(*, limit: int = 200, stages=None) -> dict:
    """Run ONE bounded fallback pass: each gated stage in order, resilient (one stage's error is captured, never
    sinks the pass). Secret-free per-stage summary. The caller must hold the driver lease + have checked the driver
    gate; each stage still re-reads its OWN gate, so a gate-off stage is a no-op (never ORs past it)."""
    results = []
    for name, fn, gate in (stages if stages is not None else default_stages()):
        try:
            if gate is not None and not gate():
                results.append({"stage": name, "skipped": "gate_off"})
                continue
            results.append({"stage": name, "summary": str(fn(limit))})
        except Exception as e:  # noqa: BLE001 — bounded + resilient; surface, never sink the pass
            logger.exception("copier execution-driver stage %s failed: %s", name, e)
            results.append({"stage": name, "error": type(e).__name__})
    return {"stages": results}


def dead_letter_summary() -> dict:
    """Non-secret counts of copier work needing operator attention — the EXISTING durable dead-letter state (failed
    executions + failed/in-flight jobs). The driver SURFACES these; it never auto-recovers a trade. Fail-soft."""
    out: dict = {}
    try:
        from trade_copier.models import CopierReplication, ReplicationStatus
        out["replications_failed"] = CopierReplication.objects.filter(
            status=ReplicationStatus.EXECUTION_FAILED).count()
        out["replications_requested"] = CopierReplication.objects.filter(
            status=ReplicationStatus.EXECUTION_REQUESTED).count()
    except Exception:  # noqa: BLE001
        out["replications_failed"] = None
    try:
        from execution.models import ExecutionJob
        out["jobs_failed"] = ExecutionJob.objects.filter(
            job_type__in=["PLACE_ORDER", "MODIFY_POSITION", "CLOSE_TRADE", "PARTIAL_CLOSE_TRADE"],
            status="FAILED").count()
    except Exception:  # noqa: BLE001
        out["jobs_failed"] = None
    return out


def driver_status(*, lease_path=None, now=None) -> dict:
    """READ-ONLY readiness/status projection (no secret, no mutation): the driver gate, the lease holder, the live
    authorization/arm counts and the dead-letter counts. Safe to call any time."""
    from django.utils import timezone
    now = now or timezone.now()
    from trade_copier.flags import copier_execution_driver_enabled, execution_banner
    out: dict = {"driver_enabled": copier_execution_driver_enabled(),
                 "execution_banner": execution_banner(), "checked_at": now.isoformat()}
    try:
        from hosted_workspace.recovery_lease import read_lease
        status, lease = read_lease(now, lease_path)
        out["lease"] = {"status": getattr(status, "value", str(status)),
                        "kind": getattr(lease, "kind", None),
                        "expires_at": getattr(lease, "expires_at", None)}
    except Exception as e:  # noqa: BLE001
        out["lease"] = {"status": "error", "reason": type(e).__name__}
    try:
        from trade_copier.models import (CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization,
                                          CopierLiveAuthorizationStatus)
        out["active_authorizations"] = CopierLiveAuthorization.objects.filter(
            status=CopierLiveAuthorizationStatus.ACTIVE, revoked_at__isnull=True).count()
        out["engaged_arms"] = CopierLiveArm.objects.filter(state=CopierLiveArmState.ENGAGED).count()
    except Exception:  # noqa: BLE001
        out["active_authorizations"] = out["engaged_arms"] = None
    out["dead_letter"] = dead_letter_summary()
    return out
