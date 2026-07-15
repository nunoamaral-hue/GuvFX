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
from execution.risk_controls import evaluate_promotion_risk
from execution.broker_symbols import can_account_trade_symbol
from execution.models import (
    MAX_TOTAL_LOT_PER_SIGNAL,
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


def _order_payload(plan: SignalExecutionPlan, leg: ProposedOrderLeg, *,
                   execution_mode: str, broker_symbol: str = None) -> dict:
    """Build the per-leg order payload. Market-only, demo. ``execution_mode`` is 'SHADOW'
    (suppressed dry-run — order_check only) or 'DEMO' (real order_send). Identical shape either
    way — the worker's PLACE_ORDER path ignores execution_mode; only the shadow worker enforces
    it. Carries the correlation id + signal timestamp so the worker can trace/re-check the signal.

    ``broker_symbol`` (resolved by the broker-symbol registry) is what the order is placed under;
    the original provider (Wayond) symbol is preserved separately for audit/reporting.
    """
    windows_username = None
    acct = plan.account
    if getattr(acct, "mt5_instance_id", None):
        windows_username = getattr(acct.mt5_instance, "windows_username", None)
    return {
        "symbol": broker_symbol or plan.symbol,   # the BROKER symbol used for order placement
        "provider_symbol": plan.symbol,           # the Wayond signal symbol (audit/reporting)
        "side": plan.direction,
        "lots": str(leg.lot_size),
        "sl_price": plan.stop_loss,
        "tp_price": leg.take_profit,
        "entry_price": None,  # market order (advisory signal entry is reference-only)
        "is_demo": plan.is_demo,
        "execution_mode": execution_mode,  # 'SHADOW' suppresses; 'DEMO' is a real order
        "comment": f"WAY{plan.id}L{leg.leg_index}",  # correlation tag (short — no MT5 truncation)
        "plan_id": plan.id,
        "leg_index": leg.leg_index,
        # SOURCE-SCOPED SIZING — the provider identity + the per-source per-leg lot ceiling.
        # The worker + bridge (which cannot read the DB) admit this leg only up to ``max_lot``
        # (fail-closed to the conservative default for an unknown source), so a source can never
        # exceed its own configured size even at the outermost order gate.
        "signal_source": plan.source,
        "max_lot": str(SignalSourceConfig.sizing_caps(plan.source)[0]),
        # OPS-OBSERVABILITY: propagate the correlation id so the worker logs the same id, and
        # so a resulting Trade can be traced back to this signal/plan.
        "correlation_id": plan.correlation_id,
        # E3-RUNTIME-RISK-CONTROLS: carry the signal timestamp for a worker-side staleness re-check.
        "signal_timestamp": plan.signal_timestamp.isoformat() if plan.signal_timestamp else None,
        "windows_username": windows_username,
    }


def _shadow_payload(plan: SignalExecutionPlan, leg: ProposedOrderLeg) -> dict:
    """Back-compat: the suppressed shadow-job payload (SHADOW-flagged)."""
    return _order_payload(plan, leg, execution_mode="SHADOW")


def _existing_jobs(plan: SignalExecutionPlan) -> list:
    return [leg.execution_job for leg in plan.legs.order_by("leg_index") if leg.execution_job_id]


# Back-compat alias (the shadow-specific name is used elsewhere).
_existing_shadow_jobs = _existing_jobs


def _validate(plan: SignalExecutionPlan, *, now,
              expected_mode=ExecutionControl.SignalExecutionMode.SHADOW,
              symbol_resolution=None) -> None:
    """Pure read-only re-validation at promotion time. Raises PromotionRejected.

    ``expected_mode`` is the global ``signal_execution_mode`` this promotion path requires —
    SHADOW for shadow jobs, DEMO for real demo orders. Every other gate is IDENTICAL, so the
    demo path inherits the exact same demo-only / risk / cap / staleness protections.
    """
    if plan.status != SignalExecutionPlan.Status.PLANNED:
        raise PromotionRejected("plan_not_planned", f"plan #{plan.id} is {plan.status}, not PLANNED")

    if ExecutionControl.get_solo().signal_execution_mode != expected_mode:
        raise PromotionRejected("execution_mode_mismatch",
                                f"global signal_execution_mode is not {expected_mode}")

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

    # Broker/account-aware symbol gate — the symbol must resolve to one the account's broker
    # offers (fail-closed with a specific reason). Provider symbol preserved; broker symbol used
    # for the order (in _order_payload).
    res = symbol_resolution or can_account_trade_symbol(plan.account, plan.symbol)
    if not res.accepted:
        raise PromotionRejected(res.reason, f"{plan.symbol}: {res.reason}")

    if not plan.stop_loss:
        raise PromotionRejected("missing_stop_loss", "plan has no stop loss")

    legs = list(plan.legs.order_by("leg_index"))
    if not legs:
        raise PromotionRejected("no_legs", f"plan #{plan.id} has no legs")
    if plan.signal_timestamp is None:
        raise PromotionRejected("no_signal_timestamp", "plan has no signal timestamp")
    if (now - plan.signal_timestamp).total_seconds() > SIGNAL_MAX_AGE_SECONDS:
        raise PromotionRejected("stale_signal", "signal aged out since planning")

    # Per-SOURCE sizing ceilings (fail-closed to the global 0.02/0.06 for an unknown source) —
    # re-validated at promotion, independent of the planning split, so a leg can never promote
    # above its source's configured size.
    cap, total_cap = SignalSourceConfig.sizing_caps(plan.source)
    total = Decimal("0")
    for leg in legs:
        if not leg.take_profit:
            raise PromotionRejected("missing_take_profit", f"leg {leg.leg_index} has no TP")
        if leg.lot_size <= 0 or leg.lot_size > cap:
            raise PromotionRejected("lot_out_of_range", f"leg {leg.leg_index} lot {leg.lot_size} > {cap}")
        total += leg.lot_size
    if total > total_cap:
        raise PromotionRejected("total_lot_exceeds_cap", f"total {total} > {total_cap}")

    # E3-RUNTIME-RISK-CONTROLS — pre-E3 runtime risk gates (exposure, max-open,
    # drawdown, concurrent). Fail-closed. Raises PromotionRejected (→ a persisted
    # PROMOTION_REJECTED audit) so every block decision is audited. Places no order.
    risk_reason = evaluate_promotion_risk(plan, legs)
    if risk_reason:
        raise PromotionRejected(risk_reason, f"runtime risk control blocked promotion: {risk_reason}")


def _promote_plan(plan: SignalExecutionPlan, *, expected_mode, job_type, payload_mode,
                  log_stage_name, actor, now) -> list:
    """Shared promotion: a PLANNED plan → one ``job_type`` job per leg.

    Idempotent (a PROMOTED plan returns its existing jobs); validates against ``expected_mode``;
    fail-closed (on any safety failure a PROMOTION_REJECTED audit is written and PromotionRejected
    is raised). The SHADOW and DEMO paths differ ONLY in ``job_type`` + ``payload_mode`` — all
    validation gates are identical.
    """
    now = now or timezone.now()

    # 0. Idempotency — an already-promoted plan returns its existing jobs.
    if plan.status == SignalExecutionPlan.Status.PROMOTED:
        return _existing_jobs(plan)

    # Resolve the broker symbol ONCE — reused by the gate (_validate) and the order payload.
    symbol_res = can_account_trade_symbol(plan.account, plan.symbol)
    try:
        _validate(plan, now=now, expected_mode=expected_mode, symbol_resolution=symbol_res)
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
                    job_type=job_type,
                    account=plan.account,
                    terminal_node_id=plan.account.terminal_node_id,
                    status=ExecutionJob.Status.PENDING,
                    created_by=actor,
                    payload=_order_payload(plan, leg, execution_mode=payload_mode,
                                           broker_symbol=symbol_res.broker_symbol),
                )
                leg.execution_job = job
                leg.status = ProposedOrderLeg.Status.PROMOTED
                leg.save(update_fields=["execution_job", "status"])
                _audit(PromotionAuditEvent.Event.JOB_CREATED, plan=plan, leg=leg, job=job,
                       approval=plan.approval, actor=actor, leg_index=leg.leg_index,
                       job_type=job.job_type, execution_mode=payload_mode)
                log_stage(log_stage_name, plan.correlation_id, plan_id=plan.id,
                          job_id=job.id, leg_index=leg.leg_index, job_type=job.job_type)
                jobs.append(job)

            plan.status = SignalExecutionPlan.Status.PROMOTED
            plan.save(update_fields=["status"])
            _audit(PromotionAuditEvent.Event.PROMOTION_CREATED, plan=plan,
                   approval=plan.approval, actor=actor, jobs=len(jobs))
    except IntegrityError:
        plan.refresh_from_db()
        if plan.status == SignalExecutionPlan.Status.PROMOTED:
            return _existing_jobs(plan)
        raise PromotionRejected("duplicate_promotion", f"plan #{plan.id} already promoted")

    return jobs


