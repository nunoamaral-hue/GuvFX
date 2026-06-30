"""
EXEC-E1b — APPROVED signal → non-executable multi-leg demo PLAN.

HARD BOUNDARY — this module creates ``SignalExecutionPlan`` + ``ProposedOrderLeg``
rows ONLY. It NEVER:

  * creates an ``ExecutionJob`` (or any worker-claimable PENDING job),
  * calls ``create_place_order_job`` / ``create_open_trade_job`` / ``order_send``,
  * contacts MT5 or the Windows agent, or makes any network call,
  * uses broker credentials,
  * activates a Telegram listener.

A plan/leg is structurally invisible to the worker claim path
(``ExecutionJob.objects.filter(status=PENDING)``), so no order can ever result.
Promotion of a plan to executable (worker-suppressed) jobs is a separate,
sponsor-gated packet (E2+).

Boundary direction: ``execution`` reads ``signal_intake`` (one-way).
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from execution.models import (
    LOT_STEP,
    MAX_PLAN_LEGS,
    MAX_TOTAL_LOT_PER_SIGNAL,
    PLAN_MAX_CONCURRENT_GROUPS,
    PLAN_MAX_GROUPS_PER_DAY,
    SIGNAL_ALLOWED_SYMBOLS,
    SIGNAL_MAX_AGE_SECONDS,
    SIGNAL_MAX_LOT_SIZE,
    PlanAuditEvent,
    ProposedOrderLeg,
    SignalExecutionPlan,
    SignalSourceConfig,
    order_creation_kill_reason,
)
from signal_intake.models import PendingSignalApproval

_STEP = Decimal(LOT_STEP)
_MAX_PER_LEG = Decimal(str(SIGNAL_MAX_LOT_SIZE))
_MAX_TOTAL = Decimal(MAX_TOTAL_LOT_PER_SIGNAL)


class PlanRejected(Exception):
    """Raised when no plan should be created (policy/config failure). A PLAN_HELD
    audit row (plan=None) is written first and persists."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class VolumeSplitError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Deterministic volume split
# ---------------------------------------------------------------------------


def split_volume(total_lot, n, *, lot_step=_STEP, max_per_leg=_MAX_PER_LEG, max_total=_MAX_TOTAL):
    """Split ``total_lot`` across ``n`` legs deterministically.

    Rule: equal split floored to the broker lot step, with the rounding
    remainder assigned to the earliest leg(s) (leg 1 first). The effective total
    is first capped to ``min(total, n*max_per_leg, max_total)`` so every leg can
    stay within the per-leg cap. Returns ``(legs, meta)``; raises
    ``VolumeSplitError`` if the total cannot give every leg at least one lot step
    or a cap would be breached.
    """
    if n < 1 or n > MAX_PLAN_LEGS:
        raise VolumeSplitError("invalid_leg_count", f"n={n} out of 1..{MAX_PLAN_LEGS}")
    try:
        requested = Decimal(str(total_lot))
    except (InvalidOperation, ValueError, TypeError):
        raise VolumeSplitError("invalid_total", f"total_lot {total_lot!r} not a number")
    if requested <= 0:
        raise VolumeSplitError("invalid_total", f"total_lot {requested} must be positive")

    effective = min(requested, n * max_per_leg, max_total)
    total_units = int((effective / lot_step).to_integral_value(rounding=ROUND_DOWN))
    if total_units < n:
        raise VolumeSplitError(
            "insufficient_total_lot",
            f"effective total {effective} cannot give {n} legs >= {lot_step}",
        )

    base, rem = divmod(total_units, n)
    legs = []
    for i in range(n):
        units = base + (1 if i < rem else 0)  # remainder to the earliest legs
        lot = (Decimal(units) * lot_step).quantize(lot_step)
        if lot > max_per_leg:
            raise VolumeSplitError("leg_exceeds_cap", f"leg {i + 1} lot {lot} > {max_per_leg}")
        legs.append(lot)

    if sum(legs) > max_total:
        raise VolumeSplitError("total_exceeds_cap", f"sum {sum(legs)} > {max_total}")
    return legs, {"effective_total": str(effective), "capped": effective < requested}


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _audit(event, *, plan=None, leg=None, approval=None, actor=None, **detail):
    PlanAuditEvent.objects.create(
        event=event, plan=plan, leg=leg, approval=approval, actor=actor, detail=detail
    )


