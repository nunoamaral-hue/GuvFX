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

from execution.models import ExecutionJob, PLAN_MAX_CONCURRENT_GROUPS, SIGNAL_MAX_AGE_SECONDS

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
# A plan that reached PLANNED but never got a durable execute/reject disposition after this long is a
# promotion-layer silent gap (the plan-layer complement to the unplanned-signal, approval-layer guard).
STUCK_PROMOTION_ALERT_SECONDS = int(os.getenv("STUCK_PROMOTION_ALERT_SECONDS", "300") or 300)
# A PLACE_ORDER the bridge reported FAILED but whose position (leg comment) LATER appeared is a lost-ACK
# that the in-process bridge recovery missed — exposure the platform believes ABSENT. Scan recently-
# FAILED order jobs this far back and alert if the leg's Trade now exists (bounds the scan).
FAILED_FILLED_LOOKBACK_SECONDS = int(os.getenv("FAILED_FILLED_LOOKBACK_SECONDS", "3600") or 3600)

# ── Orphaned-PLANNED-plan reclaim (concurrency-gate leak fix) ────────────────────────────────────
# A promotion-rejected (e.g. daily_drawdown_hit) or otherwise never-promoted plan is left PLANNED
# forever; ``count_active`` counts PLANNED-only, so each orphan permanently consumes one of
# ``PLAN_MAX_CONCURRENT_GROUPS`` slots and eventually hard-blocks EVERY new signal. Reclaim = void such
# a plan (PLANNED→VOIDED, no order) once it is old enough to be un-promotable.
# W1: the reclaim age MUST exceed SIGNAL_MAX_AGE_SECONDS — promotion re-checks staleness, so a plan older
# than that can never become an order; voiding it drops nothing. The reclaim function rejects any smaller
# value (fail loud). Default 900s (15 min) >> 120s staleness.
ORPHANED_PLANNED_RECLAIM_SECONDS = int(os.getenv("ORPHANED_PLANNED_RECLAIM_SECONDS", "900") or 900)
# The monitor-chain SELF-HEAL is inert unless enabled — same default-OFF posture as the other mutating
# monitor-chain behaviours (BREAKEVEN_ENABLED, PROVIDER_COMMANDS_ENABLED). Deploying the fix must NOT
# auto-reclaim the existing backlog before the operator has reviewed a dry-run; the one-time recovery is
# the ``reclaim_orphaned_planned_plans`` management command. Part B (transition-on-reject in the
# auto_router) prevents FUTURE accumulation with NO flag and without touching existing rows.
ORPHANED_PLANNED_RECLAIM_ENABLED = os.getenv(
    "ORPHANED_PLANNED_RECLAIM_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
# Proactively WARN when a tradeable account+symbol's PLANNED concurrency gate approaches its cap — so the
# defect is visible BEFORE it saturates and blocks trading (objective of the monitoring extension).
CONCURRENCY_SATURATION_ALERT_RATIO = float(
    os.getenv("CONCURRENCY_SATURATION_ALERT_RATIO", "0.8") or 0.8)


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
        _record_hosted_recovery_completion(ids)  # ADR-0034: close STARTED->FINISHED for hosted orphans
        logger.info("execution_health: reclaimed %s orphaned MODIFY jobs %s", n, ids[:20])
    return n


def _record_hosted_recovery_completion(ids) -> None:
    """ADR-0034 Execution Engine: a crash-after-send hosted job resolved by THIS recovery path (bulk
    ``.update()``, bypassing ``/complete``) would otherwise be stranded STARTED forever in the append-only
    ``HostedWorkspaceExecution`` occupancy log. Close it: append the terminal FINISHED provenance row for each
    hosted (``hosted_workspace_uuid``-stamped) job among ``ids``. Flag-first (zero query while the subsystem
    is dark), fail-safe + idempotent per (job, phase), and order-authority-free."""
    from execution.hosted_pin import pin_subsystem_enabled
    if not pin_subsystem_enabled():
        return
    from execution.hosted_execution import record_hosted_completion
    for job in ExecutionJob.objects.filter(id__in=ids).exclude(hosted_workspace_uuid=""):
        record_hosted_completion(job, correlation_id=str((job.payload or {}).get("correlation_id", "")))


def _leg_comment(plan_id, leg_index):
    """The broker order comment that correlates a placed order to its leg (see signal_promotion)."""
    return "WAY%sL%s" % (plan_id, leg_index)


def reconcile_orphaned_place_orders(now) -> dict:
    """A worker recycle can strand a ``PLACE_ORDER`` job RUNNING with an EXPIRED lease. Unlike SYNC /
    MODIFY, a place-order is **NOT idempotent** — re-running it could place a DUPLICATE broker order —
    so it must NEVER be re-enqueued. Instead RECONCILE against the broker's own record:

      * if the leg's order actually LANDED (a ``Trade`` with the leg's ``WAY{plan}L{leg}`` correlation
        comment exists on the account) → the worker died AFTER order_send but before recording
        completion → mark the job SUCCESS (bookkeeping catches up; auto-resolves any prior alert);
      * if NO trade landed → the order may be genuinely MISSING (a partially-executed signal) → raise
        ONE deduped WARN for an operator. Do NOT auto-retry (a live re-run risks a duplicate order).

    Only touches lease-EXPIRED rows, so it can never race a live worker claim. Returns counts."""
    from trading.models import Trade
    from reliability.models import AlertEvent
    orphans = list(ExecutionJob.objects.filter(
        status=ExecutionJob.Status.RUNNING,
        job_type=ExecutionJob.JobType.PLACE_ORDER,   # WAY-comment reconcile is PLACE_ORDER-specific
        lease_expires_at__lt=now,
    ).order_by("id")[:200])
    reconciled = alerted = 0
    for job in orphans:
        payload = job.payload or {}
        plan_id, leg_index = payload.get("plan_id"), payload.get("leg_index")
        trade = None
        if plan_id is not None and leg_index is not None:
            trade = (Trade.objects.filter(
                account_id=job.account_id, comment=_leg_comment(plan_id, leg_index))
                .order_by("-open_time").first())
        dedup_key = f"orphaned_place_order:job:{job.id}"
        if trade is not None:
            n = ExecutionJob.objects.filter(id=job.id, status=ExecutionJob.Status.RUNNING).update(
                status=ExecutionJob.Status.SUCCESS, finished_at=now, recovered=True,
                recovery_reason=(f"orphaned PLACE_ORDER reconciled to broker ticket {trade.ticket} "
                                 f"(order landed; worker recycled before completion)"),
                result={"ok": True, "reconciled": True, "ticket": trade.ticket})
            if n:
                reconciled += 1
                # ADR-0034: close the hosted STARTED->FINISHED occupancy for this crash-recovered hosted job
                # (fail-safe, flag-first no-op while dark / for non-hosted; idempotent per (job, phase) even
                # if /complete also ran). Reflect the recovered terminal status for the outcome.
                job.status = ExecutionJob.Status.SUCCESS
                from execution.hosted_execution import record_hosted_completion
                record_hosted_completion(job, correlation_id=str((job.payload or {}).get("correlation_id", "")))
                # The order was fine all along → auto-resolve any earlier "possible missing order" alert.
                AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).update(
                    status=AlertEvent.Status.RESOLVED, resolved_at=now)
                logger.info("execution_health: reconciled orphaned PLACE_ORDER job %s -> ticket %s",
                            job.id, trade.ticket)
        else:
            _alert_orphaned_place_order(job, now)
            alerted += 1
    # Independent resolve pass: the orphan query only ever returns RUNNING jobs, but a merely-slow
    # worker (not dead) can complete AFTER its lease expired and mark its own job SUCCESS — the
    # reconcile loop then never revisits it. Resolve the alert on the BROKER-side signal (the leg's
    # Trade now exists), NOT on job status: a job flipping to SUCCESS without a trade, or to FAILED,
    # must KEEP the alert open (the order really is missing). This mirrors the unplanned-signal
    # resolver so a transient orphan alert never lingers.
    resolved = 0
    for al in AlertEvent.objects.filter(
            dedup_key__startswith="orphaned_place_order:job:", status=AlertEvent.Status.OPEN):
        d = al.detail or {}
        pid, leg = d.get("plan_id"), d.get("leg_index")
        if pid is None or leg is None:
            continue
        acct_id = ExecutionJob.objects.filter(id=d.get("job_id")).values_list(
            "account_id", flat=True).first()
        if acct_id is None:
            continue
        if Trade.objects.filter(account_id=acct_id, comment=_leg_comment(pid, leg)).exists():
            al.status = AlertEvent.Status.RESOLVED
            al.resolved_at = now
            al.save(update_fields=["status", "resolved_at"])
            resolved += 1

    # (Control 5) FAILED-but-filled backstop — see module note + FAILED_FILLED_LOOKBACK_SECONDS. The
    # RUNNING scan above never revisits a job that reached FAILED, so a lost-ACK whose position appeared
    # only AFTER the bridge reported failure (settlement lag beyond the bridge's in-process recovery)
    # leaves exposure the platform believes ABSENT — the most dangerous silent path. DETECT a recently-
    # FAILED order job whose leg Trade now exists and raise ONE deduped CRITICAL alert. ALERT-ONLY: do
    # NOT auto-flip FAILED->SUCCESS (that would mutate already-resolved plan state); an operator reconciles.
    failed_filled_alerted = 0
    for job in ExecutionJob.objects.filter(
            status=ExecutionJob.Status.FAILED, job_type=ExecutionJob.JobType.PLACE_ORDER,
            finished_at__gte=now - timedelta(seconds=FAILED_FILLED_LOOKBACK_SECONDS),
    ).order_by("-finished_at")[:200]:   # recency-first so a burst never starves the newest still-open fill
        payload = job.payload or {}
        plan_id, leg_index = payload.get("plan_id"), payload.get("leg_index")
        if plan_id is None or leg_index is None:
            continue
        if not Trade.objects.filter(
                account_id=job.account_id, comment=_leg_comment(plan_id, leg_index)).exists():
            continue
        if _alert_failed_but_filled(job, now):
            failed_filled_alerted += 1
    # Resolve a failed-but-filled alert once its job is no longer FAILED (an operator reconciled it).
    failed_filled_resolved = 0
    for al in AlertEvent.objects.filter(
            dedup_key__startswith="failed_but_filled:job:", status=AlertEvent.Status.OPEN):
        jid = (al.detail or {}).get("job_id")
        if jid is None:
            continue
        if not ExecutionJob.objects.filter(id=jid, status=ExecutionJob.Status.FAILED).exists():
            al.status = AlertEvent.Status.RESOLVED
            al.resolved_at = now
            al.save(update_fields=["status", "resolved_at"])
            failed_filled_resolved += 1

    return {"place_order_reconciled": reconciled, "place_order_orphan_alerted": alerted,
            "place_order_orphan_resolved": resolved,
            "place_order_failed_but_filled_alerted": failed_filled_alerted,
            "place_order_failed_but_filled_resolved": failed_filled_resolved}


