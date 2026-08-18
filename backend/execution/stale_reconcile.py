"""ADR-0048 — deterministic reconciliation of STALE PRE-ACTIVATION order jobs.

Context (AJ#7.2.1 / Node-2): when an execution node has no authorized order-capable worker, real
signals promote to ``SignalExecutionPlan`` (PROMOTED) and enqueue PLACE_ORDER ``ExecutionJob`` rows
that then sit ``PENDING`` forever — never claimed, never dispatched, zero fills. Those jobs must
NOT suddenly execute when Node-2 execution is later activated, and the PROMOTED plans they belong to
permanently consume the per-account exposure + concurrency budget (``account_exposure_exceeded`` on
every subsequent signal — see ``execution.risk_controls._active_signal_lots``, which is anchored to
``plan.status == PROMOTED``, NOT to job status).

This module fixes both, deterministically and reusing existing primitives (it invents no new state):

  1. Cancel each stale plan's still-PENDING PLACE_ORDER jobs → ``FAILED`` (the only viable terminal
     job state; SUCCESS would demand a CLOSED Trade that never exists). Uses
     ``select_for_update(skip_locked=True)`` + compare-and-set on ``status == PENDING`` so it can
     NEVER race a live worker claim, and touches ONLY jobs that were never claimed
     (``worker_id`` blank AND ``started_at`` is NULL). A RUNNING job (possibly already order_send'd —
     PLACE_ORDER is non-idempotent) is NEVER blind-failed here; such a plan is SKIPPED.
  2. Reuse ``close_monitor.resolve_completed_plans(account_id=...)`` to transition those plans
     PROMOTED → CLOSED (every leg now terminal-FAILED), which is what actually drops the legs from
     ``_active_signal_lots`` and releases the exposure + concurrency budget.

Safety contract:
  * DRY-RUN by default; ``apply=True`` mutates.
  * FAIL-CLOSED / account-scoped: Customer Zero (account 1) and account 18 are REFUSED outright.
  * Idempotent: a re-run finds no PENDING jobs (already FAILED) and no PROMOTED plans (already
    CLOSED), so it is a no-op.
  * Creates NO order, calls NO broker/bridge, sends NO signal, creates NO ExecutionJob.
  * Preserves forensic evidence on every cancelled job (``recovered=True`` +
    ``recovery_reason`` + ``error_message`` — the same durable trail the orphaned-PLACE_ORDER
    reconciler uses).
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

# Customer Zero (account 1) and account 18 are SACRED — this tool must never touch them.
PROTECTED_ACCOUNT_IDS = frozenset({1, 18})

# A plan younger than this may still be legitimately in-flight on a freshly-activated node; only
# demonstrably stale plans are eligible. Conservative default (30 min).
DEFAULT_OLDER_THAN_SECONDS = 1800

RECOVERY_REASON = "stale_preactivation_reconcile"
CANCEL_ERROR = (
    "cancelled: stale pre-activation order — node had no authorized order worker; "
    "never claimed, never dispatched (Node-2 execution-path gap)"
)


class ProtectedAccountError(ValueError):
    """Raised when the reconciler is pointed at Customer Zero or account 18."""


def reconcile_stale_preactivation_orders(
    *, account_id: int, older_than_seconds: int = DEFAULT_OLDER_THAN_SECONDS,
    limit: int = 500, apply: bool = False,
) -> dict:
    """Reconcile stale pre-activation PENDING order jobs for ONE account. Read-only unless ``apply``.

    Returns a deterministic, machine-readable report of exactly what was (or would be) reconciled.
    """
    if account_id in PROTECTED_ACCOUNT_IDS:
        raise ProtectedAccountError(
            f"account {account_id} is protected (Customer Zero / account 18) and is never reconciled")

    from execution.models import ExecutionJob, SignalExecutionPlan

    now = timezone.now()
    cutoff = now - timezone.timedelta(seconds=older_than_seconds)
    plans = list(
        SignalExecutionPlan.objects.filter(
            account_id=account_id,
            status=SignalExecutionPlan.Status.PROMOTED,
            created_at__lte=cutoff,
        ).order_by("id")[:limit]
    )

    report = {
        "account_id": account_id,
        "apply": apply,
        "older_than_seconds": older_than_seconds,
        "scanned_promoted_plans": len(plans),
        "candidates": [],
        "skipped": [],
        "jobs_cancelled": 0,
        "plans_closed": 0,
    }

    qualifying_plan_ids: list[int] = []
    for plan in plans:
        legs = list(plan.legs.select_related("execution_job").order_by("leg_index"))
        jobs = [leg.execution_job for leg in legs if leg.execution_job_id]
        if not legs or not jobs or len(jobs) != len(legs):
            report["skipped"].append({"plan_id": plan.id, "reason": "no_jobs_or_missing_leg_job"})
            continue
        # A leg that was ever claimed/dispatched (RUNNING) or filled (SUCCESS) makes this NOT a clean
        # pre-activation-stale plan — never blind-cancel a possibly-live/filled order.
        any_active = any(j.status in (ExecutionJob.Status.RUNNING, ExecutionJob.Status.SUCCESS) for j in jobs)
        # EVERY leg must be a never-claimed PENDING *PLACE_ORDER* job. The cancel path only fails
        # PLACE_ORDER jobs (below), so a plan with a non-PLACE_ORDER leg (e.g. a PLACE_ORDER_SHADOW
        # leg in the AUTO_SHADOW regime) must be SKIPPED — otherwise it would be listed as a
        # candidate, cancel zero jobs, and never close (its PROMOTED exposure would leak forever).
        all_unclaimed_pending_place_order = all(
            j.status == ExecutionJob.Status.PENDING
            and j.job_type == ExecutionJob.JobType.PLACE_ORDER
            and not (j.worker_id or "").strip()
            and j.started_at is None
            for j in jobs
        )
        if any_active or not all_unclaimed_pending_place_order:
            report["skipped"].append({
                "plan_id": plan.id,
                "reason": "active_or_partially_dispatched",
                "job_statuses": sorted({j.status for j in jobs}),
            })
            continue
        report["candidates"].append({
            "plan_id": plan.id,
            "symbol": plan.symbol,
            "source": plan.source,
            "created_at": plan.created_at.isoformat(),
            "age_seconds": int((now - plan.created_at).total_seconds()),
            "leg_count": len(legs),
            "job_ids": [j.id for j in jobs],
            "total_lot": str(plan.total_lot),
        })
        qualifying_plan_ids.append(plan.id)

    if not apply:
        return report

    # DEFENCE-IN-DEPTH (enforces the runbook's HARD ORDERING in code): never reconcile while the
    # account's node has a LIVE eligible claimant — a worker could be mid-claim, and reconciling then
    # could race an order that is being dispatched. The reconciler is meant to run BEFORE the node-2
    # worker is activated; if one is already live, refuse and do nothing (fail-closed).
    from trading.models import TradingAccount

    account = TradingAccount.objects.filter(id=account_id).select_related("terminal_node").first()
    node = getattr(account, "terminal_node", None) if account else None
    if node is not None:
        from execution.node_execution import eligible_order_claimant

        if eligible_order_claimant(node).ok:
            report["refused"] = "live_claimant_present"
            return report

    # ---- APPLY: cancel PENDING jobs → FAILED (never racing a live claim), then close the plans. ----
    for plan_id in qualifying_plan_ids:
        report["jobs_cancelled"] += _cancel_plan_pending_jobs(plan_id)

    # Reuse the existing, fail-closed close-monitor: with every leg now terminal-FAILED it transitions
    # each PROMOTED plan → CLOSED, which is what releases exposure + concurrency. Account-scoped so it
    # never touches a bystander account's plans. ALWAYS run on apply (even with 0 newly-qualified this
    # run) so a prior run that cancelled jobs but crashed before closing self-heals on re-run.
    from execution.close_monitor import resolve_completed_plans

    closed = resolve_completed_plans(account_id=account_id, limit=max(limit, len(qualifying_plan_ids) + 1))
    report["plans_closed"] = closed.get("closed", 0)
    report["close_detail"] = closed
    return report


def _cancel_plan_pending_jobs(plan_id: int) -> int:
    """Fail this plan's still-unclaimed PENDING PLACE_ORDER jobs (cancel-before-fill).

    Mirrors ``provider_commands_engine._cancel_pending_order_jobs`` — under
    ``select_for_update(skip_locked=True)`` filtering ``status == PENDING`` so it cannot race a
    worker claim mid-flight — and additionally records the durable forensic trail.
    """
    from execution.models import ExecutionJob, ProposedOrderLeg

    now = timezone.now()
    cancelled = 0
    with transaction.atomic():
        job_ids = list(
            ProposedOrderLeg.objects.filter(plan_id=plan_id)
            .exclude(execution_job__isnull=True)
            .values_list("execution_job_id", flat=True)
        )
        jobs = (
            ExecutionJob.objects.select_for_update(skip_locked=True).filter(
                id__in=job_ids,
                status=ExecutionJob.Status.PENDING,
                job_type=ExecutionJob.JobType.PLACE_ORDER,
            )
        )
        for j in jobs:
            j.status = ExecutionJob.Status.FAILED
            j.error_message = CANCEL_ERROR
            j.finished_at = now
            j.recovered = True
            j.recovery_reason = RECOVERY_REASON
            j.save(update_fields=["status", "error_message", "finished_at", "recovered", "recovery_reason"])
            cancelled += 1
    return cancelled
