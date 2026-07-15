"""WS-C EXECUTION HEALTH — always-on monitor-chain reliability sweep.

The reliability supervisor (`reliability_tick`) is dormant in prod (`RELIABILITY_CORE_ENABLED=false`)
and, even when on, only inspects RUNNING jobs. Two silent execution defects therefore go unflagged
and unrepaired. This sweep runs inside the per-minute monitor chain (always on, no reliability-core
dependency) and closes both:

  (1) RECLAIM orphaned SYNC + MODIFY — a worker recycle (deploy/restart) orphans any in-flight
      ``SYNC_POSITIONS`` or ``MODIFY_POSITION`` job (stuck RUNNING, lease expired, never completed).
      Both are idempotent (SYNC auto-re-creates; the MODIFY SLTP edit re-reads the live SL and
      refuses any widen), so a lease-EXPIRED orphan is dead weight — SYNC drags the pipeline to
      DEGRADED, a stranded MODIFY wedges the TP-protection ladder (the sweep counts it inflight
      forever and never re-enqueues). Fail them (safe; only these two types, only lease-expired).

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

# A tradeable (auto-eligible source) APPROVED signal plans synchronously within seconds. One with
# NEITHER a plan NOR a durable AUTO_ROUTE_DEFERRED reason after this long vanished silently — the TI
# non-execution incident, where a leg-INSERT IntegrityError was mis-labelled ``duplicate_plan`` and
# never persisted. Generous vs the synchronous plan latency so a healthy signal never trips it.
UNPLANNED_SIGNAL_ALERT_SECONDS = int(os.getenv("UNPLANNED_SIGNAL_ALERT_SECONDS", "300") or 300)
# Only look back this far — a silent loss older than this is not actionable and bounds the scan.
UNPLANNED_SIGNAL_LOOKBACK_SECONDS = int(os.getenv("UNPLANNED_SIGNAL_LOOKBACK_SECONDS", "86400") or 86400)


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


def reclaim_orphaned_modify_jobs(now) -> int:
    """Fail dead (lease-expired) RUNNING MODIFY_POSITION jobs so the protection sweep re-enqueues.

    Without this a worker recycle mid-modify strands the job RUNNING forever: the worker only ever
    claims PENDING rows, and the protection sweep counts a same-stage RUNNING job as permanently
    ``inflight`` — so the leg never re-enqueues and the TP-protection ladder cannot self-heal. The
    SLTP edit is idempotent (the bridge re-reads the live SL and hard-refuses any widen), so
    re-running a modify after an orphan can never increase risk. Only touches lease-EXPIRED rows so
    it can never race a live worker. Returns the count reclaimed."""
    orphans = ExecutionJob.objects.filter(
        status=ExecutionJob.Status.RUNNING,
        job_type=ExecutionJob.JobType.MODIFY_POSITION,
        lease_expires_at__lt=now,
    )
    ids = list(orphans.values_list("id", flat=True))
    if not ids:
        return 0
    n = ExecutionJob.objects.filter(id__in=ids, status=ExecutionJob.Status.RUNNING).update(
        status=ExecutionJob.Status.FAILED, finished_at=now, recovered=True,
        recovery_reason="orphaned MODIFY_POSITION (lease expired, worker recycled) — monitor-chain reclaim",
        error_message="orphaned: lease expired, worker gone",
    )
    if n:
        logger.info("execution_health: reclaimed %s orphaned MODIFY jobs %s", n, ids[:20])
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


def _alert_unplanned_signal(approval, now) -> None:
    """One deduped WARN per tradeable APPROVED signal that reached no plan and no durable reason."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"unplanned_tradeable_signal:approval:{approval.id}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.WARN,
            component=Component.EXECUTION_PIPELINE,
            trading_account_id=None,
            title=f"TRADEABLE signal never planned — approval #{approval.id} ({approval.source})",
            body=(f"APPROVED {approval.source} {getattr(approval, 'symbol', '?')} "
                  f"{getattr(approval, 'direction', '?')} signal (approval #{approval.id}, "
                  f"message {approval.message_id}) reached NEITHER a plan NOR a durable "
                  f"AUTO_ROUTE_DEFERRED reason after {UNPLANNED_SIGNAL_ALERT_SECONDS}s — it was lost "
                  f"silently. Check the auto-router/planning path (runtime-schema parity, integrity "
                  f"errors)."),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"approval_id": approval.id, "source": approval.source,
                    "message_id": approval.message_id, "symbol": getattr(approval, "symbol", None),
                    "direction": getattr(approval, "direction", None)},
        )
        logger.error("execution_health: UNPLANNED-SIGNAL alert approval=%s source=%s",
                     approval.id, approval.source)
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("execution_health: failed to alert unplanned signal approval=%s",
                         getattr(approval, "id", "?"))