def _signal_timestamp(approval, override):
    if override is not None:
        return override
    raw = approval.raw_payload or {}
    for key in ("signal_timestamp", "date", "timestamp"):
        val = raw.get(key)
        if isinstance(val, str):
            parsed = parse_datetime(val)
            if parsed is not None:
                return parsed
    return approval.created_at


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


def plan_demo_execution(
    approval: PendingSignalApproval,
    *,
    account,
    actor=None,
    now=None,
    signal_timestamp=None,
    total_lot=None,
) -> SignalExecutionPlan:
    """Build a non-executable demo execution plan from an APPROVED signal.

    Creates NO ``ExecutionJob`` and places NO order. Returns a plan in PLANNED
    (with legs), HELD (data issue, no legs), or VOIDED (stale, no legs) state.
    Policy/config failures raise ``PlanRejected`` (no plan created).
    """
    now = now or timezone.now()
    chat_id = str((approval.raw_payload or {}).get("chat_id", ""))
    source = approval.source
    message_id = approval.message_id

    # 0. Idempotency — one plan per approval / (source, chat, message).
    existing = SignalExecutionPlan.objects.filter(approval=approval).first()
    if existing:
        return existing

    def _reject(code, message):
        _audit(PlanAuditEvent.Event.PLAN_HELD, approval=approval, actor=actor,
               code=code, message=message, account_id=getattr(account, "id", None))
        raise PlanRejected(code, message)

    # 1. Approved-only.
    if approval.status != PendingSignalApproval.Status.APPROVED:
        _reject("approval_not_approved", f"approval #{approval.id} is {approval.status}")

    # 2. Kill switch (env + DB) — fail closed.
    blocked = order_creation_kill_reason()
    if blocked:
        _reject(blocked, "execution control blocks planning")

    # 3. Source must be explicitly enabled for auto-demo.
    cfg = SignalSourceConfig.objects.filter(source=source).first()
    if cfg is None or not cfg.auto_demo_execution_enabled:
        _reject("source_not_enabled", f"source {source} is not enabled for auto-demo")

    # 4. Demo-only.
    if not account.is_demo:
        _reject("account_not_demo", "planning is demo-only")
    env = ""
    if account.broker_server_id:
        env = account.broker_server.environment or ""
        if env.lower() == "live":
            _reject("account_live", "live accounts are not permitted")

    # 5. Symbol allowlist.
    symbol = (approval.symbol or "").upper()
    direction = (approval.direction or "").upper()
    if not symbol or direction not in (
        SignalExecutionPlan.Direction.BUY, SignalExecutionPlan.Direction.SELL
    ):
        _reject("not_tradeable", "approval lacks a usable symbol/direction")
    if symbol not in SIGNAL_ALLOWED_SYMBOLS:
        _reject("symbol_not_allowed", f"{symbol} not in {SIGNAL_ALLOWED_SYMBOLS}")

    # 6. Per-signal-group caps (count GROUPS/plans, not legs).
    if SignalExecutionPlan.count_today(account.id, symbol) >= PLAN_MAX_GROUPS_PER_DAY:
        _reject("daily_limit_exceeded", f"daily group limit reached ({PLAN_MAX_GROUPS_PER_DAY})")
    if SignalExecutionPlan.count_active(account.id, symbol) >= PLAN_MAX_CONCURRENT_GROUPS:
        _reject("concurrent_limit_exceeded",
                f"concurrent group limit reached ({PLAN_MAX_CONCURRENT_GROUPS})")

    sig_ts = _signal_timestamp(approval, signal_timestamp)
    common = dict(
        approval=approval, account=account, source=source, chat_id=chat_id,
        message_id=message_id, symbol=symbol, direction=direction,
        entry=approval.entry or "", stop_loss=approval.stop_loss or "",
        is_demo=account.is_demo, account_environment=env, signal_timestamp=sig_ts,
        proposed_by=actor,
    )

    # 7. Reject/hold if SL or required TP is missing → HELD plan, no legs.
    tps = [t for t in (approval.take_profits or []) if t]
    if not approval.stop_loss:
        return _hold(common, actor, "missing_stop_loss")
    if not tps:
        return _hold(common, actor, "missing_take_profit")

    # 8. Staleness → VOIDED plan, no legs.
    age = (now - sig_ts).total_seconds()
    if age > SIGNAL_MAX_AGE_SECONDS:
        return _void(common, actor, "stale_signal", age_seconds=age)

    # 9. Deterministic split + volume allocation.
    n = min(len(tps), MAX_PLAN_LEGS)
    configured_total = Decimal(str(total_lot)) if total_lot is not None else cfg.total_lot_target
    try:
        leg_lots, split_meta = split_volume(configured_total, n)
    except VolumeSplitError as exc:
        return _hold(common, actor, "volume_split_invalid", detail=exc.message)

    # 10. Create the PLANNED plan + legs (the only success path).
    try:
        with transaction.atomic():
            plan = SignalExecutionPlan.objects.create(
                **common, order_type="MARKET",
                total_lot=sum(leg_lots), status=SignalExecutionPlan.Status.PLANNED,
            )
            _audit(PlanAuditEvent.Event.PLAN_CREATED, plan=plan, approval=approval,
                   actor=actor, symbol=symbol, direction=direction, legs=n,
                   total_lot=str(sum(leg_lots)), split=split_meta)
            for idx, (tp, lot) in enumerate(zip(tps[:n], leg_lots), start=1):
                leg = ProposedOrderLeg.objects.create(
                    plan=plan, leg_index=idx, take_profit=tp,
                    stop_loss=approval.stop_loss or "", lot_size=lot, order_type="MARKET",
                )
                _audit(PlanAuditEvent.Event.LEG_CREATED, plan=plan, leg=leg,
                       approval=approval, actor=actor, leg_index=idx,
                       take_profit=tp, lot_size=str(lot))
    except IntegrityError:
        existing = SignalExecutionPlan.objects.filter(approval=approval).first()
        if existing:
            return existing
        raise PlanRejected("duplicate_plan", f"approval #{approval.id} already planned")

    return plan