def _alert_orphaned_place_order(job, now) -> None:
    """One deduped WARN for a lease-expired RUNNING PLACE_ORDER with NO matching broker trade — the
    order may be missing (partial signal execution). ALERT-ONLY: never re-run (avoid a duplicate)."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"orphaned_place_order:job:{job.id}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        payload = job.payload or {}
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.WARN, component=Component.EXECUTION_PIPELINE,
            trading_account_id=job.account_id,
            title=f"Possible missing order — PLACE_ORDER job #{job.id} orphaned, no broker trade",
            body=(f"PLACE_ORDER job #{job.id} (plan {payload.get('plan_id')} leg "
                  f"{payload.get('leg_index')}, {payload.get('signal_source', '?')} "
                  f"{payload.get('symbol', '?')}) has been RUNNING with an EXPIRED lease and NO Trade "
                  f"with comment {_leg_comment(payload.get('plan_id'), payload.get('leg_index'))} was "
                  f"ingested — the order may be missing. NOT auto-retried (would risk a duplicate); "
                  f"verify on the broker and place manually if genuinely absent."),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"job_id": job.id, "plan_id": payload.get("plan_id"),
                    "leg_index": payload.get("leg_index"), "signal_source": payload.get("signal_source")})
        logger.error("execution_health: ORPHANED-PLACE_ORDER alert job=%s plan=%s leg=%s",
                     job.id, payload.get("plan_id"), payload.get("leg_index"))
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("execution_health: failed to alert orphaned place order job=%s",
                         getattr(job, "id", "?"))


def _alert_failed_but_filled(job, now) -> bool:
    """One deduped CRITICAL alert: an order job recorded FAILED but a Trade with the leg comment exists
    — exposure is OPEN that the platform believes ABSENT (a lost-ACK the bridge recovery missed).
    Returns True iff a NEW alert was created. Best-effort."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"failed_but_filled:job:{job.id}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return False
        payload = job.payload or {}
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.CRITICAL, component=Component.EXECUTION_PIPELINE,
            trading_account_id=job.account_id,
            title=f"Exposure open for a FAILED order — job #{job.id} filled despite reported failure",
            body=(f"{job.job_type} job #{job.id} (plan {payload.get('plan_id')} leg "
                  f"{payload.get('leg_index')}, {payload.get('signal_source', '?')} "
                  f"{payload.get('symbol', '?')}) was recorded FAILED, but a Trade with comment "
                  f"{_leg_comment(payload.get('plan_id'), payload.get('leg_index'))} exists — the order "
                  f"LANDED (lost ACK). The position is OPEN and UNMANAGED (the leg is FAILED). Manage/close "
                  f"the position on the broker AND reconcile the job to SUCCESS — this alert clears ONLY "
                  f"when the job is no longer FAILED (closing on the broker alone will not resolve it)."),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"job_id": job.id, "plan_id": payload.get("plan_id"),
                    "leg_index": payload.get("leg_index"), "signal_source": payload.get("signal_source")})
        logger.error("execution_health: FAILED-BUT-FILLED alert job=%s plan=%s leg=%s",
                     job.id, payload.get("plan_id"), payload.get("leg_index"))
        return True
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("execution_health: failed to alert failed-but-filled job=%s",
                         getattr(job, "id", "?"))
        return False


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


