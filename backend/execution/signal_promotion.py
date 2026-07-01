"""
EXEC-E2a — PLANNED SignalExecutionPlan → suppressed, un-claimable shadow jobs.

HARD BOUNDARY — this module creates ``PLACE_ORDER_SHADOW`` ``ExecutionJob``
records ONLY. It NEVER:

  * calls MT5 / ``order_send`` / ``order_check``,
  * calls ``create_place_order_job`` / ``create_open_trade_job``,
  * contacts the Windows agent / bridge or makes any network call,
  * creates an executable ``PLACE_ORDER`` / ``OPEN_TRADE`` / ``PLACE_TEST_ORDER`` job,
  * uses broker credentials,
  * activates a Telegram listener.

A ``PLACE_ORDER_SHADOW`` job is suppressed three independent ways: (1)
``execution_mode=SHADOW`` in its payload (fail-closed flag); (2) no deployed
worker requests its job_type; (3) the ``next_job`` endpoint guard refuses to
serve it to a non-``shadow_worker`` caller. No consumer executes it in E2a — it
places no order. Promotion to real (worker-suppressed) placement is E2b+, a
separate, deployment-gated, sponsor-gated packet.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.observability import log_stage
from execution.models import (
    MAX_TOTAL_LOT_PER_SIGNAL,
    SIGNAL_ALLOWED_SYMBOLS,
    SIGNAL_MAX_AGE_SECONDS,
    SIGNAL_MAX_LOT_SIZE,
    ExecutionControl,
    ExecutionJob,
    ProposedOrderLeg,
    PromotionAuditEvent,
    SignalExecutionPlan,
    SignalSourceConfig,
    order_creation_kill_reason,
)


class PromotionRejected(Exception):
    """Raised when a plan cannot be promoted. A PROMOTION_REJECTED audit row is
    written first and persists (the reject path runs in autocommit)."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _audit(event, *, plan=None, leg=None, job=None, approval=None, actor=None, **detail):
    PromotionAuditEvent.objects.create(
        event=event, plan=plan, leg=leg, job=job, approval=approval, actor=actor, detail=detail
    )


def _shadow_payload(plan: SignalExecutionPlan, leg: ProposedOrderLeg) -> dict:
    """Build the suppressed shadow-job payload. Market-only, demo, SHADOW-flagged."""
    windows_username = None
    acct = plan.account
    if getattr(acct, "mt5_instance_id", None):
        windows_username = getattr(acct.mt5_instance, "windows_username", None)
    return {
        "symbol": plan.symbol,
        "side": plan.direction,
        "lots": str(leg.lot_size),
        "sl_price": plan.stop_loss,
        "tp_price": leg.take_profit,
        "entry_price": None,  # market order
        "is_demo": plan.is_demo,
        "execution_mode": "SHADOW",  # fail-closed suppression flag
        "comment": f"WAY{plan.id}L{leg.leg_index}",  # correlation tag
        "plan_id": plan.id,
        "leg_index": leg.leg_index,
        # OPS-OBSERVABILITY: propagate the correlation id into the job payload so
        # the worker can log stages 5-9 under the same id.
        "correlation_id": plan.correlation_id,
        "windows_username": windows_username,
    }


def _existing_shadow_jobs(plan: SignalExecutionPlan) -> list:
    return [leg.execution_job for leg in plan.legs.order_by("leg_index") if leg.execution_job_id]