def _hold(common, actor, reason, *, detail="") -> SignalExecutionPlan:
    with transaction.atomic():
        plan = SignalExecutionPlan.objects.create(
            **common, order_type="MARKET", total_lot=Decimal("0.00"),
            status=SignalExecutionPlan.Status.HELD, hold_reason=reason,
        )
        _audit(PlanAuditEvent.Event.PLAN_HELD, plan=plan, approval=common["approval"],
               actor=actor, reason=reason, detail=detail)
    return plan


def _void(common, actor, reason, **extra) -> SignalExecutionPlan:
    with transaction.atomic():
        plan = SignalExecutionPlan.objects.create(
            **common, order_type="MARKET", total_lot=Decimal("0.00"),
            status=SignalExecutionPlan.Status.VOIDED, hold_reason=reason,
        )
        _audit(PlanAuditEvent.Event.PLAN_VOIDED, plan=plan, approval=common["approval"],
               actor=actor, reason=reason, **extra)
    return plan


# ---------------------------------------------------------------------------
# Source config helper (per-source arming)
# ---------------------------------------------------------------------------


def set_source_enabled(source: str, enabled: bool, *, actor=None, total_lot_target=None):
    cfg, _ = SignalSourceConfig.objects.get_or_create(source=source)
    cfg.auto_demo_execution_enabled = bool(enabled)
    if total_lot_target is not None:
        cfg.total_lot_target = Decimal(str(total_lot_target))
    cfg.updated_by = actor
    cfg.save()
    return cfg