def promote_plan_to_shadow_jobs(plan: SignalExecutionPlan, *, actor=None, now=None) -> list:
    """Promote a PLANNED plan into one PLACE_ORDER_SHADOW job per leg — suppressed, un-claimable,
    NO order, NO MT5. Requires global mode SHADOW. Idempotent; fail-closed. (Unchanged behaviour.)
    """
    return _promote_plan(
        plan, expected_mode=ExecutionControl.SignalExecutionMode.SHADOW,
        job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW, payload_mode="SHADOW",
        log_stage_name="shadow_job_created", actor=actor, now=now,
    )


def promote_plan_to_demo_jobs(plan: SignalExecutionPlan, *, actor=None, now=None) -> list:
    """E3-DEMO-PROMOTION — promote a PLANNED plan into one real ``PLACE_ORDER`` job per leg on a
    DEMO account.

    The ONLY difference from the shadow path is ``job_type=PLACE_ORDER`` (a real order the worker
    will ``order_send``) instead of ``PLACE_ORDER_SHADOW``. It requires global mode **DEMO** — the
    default is SHADOW, so this NEVER runs unless an operator has explicitly flipped
    ``signal_execution_mode=DEMO`` (under Nuno's recorded sign-off) AND armed every other gate. It
    inherits the identical demo-only + symbol + SL/TP + lot-cap + staleness + runtime-risk +
    kill-switch validation, and the worker enforces the per-SOURCE lot cap (from the payload's
    ``max_lot``, fail-closed) and demo account. Idempotent; fail-closed.
    """
    return _promote_plan(
        plan, expected_mode=ExecutionControl.SignalExecutionMode.DEMO,
        job_type=ExecutionJob.JobType.PLACE_ORDER, payload_mode="DEMO",
        log_stage_name="demo_order_job_created", actor=actor, now=now,
    )