def _alert_stuck_promotion(plan, now) -> None:
    """One deduped WARN per PLANNED plan that reached no order and no durable rejection."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"stuck_promotion:plan:{plan.id}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.WARN,
            component=Component.EXECUTION_PIPELINE,
            trading_account_id=plan.account_id,
            title=f"Promotion stuck — plan #{plan.id} ({plan.source}) has no disposition",
            body=(f"{plan.source} {plan.symbol} {plan.direction} plan #{plan.id} (message "
                  f"{plan.message_id}) is still PLANNED after {STUCK_PROMOTION_ALERT_SECONDS}s with "
                  f"NEITHER a PLACE_ORDER job NOR a PROMOTION_REJECTED reason — it reached planning but "
                  f"never got a durable execute/reject disposition. Check the promotion path."),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"plan_id": plan.id, "source": plan.source, "message_id": plan.message_id,
                    "symbol": plan.symbol, "direction": plan.direction},
        )
        logger.error("execution_health: STUCK-PROMOTION alert plan=%s source=%s",
                     plan.id, plan.source)
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("execution_health: failed to alert stuck promotion plan=%s",
                         getattr(plan, "id", "?"))


def detect_stuck_promotions(now) -> dict:
    """A PLANNED plan (auto-eligible source) must reach a durable disposition — a PLACE_ORDER job OR a
    PROMOTION_REJECTED reason — within a bounded interval. One with NEITHER after the threshold is a
    silent PROMOTION-layer gap: the plan-layer complement to ``detect_unplanned_tradeable_signals``
    (which guards the approval layer). Together they close the gap end to end — every APPROVED signal
    reaches a plan, and every plan reaches an execute/reject disposition. Raise ONE deduped WARN per
    plan and auto-resolve when an order, a rejection, or a terminal status later appears. ALERT-ONLY —
    never promotes, mutates the plan, or replays. Returns
    ``{"stuck_promotion_alerted", "stuck_promotion_resolved"}``."""
    from execution.models import (SignalExecutionPlan, ExecutionJob, PromotionAuditEvent,
                                  SignalSourceConfig)
    from reliability.models import AlertEvent

    def _plans_with_order(plan_ids):
        # ONE query (no per-plan N+1): which of these plan ids have a PLACE_ORDER job.
        if not plan_ids:
            return set()
        return set(ExecutionJob.objects.filter(
            job_type="PLACE_ORDER", payload__plan_id__in=list(plan_ids))
            .values_list("payload__plan_id", flat=True))

    tradeable = set(SignalSourceConfig.objects.filter(auto_demo_execution_enabled=True)
                    .values_list("source", flat=True))
    cutoff = now - timedelta(seconds=STUCK_PROMOTION_ALERT_SECONDS)
    # ALERT — only when there is a tradeable source. PLANNED plans in the window, MINUS those carrying a
    # durable PROMOTION_REJECTED reason (the normal drawdown/risk rejections, e.g. daily_drawdown_hit).
    # PROMOTED/CLOSED/VOIDED/HELD/SUPERSEDED are already excluded by ``status=PLANNED``. The exclude is a
    # NOT-EXISTS subquery (no join fan-out), so the remainder is the stuck set (~empty), no DISTINCT.
    alerted = 0
    if tradeable:
        lookback = now - timedelta(seconds=UNPLANNED_SIGNAL_LOOKBACK_SECONDS)
        candidates = list(SignalExecutionPlan.objects.filter(
            source__in=tradeable, status=SignalExecutionPlan.Status.PLANNED,
            created_at__gte=lookback, created_at__lt=cutoff)
            .exclude(promotion_audit_events__event="PROMOTION_REJECTED")
            .order_by("id")[:500])
        ordered = _plans_with_order([p.id for p in candidates])
        for p in candidates:
            if p.id in ordered:
                continue  # an order exists → executed (status merely lagged), not a silent gap
            dedup_key = f"stuck_promotion:plan:{p.id}"
            if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
                continue
            _alert_stuck_promotion(p, now)
            alerted += 1
    # RESOLVE — ALWAYS runs (independent of ``tradeable``, so disabling every source cannot strand an
    # OPEN alert). Auto-close any OPEN alert whose plan has since gained an order, a rejection, or a
    # non-PLANNED status. Bounded by the (tiny) number of open alerts; batched, no per-plan N+1.
    resolved = 0
    open_alerts = list(AlertEvent.objects.filter(
        dedup_key__startswith="stuck_promotion:plan:", status=AlertEvent.Status.OPEN))
    open_pids = [pid for pid in ((al.detail or {}).get("plan_id") for al in open_alerts)
                 if pid is not None]
    if open_pids:
        status_by_id = dict(SignalExecutionPlan.objects.filter(id__in=open_pids)
                            .values_list("id", "status"))
        ordered_open = _plans_with_order(open_pids)
        rejected_open = set(PromotionAuditEvent.objects.filter(
            plan_id__in=open_pids, event="PROMOTION_REJECTED").values_list("plan_id", flat=True))
        for al in open_alerts:
            pid = (al.detail or {}).get("plan_id")
            if pid is None or pid not in status_by_id:
                continue
            if (status_by_id[pid] != SignalExecutionPlan.Status.PLANNED
                    or pid in ordered_open or pid in rejected_open):
                al.status = AlertEvent.Status.RESOLVED
                al.resolved_at = now
                al.save(update_fields=["status", "resolved_at"])
                resolved += 1
    return {"stuck_promotion_alerted": alerted, "stuck_promotion_resolved": resolved}


def detect_protection_watcher_health(now) -> dict:
    """GFX-PKT-TP-PROTECTION-LATENCY / WS-L — deduped, auto-resolving alerts for the fast-protection
    path: (1) the armed watcher's heartbeat is stale (it stopped → protection silently fell back to
    the slow minute chain); (2) protection position-syncs are repeatedly stranding within the last
    hour (the intermittent bridge/MT5 stall that delays TP-close ingestion). Best-effort; the only
    mutation is the AlertEvent it manages."""
    from datetime import timedelta
    out = {"watcher_stale_alerted": 0, "sync_stall_alerted": 0}
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent, Heartbeat
        armed = os.getenv("TP_WATCHER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
        hb = Heartbeat.objects.filter(source="tp_protection_watcher").first()
        interval = (hb.expected_interval_s if hb else None) or 90
        age = (now - hb.last_beat_at).total_seconds() if (hb and hb.last_beat_at) else None
        stale = bool(armed and (hb is None or (age is not None and age > interval * 3)))
        open_stale = AlertEvent.objects.filter(dedup_key="tp_watcher_stale", status=AlertEvent.Status.OPEN)
        if stale and not open_stale.exists():
            AlertEvent.objects.create(
                severity=AlertEvent.Severity.WARN, component=Component.EXECUTION_PIPELINE,
                title="TP protection watcher heartbeat stale",
                body=("The adaptive TP-protection watcher is armed but has not heart-beaten in "
                      f"{'?' if age is None else int(age)}s — protection has fallen back to the "
                      "slower minute monitor chain."),
                dedup_key="tp_watcher_stale", status=AlertEvent.Status.OPEN, detail={"age_s": age})
            out["watcher_stale_alerted"] = 1
        elif not stale and open_stale.exists():
            open_stale.update(status=AlertEvent.Status.RESOLVED, resolved_at=now)

        # A stranded protection sync is EITHER lease-reclaimed (recovered=True) OR self-failed fast by
        # the worker on a bridge/HTTP error (marked ``self_failed`` — reaches FAILED before the lease,
        # so the reclaimer never sees it). Count both so a real bridge/terminal hang still trips this.
        from django.db.models import Q as _Q
        _stranded = _Q(recovered=True) | _Q(error_message__startswith="worker processing error")
        strands = ExecutionJob.objects.filter(
            _stranded, job_type=ExecutionJob.JobType.SYNC_POSITIONS, status=ExecutionJob.Status.FAILED,
            payload__breakeven_sync=True, finished_at__gte=now - timedelta(hours=1)).count()
        threshold = int(os.getenv("PROTECTION_SYNC_STALL_ALERT_THRESHOLD", "3"))
        open_stall = AlertEvent.objects.filter(dedup_key="protection_sync_stall", status=AlertEvent.Status.OPEN)
        if strands >= threshold and not open_stall.exists():
            AlertEvent.objects.create(
                severity=AlertEvent.Severity.WARN, component=Component.EXECUTION_PIPELINE,
                title="Protection SYNC ingestion stalling",
                body=(f"{strands} protection position-syncs stranded (lease-reclaimed or worker fast-fail) in the last hour "
                      "— the MT5 bridge/terminal is intermittently hanging, delaying TP-close ingestion "
                      "and protection. The short protection-sync lease bounds the impact; investigate "
                      "the bridge if this persists."),
                dedup_key="protection_sync_stall", status=AlertEvent.Status.OPEN, detail={"strands_1h": strands})
            out["sync_stall_alerted"] = 1
        elif strands < threshold and open_stall.exists():
            open_stall.update(status=AlertEvent.Status.RESOLVED, resolved_at=now)

        # BSTALL: throttle-storm detector. If MANY jobs (any SYNC type) orphan within the hour, the
        # worker is likely being HTTP-429'd by the backend claim throttle and leaving jobs RUNNING —
        # the self-inflicted stall root cause. A distinct deduped, auto-resolving alert so a recurrence
        # is caught directly (the be_sync alert above only sees protection syncs).
        all_strands = ExecutionJob.objects.filter(
            _stranded, job_type=ExecutionJob.JobType.SYNC_POSITIONS, status=ExecutionJob.Status.FAILED,
            finished_at__gte=now - timedelta(hours=1)).count()
        storm_threshold = int(os.getenv("WORKER_THROTTLE_STORM_THRESHOLD", "15"))
        out["orphaned_sync_1h"] = all_strands
        open_storm = AlertEvent.objects.filter(dedup_key="worker_throttle_storm", status=AlertEvent.Status.OPEN)
        if all_strands >= storm_threshold and not open_storm.exists():
            AlertEvent.objects.create(
                severity=AlertEvent.Severity.WARN, component=Component.EXECUTION_PIPELINE,
                title="Worker job-claim throttle storm (orphaned SYNCs)",
                body=(f"{all_strands} SYNC jobs stranded (lease-reclaimed or worker fast-fail) in the last hour — the ingest "
                      "worker is likely exceeding the backend claim rate limit (HTTP 429) and leaving "
                      "jobs RUNNING. Check worker logs for 'rate_limited'/429 and the jobs/next/ call rate."),
                dedup_key="worker_throttle_storm", status=AlertEvent.Status.OPEN, detail={"orphaned_sync_1h": all_strands})
            out["throttle_storm_alerted"] = 1
        elif all_strands < storm_threshold and open_storm.exists():
            open_storm.update(status=AlertEvent.Status.RESOLVED, resolved_at=now)
    except Exception:  # pragma: no cover - alert-only must never break the sweep
        logger.exception("execution_health: protection-watcher health check failed")
    return out


def reclaim_orphaned_planned_plans(now=None, *, older_than_seconds=None, account_id=None,
                                   symbol=None, source=None, limit=500, apply=False) -> dict:
    """Reclaim orphaned PLANNED ``SignalExecutionPlan``s that permanently hold a concurrency slot.

    Root cause context: ``count_active`` counts PLANNED-only, and a promotion-rejected / never-promoted
    plan is left PLANNED forever — so each orphan permanently consumes one of the
    ``PLAN_MAX_CONCURRENT_GROUPS`` slots and eventually rejects EVERY new signal (concurrent_limit_exceeded).

    SAFE BY CONSTRUCTION. The only code that turns a plan into a real order (``signal_promotion._promote_plan``
    → ``_validate``) re-reads ``status == PLANNED`` AND re-checks signal age ≤ ``SIGNAL_MAX_AGE_SECONDS``
    before creating any job. We only ever void plans OLDER (by ``created_at`` — W5) than
    ``older_than_seconds``, which MUST exceed ``SIGNAL_MAX_AGE_SECONDS`` (W1, enforced below): such a plan is
    already un-promotable, so voiding it drops nothing that could execute. The transition is a compare-and-set
    ``PLANNED → VOIDED`` (race-safe — a plan promoted concurrently matches zero rows and is untouched),
    idempotent, and creates NO order or job.

    ``apply=False`` (DEFAULT) is a pure DRY-RUN: it lists what WOULD be reclaimed and mutates nothing.
    Returns a deterministic report dict (``scanned``/``reclaimed``/``apply``/``older_than_seconds``/
    ``age_buckets``/``candidates``).
    """
    from datetime import timedelta

    from execution.models import PlanAuditEvent, PromotionAuditEvent, SignalExecutionPlan

    now = now or timezone.now()
    threshold = ORPHANED_PLANNED_RECLAIM_SECONDS if older_than_seconds is None else int(older_than_seconds)
    # W1 — hard floor: the entire safety argument depends on only voiding un-promotable (stale) plans.
    if threshold <= SIGNAL_MAX_AGE_SECONDS:
        raise ValueError(
            f"reclaim threshold {threshold}s must exceed SIGNAL_MAX_AGE_SECONDS ({SIGNAL_MAX_AGE_SECONDS}s): "
            f"a younger plan may still be within the promotable window")
    # LOAD-BEARING INVARIANT: created_at is auto_now_add (set at INSERT, after the signal arrived), so
    # signal_timestamp <= created_at always. A plan with created_at older than a threshold >
    # SIGNAL_MAX_AGE_SECONDS is therefore necessarily stale (un-promotable). W5 keys on created_at for
    # liveness even if signal_timestamp is bogus/future-dated.
    cutoff = now - timedelta(seconds=threshold)  # W5 — created_at basis (never signal_timestamp)
    qs = SignalExecutionPlan.objects.filter(
        status=SignalExecutionPlan.Status.PLANNED, created_at__lt=cutoff)
    if account_id is not None:
        qs = qs.filter(account_id=account_id)
    if symbol:
        qs = qs.filter(symbol=symbol.upper())
    if source:
        qs = qs.filter(source=source)
    plans = list(qs.order_by("id")[:limit])  # deterministic order for a reproducible dry-run report

    reasons = {}  # latest durable PROMOTION_REJECTED code per candidate (one query; for report + audit)
    if plans:
        for rpid, detail in (PromotionAuditEvent.objects.filter(
                plan_id__in=[p.id for p in plans], event="PROMOTION_REJECTED")
                .order_by("created_at").values_list("plan_id", "detail")):
            reasons[rpid] = (detail or {}).get("code", "promotion_rejected")  # last (latest) wins

    candidates, buckets = [], {"lt_1h": 0, "1h_6h": 0, "6h_24h": 0, "gte_24h": 0}
    for p in plans:
        age_s = int((now - p.created_at).total_seconds())
        buckets["lt_1h" if age_s < 3600 else "1h_6h" if age_s < 21600
                else "6h_24h" if age_s < 86400 else "gte_24h"] += 1
        candidates.append({
            "plan_id": p.id, "account_id": p.account_id, "symbol": p.symbol, "source": p.source,
            "direction": p.direction, "message_id": p.message_id,
            "created_at": p.created_at.isoformat(), "age_seconds": age_s,
            "prior_reject_reason": reasons.get(p.id),
        })

    reclaimed = 0
    if apply:
        for p in plans:
            # Compare-and-set: a plan promoted concurrently (→ PROMOTED) matches zero rows → untouched.
            updated = SignalExecutionPlan.objects.filter(
                id=p.id, status=SignalExecutionPlan.Status.PLANNED
            ).update(status=SignalExecutionPlan.Status.VOIDED, hold_reason="orphaned_planned_reclaim")
            if not updated:
                continue  # promoted/closed/voided between the read and here — leave it
            reclaimed += 1
            try:
                PlanAuditEvent.objects.create(
                    event=PlanAuditEvent.Event.PLAN_VOIDED, plan_id=p.id, approval_id=p.approval_id,
                    detail={"reason": "orphaned_planned_reclaim",
                            "age_seconds": int((now - p.created_at).total_seconds()),
                            "prior_reject_reason": reasons.get(p.id), "threshold_seconds": threshold})
            except Exception:  # pragma: no cover - audit is best-effort; the transition already committed
                logger.exception("reclaim: PLAN_VOIDED audit failed for plan=%s (transition committed)", p.id)
        if reclaimed:
            logger.warning("execution_health: reclaimed %s orphaned PLANNED plans (freed concurrency slots) %s",
                           reclaimed, [p.id for p in plans][:20])
    return {"scanned": len(plans), "reclaimed": reclaimed, "apply": apply,
            "older_than_seconds": threshold, "age_buckets": buckets, "candidates": candidates}


def detect_saturated_concurrency_gates(now) -> dict:
    """Proactively WARN when a tradeable account+symbol's PLANNED concurrency gate approaches its cap —
    BEFORE it saturates and starts rejecting every signal (``concurrent_limit_exceeded``). One deduped
    alert per (account, symbol); CRITICAL at the cap, WARN above the ratio; auto-resolves when utilisation
    falls back below the threshold. ALERT-ONLY — never mutates a plan. Returns
    ``{"saturation_alerted", "saturation_resolved"}``."""
    from django.db.models import Count

    from execution.models import SignalExecutionPlan, SignalSourceConfig
    from reliability.constants import Component
    from reliability.models import AlertEvent

    result = {"saturation_alerted": 0, "saturation_resolved": 0}
    tradeable = set(SignalSourceConfig.objects.filter(auto_demo_execution_enabled=True)
                    .values_list("source", flat=True))
    warn_at = max(1, int(CONCURRENCY_SATURATION_ALERT_RATIO * PLAN_MAX_CONCURRENT_GROUPS))
    # Count PLANNED source-AGNOSTICALLY per (account, symbol) — this MUST match the real gate, which is
    # source-agnostic: ``count_active`` / ``signal_planning.py:242`` count EVERY PLANNED plan for the
    # (account, symbol), regardless of source. An orphan from a now-disabled source still saturates the gate
    # and blocks tradeable signals, so it must be counted here or the alert under-reports the true block.
    # We still gate the whole check on the system having at least one tradeable source (else nothing trades,
    # so a saturated gate cannot block anything). One grouped query, no per-account loop.
    saturated = {}
    if tradeable:
        for g in (SignalExecutionPlan.objects.filter(status=SignalExecutionPlan.Status.PLANNED)
                  .values("account_id", "symbol").annotate(n=Count("id")).filter(n__gte=warn_at)):
            key = f"concurrency_saturation:{g['account_id']}:{g['symbol']}"
            saturated[key] = (g["account_id"], g["symbol"], g["n"])
    for key, (acct, sym, n) in saturated.items():
        if AlertEvent.objects.filter(dedup_key=key, status=AlertEvent.Status.OPEN).exists():
            continue
        at_cap = n >= PLAN_MAX_CONCURRENT_GROUPS
        try:
            AlertEvent.objects.create(
                severity=AlertEvent.Severity.CRITICAL if at_cap else AlertEvent.Severity.WARN,
                component=Component.EXECUTION_PIPELINE, trading_account_id=acct,
                title=(f"Concurrency gate {'SATURATED' if at_cap else 'near cap'} — acct {acct} {sym} "
                       f"({n}/{PLAN_MAX_CONCURRENT_GROUPS} PLANNED)"),
                body=(f"{n} PLANNED plans hold the concurrency gate for account {acct} {sym} (cap "
                      f"{PLAN_MAX_CONCURRENT_GROUPS}). AT the cap every new signal is rejected "
                      f"concurrent_limit_exceeded. Orphaned PLANNED plans from rejected/never-promoted "
                      f"signals leak here — run reclaim_orphaned_planned_plans."),
                dedup_key=key, status=AlertEvent.Status.OPEN,
                detail={"account_id": acct, "symbol": sym, "planned_count": n,
                        "cap": PLAN_MAX_CONCURRENT_GROUPS})
            result["saturation_alerted"] += 1
            logger.error("execution_health: CONCURRENCY-SATURATION alert acct=%s %s planned=%s/%s",
                         acct, sym, n, PLAN_MAX_CONCURRENT_GROUPS)
        except Exception:  # pragma: no cover - alerting is best-effort
            logger.exception("execution_health: failed to alert concurrency saturation acct=%s %s", acct, sym)
    # RESOLVE — close any OPEN saturation alert whose group has dropped below the threshold.
    for al in AlertEvent.objects.filter(dedup_key__startswith="concurrency_saturation:",
                                        status=AlertEvent.Status.OPEN):
        if al.dedup_key not in saturated:
            al.status = AlertEvent.Status.RESOLVED
            al.resolved_at = now
            al.save(update_fields=["status", "resolved_at"])
            result["saturation_resolved"] += 1
    return result


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
        "place_order_reconciled": 0,
        "place_order_orphan_alerted": 0,
        "unplanned_alerted": 0,
        "unplanned_resolved": 0,
        "stuck_promotion_alerted": 0,
        "stuck_promotion_resolved": 0,
        "planned_reclaimed": 0,
        "planned_reclaim_scanned": 0,
        "saturation_alerted": 0,
        "saturation_resolved": 0,
    }
    # Part C — self-heal orphaned PLANNED plans that permanently hold a concurrency slot. MUTATING, so
    # inert unless ORPHANED_PLANNED_RECLAIM_ENABLED (same default-OFF posture as the other mutating
    # monitor-chain behaviours) — deploying the fix must NOT auto-reclaim the existing backlog before the
    # operator has reviewed a dry-run (that one-time recovery is the management command). Own try/except so
    # a config/validation error never blocks the rest of the sweep.
    if ORPHANED_PLANNED_RECLAIM_ENABLED:
        try:
            pr = reclaim_orphaned_planned_plans(now, apply=True, limit=limit)
            result["planned_reclaimed"] = pr["reclaimed"]
            result["planned_reclaim_scanned"] = pr["scanned"]
        except Exception:  # pragma: no cover - defensive; self-heal must not break the sweep
            logger.exception("execution_health: orphaned-PLANNED reclaim failed")
    # Reconcile orphaned RUNNING place-orders against the broker (safe: never re-runs an order). Its
    # own try/except keeps a failure here from blocking the alert-only detector below.
    try:
        result.update(reconcile_orphaned_place_orders(now))
    except Exception:  # pragma: no cover - defensive
        logger.exception("execution_health: place-order reconcile failed")
    try:
        result.update(detect_unplanned_tradeable_signals(now))
    except Exception:  # pragma: no cover - defensive; alert-only must not break the sweep
        logger.exception("execution_health: unplanned-signal detector failed")
    try:
        result.update(detect_stuck_promotions(now))
    except Exception:  # pragma: no cover - defensive; alert-only must not break the sweep
        logger.exception("execution_health: stuck-promotion detector failed")
    try:
        result.update(detect_protection_watcher_health(now))
    except Exception:  # pragma: no cover - defensive; alert-only must not break the sweep
        logger.exception("execution_health: protection-watcher health check failed")
    try:
        result.update(detect_saturated_concurrency_gates(now))
    except Exception:  # pragma: no cover - defensive; alert-only must not break the sweep
        logger.exception("execution_health: concurrency-saturation detector failed")
    return result
