"""RX-2E — Execution Supervisor (detection only).

Detects orphaned RUNNING ExecutionJobs using the mandatory lease_expires_at.
Phase 1: sets ComponentHealth + returns orphans for alerting + recommendation.
NEVER force-fails a job automatically.
"""
from django.utils import timezone

from execution.models import ExecutionJob

from ..constants import Component, HealthStatus, job_category
from . import health_store, telemetry_freshness


def find_orphaned_jobs():
    """RUNNING jobs whose lease has expired (or that have no lease at all)."""
    now = timezone.now()
    running = ExecutionJob.objects.filter(status=ExecutionJob.Status.RUNNING)
    orphans = []
    for j in running:
        lease = j.lease_expires_at
        if lease is None or lease < now:
            orphans.append(j)
    return orphans


def evaluate():
    """Set EXECUTION_PIPELINE health with severity calibrated by job category;
    return orphan jobs for alerting. (RX-2E severity calibration.)

    - Stale trade-execution job  -> FAILED (critical -> DOWN, blocks trading).
    - Stale sync/validation/unknown -> DEGRADED (does NOT block can_trade).
    """
    orphans = find_orphaned_jobs()
    by_cat = {"trade_exec": [], "sync": [], "validation": [], "unknown": []}
    for j in orphans:
        by_cat[job_category(j.job_type)].append(j.id)

    if by_cat["trade_exec"]:
        status = HealthStatus.FAILED          # critical: trading is impaired
    elif orphans:
        status = HealthStatus.DEGRADED        # sync/validation/unknown: non-blocking
    else:
        status = HealthStatus.OK

    detail = {
        "orphaned_running_jobs": [j.id for j in orphans],
        "count": len(orphans),
        "by_category": {k: v for k, v in by_cat.items() if v},
        "telemetry": telemetry_freshness.latest_ages(),  # RX-2D data-flow ages
    }
    health_store.upsert(Component.EXECUTION_PIPELINE, status, detail=detail)
    return orphans
