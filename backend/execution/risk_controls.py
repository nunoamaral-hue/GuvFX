"""
GFX-PKT-E3-RUNTIME-RISK-CONTROLS — pre-E3 runtime risk gates (fail-closed).

Additive, shadow-testable risk controls evaluated at **promotion time** (before any
``PLACE_ORDER_SHADOW`` job is created) plus a runtime staleness re-check the worker
applies before it validates. These gates are the pre-condition for a future
demo-live path — they place NO order, call NO ``order_send``, and do NOT touch the
kill switch. Every evaluator is **fail-closed**: if risk state cannot be determined
(any exception), it returns a block reason rather than allowing the action.

Exposure counts BOTH paths on a shared account (see Blueprint doc 06): real open
``Trade`` positions AND in-flight signal exposure (legs of PROMOTED plans).

Controls:
  1. per-account exposure limit        (account_exposure)
  2. per-symbol exposure limit         (symbol_exposure)
  3. max open positions / active jobs  (max_open_positions)
  4. daily drawdown guard              (daily_drawdown)
  6. concurrent-position enforcement   (concurrent_positions)
(5 — runtime staleness re-check — lives in the worker; the signal_timestamp is
propagated into the shadow payload for it.)
"""

from __future__ import annotations

import os
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from execution.models import (
    MAX_TOTAL_LOT_PER_SIGNAL,
    PLAN_MAX_CONCURRENT_GROUPS,
    ExecutionJob,
    ProposedOrderLeg,
    SignalExecutionPlan,
)
from trading.models import Trade


def _dec_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(os.getenv(name, default)))
    except Exception:
        return Decimal(default)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


# Caps (env-overridable). Defaults sized so one within-spec signal (<= one
# MAX_TOTAL_LOT_PER_SIGNAL) on a clean account passes; concurrent/large exposure trips.
MAX_ACCOUNT_EXPOSURE_LOT = _dec_env("RISK_MAX_ACCOUNT_EXPOSURE_LOT", "0.10")
MAX_SYMBOL_EXPOSURE_LOT = _dec_env("RISK_MAX_SYMBOL_EXPOSURE_LOT", MAX_TOTAL_LOT_PER_SIGNAL)
MAX_OPEN_POSITIONS_PER_ACCOUNT = _int_env("RISK_MAX_OPEN_POSITIONS", 3)
MAX_DAILY_DRAWDOWN_ABS = _dec_env("RISK_MAX_DAILY_DRAWDOWN_ABS", "100.00")

_ORDER_OPENING_JOBS = (
    ExecutionJob.JobType.OPEN_TRADE,
    ExecutionJob.JobType.PLACE_ORDER,
    ExecutionJob.JobType.PLACE_ORDER_SHADOW,
)
_ACTIVE_PLAN_STATUSES = (
    SignalExecutionPlan.Status.PLANNED,
    SignalExecutionPlan.Status.PROMOTED,
)


# --- state helpers (each already fail-closed by the outer evaluator) ---------

def _open_position_lots(account_id, symbol=None) -> Decimal:
    """Sum of volumes of currently-open real positions (Trade.close_time is null)."""
    qs = Trade.objects.filter(account_id=account_id, close_time__isnull=True)
    if symbol:
        qs = qs.filter(symbol=symbol)
    return qs.aggregate(s=Sum("volume"))["s"] or Decimal("0")


def _active_signal_lots(account_id, symbol=None, exclude_plan_id=None) -> Decimal:
    """Sum of leg lots of in-flight signal exposure (legs of PROMOTED plans)."""
    qs = ProposedOrderLeg.objects.filter(
        plan__account_id=account_id, plan__status=SignalExecutionPlan.Status.PROMOTED
    )
    if symbol:
        qs = qs.filter(plan__symbol=symbol)
    if exclude_plan_id is not None:
        qs = qs.exclude(plan_id=exclude_plan_id)
    return qs.aggregate(s=Sum("lot_size"))["s"] or Decimal("0")


def _open_positions_count(account_id) -> int:
    """Open real positions + active (PENDING/RUNNING) order-opening jobs."""
    open_trades = Trade.objects.filter(account_id=account_id, close_time__isnull=True).count()
    active_jobs = ExecutionJob.objects.filter(
        account_id=account_id,
        status__in=(ExecutionJob.Status.PENDING, ExecutionJob.Status.RUNNING),
        job_type__in=_ORDER_OPENING_JOBS,
    ).count()
    return open_trades + active_jobs


def _today_realized_pnl(account_id) -> Decimal:
    """Sum of realized P&L on positions closed today (aware, local day)."""
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    qs = Trade.objects.filter(
        account_id=account_id, close_time__isnull=False, close_time__gte=start
    )
    return qs.aggregate(s=Sum("profit"))["s"] or Decimal("0")


# --- control evaluators (return a block reason code, or None) ----------------

def evaluate_promotion_risk(plan, legs) -> str | None:
    """Run all promotion-time risk controls. Returns the first block reason code
    (a stable string) or ``None`` if all pass. FAIL-CLOSED: any error blocks."""
    try:
        account_id = plan.account_id
        symbol = plan.symbol
        new_total = sum((leg.lot_size for leg in legs), Decimal("0"))

        # 1 + 2: exposure (shared budget: open positions + in-flight signal legs)
        acct_exposure = _open_position_lots(account_id) + _active_signal_lots(
            account_id, exclude_plan_id=plan.id
        )
        if acct_exposure + new_total > MAX_ACCOUNT_EXPOSURE_LOT:
            return "account_exposure_exceeded"

        sym_exposure = _open_position_lots(account_id, symbol) + _active_signal_lots(
            account_id, symbol, exclude_plan_id=plan.id
        )
        if sym_exposure + new_total > MAX_SYMBOL_EXPOSURE_LOT:
            return "symbol_exposure_exceeded"

        # 3: max open positions / active jobs
        if _open_positions_count(account_id) >= MAX_OPEN_POSITIONS_PER_ACCOUNT:
            return "max_open_positions_reached"

        # 4: daily drawdown guard
        if _today_realized_pnl(account_id) <= -MAX_DAILY_DRAWDOWN_ABS:
            return "daily_drawdown_hit"

        # 6: concurrent-position enforcement (other active plans, same account+symbol)
        other_active = (
            SignalExecutionPlan.objects.filter(
                account_id=account_id, symbol=symbol, status__in=_ACTIVE_PLAN_STATUSES
            )
            .exclude(id=plan.id)
            .count()
        )
        if other_active >= PLAN_MAX_CONCURRENT_GROUPS:
            return "concurrent_position_limit"

        return None
    except Exception:
        # FAIL CLOSED — indeterminate risk state must block, never allow.
        return "risk_state_indeterminate"
