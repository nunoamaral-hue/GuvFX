"""
Central risk gating and runtime state helpers for GuvFX strategy engines.

All engines (TBP, ALTS, SCE) call check_risk_gates() before generating a signal.
Fail-open audit: if event recording fails, signals are NOT blocked.

Reason codes (PART G strict enum) are defined as module-level constants.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from strategies.models import (
    StrategyAssignment,
    StrategyRuntimeEvent,
    StrategyRuntimeState,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reason codes (PART G strict enum)
# ---------------------------------------------------------------------------

STAGE_NOT_LIVE = "STAGE_NOT_LIVE"
PAUSED = "PAUSED"
DAILY_LOSS_CAP = "DAILY_LOSS_CAP"
WEEKLY_LOSS_CAP = "WEEKLY_LOSS_CAP"
LOSS_STREAK_PAUSE = "LOSS_STREAK_PAUSE"
NO_TRADE_WINDOW = "NO_TRADE_WINDOW"
DATA_MISSING = "DATA_MISSING"
REGIME_NOT_RANGE = "REGIME_NOT_RANGE"
NO_BIAS = "NO_BIAS"
NO_BOS = "NO_BOS"
BOS_CONFLICTS_BIAS = "BOS_CONFLICTS_BIAS"
NO_PULLBACK = "NO_PULLBACK"
NO_REJECTION = "NO_REJECTION"
SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
SHOCK_CANDLE_PAUSE = "SHOCK_CANDLE_PAUSE"
POSITION_OPEN = "POSITION_OPEN"
NO_SIGNAL = "NO_SIGNAL"
MIN_STOP_VIOLATION = "MIN_STOP_VIOLATION"
DAILY_TRADE_CAP = "DAILY_TRADE_CAP"
VOLUME_ZERO = "VOLUME_ZERO"
ORDER_FAIL = "ORDER_FAIL"
ORDER_PLACED = "ORDER_PLACED"


# ---------------------------------------------------------------------------
# Daily / weekly counter resets
# ---------------------------------------------------------------------------

def reset_daily_counters_if_needed(
    state: StrategyRuntimeState,
    today: Optional[date] = None,
) -> bool:
    """
    Reset daily_r_pnl and daily_trade_count if daily_reset_date != today.

    Returns True if a reset occurred.
    Does NOT call state.save() — caller is responsible for saving.
    """
    today = today or timezone.now().date()
    if state.daily_reset_date == today:
        return False

    state.daily_r_pnl = Decimal("0")
    state.daily_trade_count = 0
    state.daily_reset_date = today
    return True


def reset_weekly_counters_if_needed(
    state: StrategyRuntimeState,
    today: Optional[date] = None,
) -> bool:
    """
    Reset weekly_r_pnl if the current week's Monday differs from weekly_reset_date.

    Returns True if a reset occurred.
    Does NOT call state.save() — caller is responsible for saving.
    """
    today = today or timezone.now().date()
    # Monday of the current week (Monday=0)
    current_monday = today - timedelta(days=today.weekday())

    if state.weekly_reset_date == current_monday:
        return False

    state.weekly_r_pnl = Decimal("0")
    state.weekly_reset_date = current_monday
    return True


# ---------------------------------------------------------------------------
# Risk gate checks
# ---------------------------------------------------------------------------

def check_risk_gates(
    assignment: StrategyAssignment,
    strategy_key: str,
    symbol: str,
    config_risk_limits: Dict[str, Any],
    now_ts: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """
    Check all risk gates for a signal.

    Parameters
    ----------
    assignment : StrategyAssignment
    strategy_key : template slug (e.g. "adaptive-liquidity-trap-scalper")
    symbol : e.g. "EURUSD"
    config_risk_limits : dict with optional keys:
        daily_loss_cap_r (float), daily_trade_cap (int),
        weekly_loss_cap_r (float), consecutive_loss_pause (int),
        cooldown_minutes (int), max_concurrent_positions (int),
        no_trade_hours (list of int hours 0-23)
    now_ts : current timestamp (default: timezone.now())

    Returns
    -------
    (allowed: bool, reason_code: str)
        If allowed is True, reason_code will be "" (empty).
        If allowed is False, reason_code is the first failing gate.
    """
    now_ts = now_ts or timezone.now()
    today = now_ts.date()

    # Get or create runtime state
    state, _created = StrategyRuntimeState.objects.get_or_create(
        assignment=assignment,
        strategy_key=strategy_key,
        symbol=symbol,
        defaults={
            "daily_reset_date": today,
            "weekly_reset_date": today - timedelta(days=today.weekday()),
        },
    )

    # Auto-reset daily/weekly counters
    daily_reset = reset_daily_counters_if_needed(state, today)
    weekly_reset = reset_weekly_counters_if_needed(state, today)
    if daily_reset or weekly_reset:
        save_fields = []
        if daily_reset:
            save_fields += ["daily_r_pnl", "daily_trade_count", "daily_reset_date"]
        if weekly_reset:
            save_fields += ["weekly_r_pnl", "weekly_reset_date"]
        state.save(update_fields=save_fields)

    # 1. Pause / cooldown
    if state.paused_until and now_ts < state.paused_until:
        return False, PAUSED

    # Clear expired pause
    if state.paused_until and now_ts >= state.paused_until:
        state.paused_until = None
        state.pause_reason = ""
        state.save(update_fields=["paused_until", "pause_reason"])

    # 2. Daily R loss cap
    daily_cap = config_risk_limits.get("daily_loss_cap_r")
    if daily_cap is not None:
        if state.daily_r_pnl <= Decimal(str(-abs(daily_cap))):
            return False, DAILY_LOSS_CAP

    # 3. Daily trade cap
    trade_cap = config_risk_limits.get("daily_trade_cap")
    if trade_cap is not None:
        if state.daily_trade_count >= trade_cap:
            return False, DAILY_TRADE_CAP

    # 4. Weekly R loss cap
    weekly_cap = config_risk_limits.get("weekly_loss_cap_r")
    if weekly_cap is not None:
        if state.weekly_r_pnl <= Decimal(str(-abs(weekly_cap))):
            return False, WEEKLY_LOSS_CAP

    # 5. Consecutive loss pause
    max_consec = config_risk_limits.get("consecutive_loss_pause")
    cooldown_min = config_risk_limits.get("cooldown_minutes", 60)
    if max_consec is not None and state.consecutive_losses >= max_consec:
        # Initiate cooldown
        state.paused_until = now_ts + timedelta(minutes=cooldown_min)
        state.pause_reason = LOSS_STREAK_PAUSE
        state.save(update_fields=["paused_until", "pause_reason"])
        return False, LOSS_STREAK_PAUSE

    # 6. No-trade window (list of hours to skip, e.g. [22, 23, 0, 1])
    no_trade_hours = config_risk_limits.get("no_trade_hours")
    if no_trade_hours and now_ts.hour in no_trade_hours:
        return False, NO_TRADE_WINDOW

    # 7. Max concurrent positions (uses ExecutionJob pending count)
    max_concurrent = config_risk_limits.get("max_concurrent_positions")
    if max_concurrent is not None:
        try:
            from execution.models import ExecutionJob
            pending_count = ExecutionJob.objects.filter(
                account=assignment.account,
                strategy=assignment.strategy,
                job_type="PLACE_ORDER",
                status__in=["PENDING", "IN_PROGRESS"],
                payload__symbol=symbol,
            ).count()
            if pending_count >= max_concurrent:
                return False, POSITION_OPEN
        except Exception as e:
            logger.warning("[RISK] Concurrent position check failed (fail-open): %s", e)

    return True, ""


# ---------------------------------------------------------------------------
# Event recording (fail-open)
# ---------------------------------------------------------------------------

def record_signal_event(
    assignment: StrategyAssignment,
    strategy_key: str,
    symbol: str,
    event_type: str,
    reason_code: str = "",
    payload: Optional[Dict[str, Any]] = None,
    bar_close_time: str = "",
) -> Optional[StrategyRuntimeEvent]:
    """
    Record a StrategyRuntimeEvent (append-only audit).

    Fail-open: if recording fails, logs the error and returns None.
    Signals are never blocked by audit failures.
    """
    try:
        event = StrategyRuntimeEvent.objects.create(
            assignment=assignment,
            strategy_key=strategy_key,
            symbol=symbol,
            event_type=event_type,
            reason_code=reason_code,
            payload=payload or {},
            bar_close_time=bar_close_time,
        )
        return event
    except Exception as e:
        logger.error(
            "[RISK] Failed to record event (fail-open): "
            "assignment=%s key=%s symbol=%s type=%s reason=%s error=%s",
            assignment.id, strategy_key, symbol, event_type, reason_code, e,
        )
        return None


# ---------------------------------------------------------------------------
# Runtime state updates
# ---------------------------------------------------------------------------

def update_runtime_state(
    assignment: StrategyAssignment,
    strategy_key: str,
    symbol: str,
    **kwargs,
) -> StrategyRuntimeState:
    """
    Get-or-create a StrategyRuntimeState and update fields atomically.

    Uses select_for_update to prevent race conditions in concurrent
    scheduler invocations.

    kwargs can include any field on StrategyRuntimeState:
        last_eval_at, paused_until, pause_reason, regime_blob,
        daily_r_pnl, daily_trade_count, consecutive_losses, etc.

    Returns the updated state instance.
    """
    today = timezone.now().date()
    with transaction.atomic():
        state, _created = StrategyRuntimeState.objects.select_for_update().get_or_create(
            assignment=assignment,
            strategy_key=strategy_key,
            symbol=symbol,
            defaults={
                "daily_reset_date": today,
                "weekly_reset_date": today - timedelta(days=today.weekday()),
            },
        )

        update_fields = []
        for field, value in kwargs.items():
            if hasattr(state, field):
                setattr(state, field, value)
                update_fields.append(field)
            else:
                logger.warning(
                    "[RISK] Unknown field '%s' in update_runtime_state", field
                )

        if update_fields:
            state.save(update_fields=update_fields)

    return state


def increment_daily_trade_count(
    assignment: StrategyAssignment,
    strategy_key: str,
    symbol: str,
) -> StrategyRuntimeState:
    """
    Atomically increment daily_trade_count for an assignment+key+symbol.

    Also auto-resets counters if the day has changed.
    """
    today = timezone.now().date()
    with transaction.atomic():
        state, _created = StrategyRuntimeState.objects.select_for_update().get_or_create(
            assignment=assignment,
            strategy_key=strategy_key,
            symbol=symbol,
            defaults={
                "daily_reset_date": today,
                "weekly_reset_date": today - timedelta(days=today.weekday()),
            },
        )
        reset_daily_counters_if_needed(state, today)
        state.daily_trade_count += 1
        state.last_eval_at = timezone.now()
        state.save(update_fields=[
            "daily_trade_count", "daily_r_pnl", "daily_reset_date", "last_eval_at",
        ])

    return state
