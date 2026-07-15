"""WS-C EXECUTION HEALTH — always-on monitor-chain reliability sweep.

The reliability supervisor (`reliability_tick`) is dormant in prod (`RELIABILITY_CORE_ENABLED=false`)
and, even when on, only inspects RUNNING jobs. Two silent execution defects therefore go unflagged
and unrepaired. This sweep runs inside the per-minute monitor chain (always on, no reliability-core
dependency) and closes both:

  (1) RECLAIM orphaned SYNC — a worker recycle (deploy/restart) orphans any in-flight
      ``SYNC_POSITIONS`` job (stuck RUNNING, lease expired, never completed). SYNC is idempotent and
      auto-re-created, so a lease-EXPIRED orphan is dead weight that drags the pipeline to DEGRADED.
      Fail it (safe; only SYNC, only lease-expired).

  (2) DETECT stuck PENDING order (the single most dangerous silent path, R1) — an order-opening job
      (``PLACE_ORDER``/``PLACE_TEST_ORDER``) that never gets claimed (e.g. node-routing mismatch)
      sits PENDING forever: the plan is PROMOTED, the promotion audit says "success", yet NO order is
      placed and nothing flags it (the supervisor inspects only RUNNING). Raise ONE deduped alert per
      stuck job. ALERT-ONLY — never fail a PENDING order from here (it would race a live worker claim).

HARD BOUNDARY: never creates an order, never modifies a Trade, never touches PLACE_ORDER job state.
It only (a) fails dead lease-expired SYNC rows and (b) raises alerts. Idempotent; safe every minute.
Env: ``EXECUTION_HEALTH_ENABLED`` (default TRUE) — set false to disable the whole sweep.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.utils import timezone

from execution.models import ExecutionJob

logger = logging.getLogger("guvfx.execution.health")

# A PLACE_ORDER older than this that is STILL PENDING was never claimed (a worker claims within
# seconds). Well past the 120s signal-staleness window so a normal in-flight order never trips it.
STUCK_PENDING_ORDER_SECONDS = int(os.getenv("STUCK_PENDING_ORDER_SECONDS", "300") or 300)
_ORDER_TYPES = (ExecutionJob.JobType.PLACE_ORDER, ExecutionJob.JobType.PLACE_TEST_ORDER)


def execution_health_enabled() -> bool:
    return os.getenv("EXECUTION_HEALTH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def reclaim_orphaned_sync_jobs(now) -> int:
    """Fail dead (lease-expired) RUNNING SYNC_POSITIONS jobs. Returns the count reclaimed."""
    orphans = ExecutionJob.objects.filter(
        status=ExecutionJob.Status.RUNNING,
        job_type=ExecutionJob.JobType.SYNC_POSITIONS,
        lease_expires_at__lt=now,
    )
    ids = list(orphans.values_list("id", flat=True))
    if not ids:
        return 0
    n = ExecutionJob.objects.filter(id__in=ids, status=ExecutionJob.Status.RUNNING).update(
        status=ExecutionJob.Status.FAILED, finished_at=now, recovered=True,
        recovery_reason="orphaned SYNC (lease expired, worker recycled) — monitor-chain reclaim",
        error_message="orphaned: lease expired, worker gone",
    )
    if n:
        logger.info("execution_health: reclaimed %s orphaned SYNC jobs %s", n, ids[:20])
    return n


def _alert_stuck_order(job) -> None:
    """One deduped WARN per stuck-PENDING order-opening job. Best-effort."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"stuck_pending_order:job:{job.id}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        payload = job.payload or {}
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.WARN,
            component=Component.EXECUTION_PIPELINE,
            trading_account_id=job.account_id,
            title=f"Order never placed — job #{job.id} stuck PENDING",
            body=(f"{job.job_type} job #{job.id} ({payload.get('signal_source', '?')} "
                  f"{payload.get('symbol', '?')}) has been PENDING with no worker claim — the plan "
                  f"is promoted but NO order was placed. Likely a node-routing mismatch "
                  f"(terminal_node_id={job.terminal_node_id}). Check worker claim filters."),
            dedup_key=dedup_key,
            status=AlertEvent.Status.OPEN,
            detail={"job_id": job.id, "job_type": job.job_type,
                    "terminal_node_id": job.terminal_node_id,
                    "signal_source": payload.get("signal_source"), "symbol": payload.get("symbol")},
        )
        logger.error("execution_health: STUCK-PENDING order alert job=%s node=%s",
                     job.id, job.terminal_node_id)
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("execution_health: failed to alert stuck order job=%s", job.id)


def detect_stuck_pending_orders(now) -> int:
    """Raise a deduped alert for each order-opening job stuck PENDING past the threshold. Returns
    the count alerted. ALERT-ONLY — never mutates the job (a live worker may still claim it)."""
    cutoff = now - timedelta(seconds=STUCK_PENDING_ORDER_SECONDS)
    stuck = ExecutionJob.objects.filter(
        status=ExecutionJob.Status.PENDING, job_type__in=_ORDER_TYPES, created_at__lt=cutoff,
    ).order_by("id")[:500]
    n = 0
    for job in stuck:
        _alert_stuck_order(job)
        n += 1
    return n


def sweep_execution_health(*, limit: int = 500) -> dict:
    """One pass: reclaim dead SYNC orphans + alert on stuck-PENDING orders. Returns a counts dict."""
    if not execution_health_enabled():
        return {"enabled": False}
    now = timezone.now()
    return {
        "enabled": True,
        "reclaimed": reclaim_orphaned_sync_jobs(now),
        "stuck_alerted": detect_stuck_pending_orders(now),
    }
