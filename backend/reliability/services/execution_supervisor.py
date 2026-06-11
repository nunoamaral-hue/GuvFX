"""RX-2E — Execution Supervisor (detection only).

Detects orphaned RUNNING ExecutionJobs using the mandatory lease_expires_at.
Phase 1: sets ComponentHealth + returns orphans for alerting + recommendation.
NEVER force-fails a job automatically.
"""
from django.utils import timezone

from execution.models import ExecutionJob

from ..constants import Component, HealthStatus
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
    """Set EXECUTION_PIPELINE health; return list of orphan jobs for alerting."""
    orphans = find_orphaned_jobs()
    detail = {
        "orphaned_running_jobs": [j.id for j in orphans],
        "count": len(orphans),
        "telemetry": telemetry_freshness.latest_ages(),  # RX-2D data-flow ages
    }
    status = HealthStatus.DEGRADED if orphans else HealthStatus.OK
    health_store.upsert(Component.EXECUTION_PIPELINE, status, detail=detail)
    return orphans