def _validate(plan: SignalExecutionPlan, *, now) -> None:
    """Pure read-only re-validation at promotion time. Raises PromotionRejected."""
    if plan.status != SignalExecutionPlan.Status.PLANNED:
        raise PromotionRejected("plan_not_planned", f"plan #{plan.id} is {plan.status}, not PLANNED")

    if ExecutionControl.get_solo().signal_execution_mode != ExecutionControl.SignalExecutionMode.SHADOW:
        raise PromotionRejected("execution_mode_not_shadow", "global signal_execution_mode is not SHADOW")

    blocked = order_creation_kill_reason()
    if blocked:
        raise PromotionRejected(blocked, "execution control blocks promotion")

    cfg = SignalSourceConfig.objects.filter(source=plan.source).first()
    if cfg is None or not cfg.auto_demo_execution_enabled:
        raise PromotionRejected("source_not_enabled", f"source {plan.source} is not enabled for auto-demo")

    if not plan.account.is_demo:
        raise PromotionRejected("account_not_demo", "promotion is demo-only")
    if plan.account.broker_server_id:
        env = (plan.account.broker_server.environment or "")
        if env.lower() == "live":
            raise PromotionRejected("account_live", "live accounts are not permitted")

    if plan.symbol not in SIGNAL_ALLOWED_SYMBOLS:
        raise PromotionRejected("symbol_not_allowed", f"{plan.symbol} not in {SIGNAL_ALLOWED_SYMBOLS}")

    if not plan.stop_loss:
        raise PromotionRejected("missing_stop_loss", "plan has no stop loss")

    legs = list(plan.legs.order_by("leg_index"))
    if not legs:
        raise PromotionRejected("no_legs", f"plan #{plan.id} has no legs")
    if plan.signal_timestamp is None:
        raise PromotionRejected("no_signal_timestamp", "plan has no signal timestamp")
    if (now - plan.signal_timestamp).total_seconds() > SIGNAL_MAX_AGE_SECONDS:
        raise PromotionRejected("stale_signal", "signal aged out since planning")

    cap = Decimal(str(SIGNAL_MAX_LOT_SIZE))
    total = Decimal("0")
    for leg in legs:
        if not leg.take_profit:
            raise PromotionRejected("missing_take_profit", f"leg {leg.leg_index} has no TP")
        if leg.lot_size <= 0 or leg.lot_size > cap:
            raise PromotionRejected("lot_out_of_range", f"leg {leg.leg_index} lot {leg.lot_size} > {cap}")
        total += leg.lot_size
    if total > Decimal(MAX_TOTAL_LOT_PER_SIGNAL):
        raise PromotionRejected("total_lot_exceeds_cap", f"total {total} > {MAX_TOTAL_LOT_PER_SIGNAL}")


def promote_plan_to_shadow_jobs(plan: SignalExecutionPlan, *, actor=None, now=None) -> list:
    """Promote a PLANNED plan into one PLACE_ORDER_SHADOW job per leg.

    Creates suppressed, un-claimable shadow jobs ONLY — no order, no MT5, no
    executable PLACE_ORDER. Idempotent: a PROMOTED plan returns its existing
    shadow jobs and creates none. On any safety failure a PROMOTION_REJECTED
    audit is written and PromotionRejected is raised.
    """
    now = now or timezone.now()

    # 0. Idempotency — an already-promoted plan returns its existing shadow jobs.
    if plan.status == SignalExecutionPlan.Status.PROMOTED:
        return _existing_shadow_jobs(plan)

    try:
        _validate(plan, now=now)
    except PromotionRejected as exc:
        _audit(PromotionAuditEvent.Event.PROMOTION_REJECTED, plan=plan,
               approval=plan.approval, actor=actor, code=exc.code, message=exc.message)
        raise

    legs = list(plan.legs.order_by("leg_index"))
    try:
        with transaction.atomic():
            jobs = []
            for leg in legs:
                if leg.execution_job_id:  # belt: never double-create for a leg
                    jobs.append(leg.execution_job)
                    continue
                job = ExecutionJob.objects.create(
                    job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW,
                    account=plan.account,
                    terminal_node_id=plan.account.terminal_node_id,
                    status=ExecutionJob.Status.PENDING,  # un-claimable: distinct type + endpoint guard
                    created_by=actor,
                    payload=_shadow_payload(plan, leg),
                )
                leg.execution_job = job
                leg.status = ProposedOrderLeg.Status.PROMOTED
                leg.save(update_fields=["execution_job", "status"])
                _audit(PromotionAuditEvent.Event.JOB_CREATED, plan=plan, leg=leg, job=job,
                       approval=plan.approval, actor=actor, leg_index=leg.leg_index,
                       job_type=job.job_type, execution_mode="SHADOW")
                log_stage("shadow_job_created", plan.correlation_id, plan_id=plan.id,
                          job_id=job.id, leg_index=leg.leg_index, job_type=job.job_type)
                jobs.append(job)

            plan.status = SignalExecutionPlan.Status.PROMOTED
            plan.save(update_fields=["status"])
            _audit(PromotionAuditEvent.Event.PROMOTION_CREATED, plan=plan,
                   approval=plan.approval, actor=actor, jobs=len(jobs))
    except IntegrityError:
        plan.refresh_from_db()
        if plan.status == SignalExecutionPlan.Status.PROMOTED:
            return _existing_shadow_jobs(plan)
        raise PromotionRejected("duplicate_promotion", f"plan #{plan.id} already promoted")

    return jobs