def detect_unplanned_tradeable_signals(now) -> dict:
    """A tradeable (auto-eligible source) APPROVED signal must reach a durable disposition — a plan,
    OR an AUTO_ROUTE_DEFERRED reason — within a bounded interval. One with NEITHER after the threshold
    vanished silently (the TI non-execution incident). Raise ONE deduped WARN per approval and
    auto-resolve when a plan or a durable reason later appears. ALERT-ONLY — never plans, mutates the
    approval, or replays a signal. Returns ``{"unplanned_alerted", "unplanned_resolved"}``."""
    from signal_intake.models import PendingSignalApproval, SignalAuditEvent
    from execution.models import SignalExecutionPlan, SignalSourceConfig
    from reliability.models import AlertEvent
    _DEFERRED = SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED
    tradeable = set(SignalSourceConfig.objects.filter(auto_demo_execution_enabled=True)
                    .values_list("source", flat=True))
    if not tradeable:
        return {"unplanned_alerted": 0, "unplanned_resolved": 0}
    lookback = now - timedelta(seconds=UNPLANNED_SIGNAL_LOOKBACK_SECONDS)
    cutoff = now - timedelta(seconds=UNPLANNED_SIGNAL_ALERT_SECONDS)
    # (a) ALERT — scan ONLY the tradeable APPROVED signals that have NO plan (``execution_plan`` is the
    # OneToOne reverse relation), so a healthy backlog of planned signals never dilutes the cap and the
    # OLDEST (most-overdue) unplanned signal is never dropped by it. This set is normally ~empty.
    unplanned = PendingSignalApproval.objects.filter(
        source__in=tradeable, status=PendingSignalApproval.Status.APPROVED,
        created_at__gte=lookback, created_at__lt=cutoff, execution_plan__isnull=True,
    ).order_by("id")[:500]
    alerted = 0
    for a in unplanned:
        if SignalAuditEvent.objects.filter(approval=a, event=_DEFERRED).exists():
            continue  # durably deferred → a real disposition exists, not a silent loss
        dedup_key = f"unplanned_tradeable_signal:approval:{a.id}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            continue
        _alert_unplanned_signal(a, now)
        alerted += 1
    # (b) RESOLVE — auto-close any OPEN alert whose approval has since gained a plan or a durable
    # reason. Bounded by the (tiny) number of open alerts, independent of the healthy-signal volume.
    resolved = 0
    for al in AlertEvent.objects.filter(
            dedup_key__startswith="unplanned_tradeable_signal:approval:",
            status=AlertEvent.Status.OPEN):
        aid = (al.detail or {}).get("approval_id")
        if aid is None:
            continue
        a = PendingSignalApproval.objects.filter(id=aid).first()
        if a is None:
            continue
        if (SignalExecutionPlan.objects.filter(approval=a).exists()
                or SignalAuditEvent.objects.filter(approval=a, event=_DEFERRED).exists()):
            al.status = AlertEvent.Status.RESOLVED
            al.resolved_at = now
            al.save(update_fields=["status", "resolved_at"])
            resolved += 1
    return {"unplanned_alerted": alerted, "unplanned_resolved": resolved}


def sweep_execution_health(*, limit: int = 500) -> dict:
    """One pass: reclaim dead SYNC/MODIFY orphans + alert on stuck-PENDING orders and tradeable
    signals that never reached a plan or a durable reason. Returns a counts dict."""
    if not execution_health_enabled():
        return {"enabled": False}
    now = timezone.now()
    # Job reclamation first — it unsticks the pipeline and must never be blocked by a downstream
    # alert-only check. The unplanned-signal detector runs LAST and fail-open (its failure degrades to
    # zero counts, never to a skipped reclaim).
    result = {
        "enabled": True,
        "reclaimed": reclaim_orphaned_sync_jobs(now),
        "reclaimed_modify": reclaim_orphaned_modify_jobs(now),
        "stuck_alerted": detect_stuck_pending_orders(now),
        "unplanned_alerted": 0,
        "unplanned_resolved": 0,
    }
    try:
        result.update(detect_unplanned_tradeable_signals(now))
    except Exception:  # pragma: no cover - defensive; alert-only must not break the sweep
        logger.exception("execution_health: unplanned-signal detector failed")
    return result
