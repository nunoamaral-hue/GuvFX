"""
Trendline Break Pocket (Ali) Signal Engine

This module implements the signal generation logic for the Trendline Break Pocket
strategy template (mp-005, slug: trendline-break-pocket-ali).

STRATEGY RULES:
1. HTF Zone: Price must be within a D1 supply/demand zone
2. Trendline Break: H4 close must break the trendline by entry_buffer_pips
3. Structure Shift: Swing break confirmation (close beyond swing high/low)
4. Pocket Entry: Optional retest into the pocket zone after break
5. Fixed 2R target from entry, stop at structural invalidation

SAFETY:
- Demo accounts only (is_demo=True)
- Symbols: EURUSD, GBPUSD only
- Max lots: 0.02 (hard cap)
- Max trades per day per account+strategy+symbol: 3
- Max concurrent positions per symbol: 1

NOTE: This is a simplified implementation for the MVP. The signal evaluation
currently uses a placeholder that generates signals based on basic price
structure analysis. Full implementation requires H4 OHLC data feed.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.http import HttpRequest

from execution.models import (
    ExecutionJob,
    SIGNAL_ALLOWED_SYMBOLS,
    SIGNAL_MAX_LOT_SIZE,
    SIGNAL_MAX_TRADES_PER_DAY,
    SIGNAL_MAX_CONCURRENT_POSITIONS,
)
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount, Trade
from core.audit import log_signal_evaluated, log_signal_rejected, log_signal_created

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class SignalResult:
    """Result of signal evaluation."""

    ok: bool
    signal_type: Optional[str] = None  # "BUY" or "SELL" or None
    symbol: str = ""
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    lots: Optional[float] = None
    reason: str = ""
    job_id: Optional[int] = None
    details: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "signal_type": self.signal_type,
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "lots": self.lots,
            "reason": self.reason,
            "job_id": self.job_id,
            "details": self.details or {},
        }


@dataclass
class TrendlineBreakPocketConfig:
    """Configuration extracted from strategy filters for TBP strategy."""

    enabled: bool = True
    direction_mode: str = "both"  # "both", "long", "short"
    pairs_enabled: list = None
    htf_timeframe: str = "D1"
    execution_timeframe: str = "H4"
    rr_target: float = 2.0
    trendline_lookback_bars: int = 101
    trendline_pivot_strength: int = 2
    break_confirm_bars: int = 1
    swing_break_mode: str = "close_break"
    swing_lookback: int = 7
    pocket_retest_required: bool = True
    entry_buffer_pips: dict = None
    overshoot_max_pips: dict = None
    clean_air_min_pips: dict = None
    max_trades_per_day: int = 1
    max_concurrent_positions: int = 1
    news_filter_mode: str = "major_only"
    zones: dict = None

    def __post_init__(self):
        if self.pairs_enabled is None:
            self.pairs_enabled = ["EURUSD", "GBPUSD"]
        if self.entry_buffer_pips is None:
            self.entry_buffer_pips = {"EURUSD": 2, "GBPUSD": 3}
        if self.overshoot_max_pips is None:
            self.overshoot_max_pips = {"EURUSD": 12, "GBPUSD": 18}
        if self.clean_air_min_pips is None:
            self.clean_air_min_pips = {"EURUSD": 8, "GBPUSD": 12}
        if self.zones is None:
            self.zones = {}

    @classmethod
    def from_filters(cls, filters: dict) -> "TrendlineBreakPocketConfig":
        """Create config from strategy filters JSON."""
        return cls(
            enabled=filters.get("enabled", True),
            direction_mode=filters.get("direction_mode", "both"),
            pairs_enabled=filters.get("pairs_enabled", ["EURUSD", "GBPUSD"]),
            htf_timeframe=filters.get("htf_timeframe", "D1"),
            execution_timeframe=filters.get("execution_timeframe", "H4"),
            rr_target=filters.get("rr_target", 2.0),
            trendline_lookback_bars=filters.get("trendline_lookback_bars", 101),
            trendline_pivot_strength=filters.get("trendline_pivot_strength", 2),
            break_confirm_bars=filters.get("break_confirm_bars", 1),
            swing_break_mode=filters.get("swing_break_mode", "close_break"),
            swing_lookback=filters.get("swing_lookback", 7),
            pocket_retest_required=filters.get("pocket_retest_required", True),
            entry_buffer_pips=filters.get("entry_buffer_pips", {"EURUSD": 2, "GBPUSD": 3}),
            overshoot_max_pips=filters.get("overshoot_max_pips", {"EURUSD": 12, "GBPUSD": 18}),
            clean_air_min_pips=filters.get("clean_air_min_pips", {"EURUSD": 8, "GBPUSD": 12}),
            max_trades_per_day=filters.get("max_trades_per_day", 1),
            max_concurrent_positions=filters.get("max_concurrent_positions", 1),
            news_filter_mode=filters.get("news_filter_mode", "major_only"),
            zones=filters.get("zones", {}),
        )


# =============================================================================
# Safety Validation
# =============================================================================


def validate_signal_safety(
    strategy: Strategy,
    account: TradingAccount,
    assignment: StrategyAssignment,
    symbol: str,
    config: TrendlineBreakPocketConfig,
) -> tuple[bool, str]:
    """
    Validate all safety rails before allowing signal execution.

    Returns (is_valid, error_reason).
    """
    # 1. Strategy must be enabled
    if not config.enabled:
        return False, "strategy_disabled"

    # 2. Account must be demo (hard requirement for MVP)
    if not account.is_demo:
        return False, "account_not_demo"

    # 3. Account must be active
    if not account.is_active:
        return False, "account_not_active"

    # 4. Strategy must be active
    if not strategy.is_active:
        return False, "strategy_not_active"

    # 5. Assignment must be active
    if not assignment.is_active:
        return False, "assignment_not_active"

    # 6. Symbol must be in strategy's pairs_enabled
    if symbol not in config.pairs_enabled:
        return False, f"symbol_not_enabled:{symbol}"

    # 7. Symbol must be in global allowed list
    if symbol not in SIGNAL_ALLOWED_SYMBOLS:
        return False, f"symbol_not_allowed:{symbol}"

    # 8. Zones must exist for symbol and be well-formed
    symbol_zones = config.zones.get(symbol, [])
    if not symbol_zones:
        return False, f"no_zones_defined:{symbol}"

    for i, zone in enumerate(symbol_zones):
        low = zone.get("low")
        high = zone.get("high")
        if low is None or high is None:
            return False, f"zone_missing_levels:{symbol}[{i}]"
        if float(low) >= float(high):
            return False, f"zone_low_gte_high:{symbol}[{i}]"

    # 9. Daily trade limit check
    effective_max_trades = min(config.max_trades_per_day, SIGNAL_MAX_TRADES_PER_DAY)
    today_count = ExecutionJob.count_today_signal_trades(
        account_id=account.id,
        strategy_id=strategy.id,
        symbol=symbol,
    )
    if today_count >= effective_max_trades:
        return False, f"daily_limit_reached:{today_count}/{effective_max_trades}"

    # 10. Concurrent positions check (pending jobs)
    pending_count = ExecutionJob.count_pending_signal_jobs(
        account_id=account.id,
        strategy_id=strategy.id,
        symbol=symbol,
    )
    effective_max_concurrent = min(config.max_concurrent_positions, SIGNAL_MAX_CONCURRENT_POSITIONS)
    if pending_count >= effective_max_concurrent:
        return False, f"concurrent_limit_reached:{pending_count}/{effective_max_concurrent}"

    # 11. Direction mode validation
    if config.direction_mode not in ("both", "long", "short"):
        return False, f"invalid_direction_mode:{config.direction_mode}"

    return True, ""


# =============================================================================
# Lot Size Calculation
# =============================================================================


def calculate_lot_size(
    account: TradingAccount,
    risk_pct: float,
    stop_distance_pips: float,
    symbol: str,
) -> float:
    """
    Calculate position size based on risk percentage and stop distance.

    For forex pairs:
    - 1 pip = 0.0001 for EURUSD, GBPUSD (4 decimal places)
    - 1 lot = 100,000 units
    - Pip value per lot = $10 for USD quote pairs

    Formula:
    risk_amount = balance * (risk_pct / 100)
    pip_value_per_lot = 10 (for USD quote pairs)
    lots = risk_amount / (stop_distance_pips * pip_value_per_lot)
    """
    # Get balance - for MVP, use a default if not available
    balance = getattr(account, "balance", None)
    if balance is None or float(balance) <= 0:
        # Fallback: reject if no balance
        return 0.0

    balance_float = float(balance)
    risk_amount = balance_float * (risk_pct / 100.0)

    # Pip value per lot for USD quote pairs
    pip_value_per_lot = 10.0

    # Calculate lots
    if stop_distance_pips <= 0:
        return 0.0

    lots = risk_amount / (stop_distance_pips * pip_value_per_lot)

    # Apply hard cap
    lots = min(lots, SIGNAL_MAX_LOT_SIZE)

    # Round to 2 decimal places (standard lot precision)
    lots = round(lots, 2)

    # Minimum lot size
    if lots < 0.01:
        lots = 0.01

    return lots


# =============================================================================
# Signal Evaluation (Placeholder for MVP)
# =============================================================================


def evaluate_trendline_break_pocket_signal(
    strategy: Strategy,
    account: TradingAccount,
    assignment: StrategyAssignment,
    symbol: str,
    config: TrendlineBreakPocketConfig,
    current_price: Optional[float] = None,
) -> SignalResult:
    """
    Evaluate whether a signal should be generated for the given symbol.

    MVP IMPLEMENTATION:
    This is a simplified placeholder that demonstrates the signal flow.
    Full implementation requires:
    1. H4 OHLC data feed from MT5 or external source
    2. Pivot high/low detection algorithm
    3. Trendline construction and break detection
    4. Swing structure analysis

    For the MVP, we return a "no_signal" result and let the user manually
    trigger test signals via a special endpoint.
    """
    # Get the first zone for reference (for testing purposes)
    symbol_zones = config.zones.get(symbol, [])
    if not symbol_zones:
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason="no_zones_available",
        )

    # For MVP: Return no_signal with details about what would be checked
    return SignalResult(
        ok=True,
        signal_type=None,  # No signal generated
        symbol=symbol,
        reason="no_signal_conditions_not_met",
        details={
            "zones_count": len(symbol_zones),
            "direction_mode": config.direction_mode,
            "rr_target": config.rr_target,
            "lookback_bars": config.trendline_lookback_bars,
            "pivot_strength": config.trendline_pivot_strength,
            "pocket_retest_required": config.pocket_retest_required,
            "note": "MVP placeholder - full signal logic not yet implemented",
        },
    )


# =============================================================================
# Manual Signal Generation (for testing)
# =============================================================================


def generate_manual_test_signal(
    strategy: Strategy,
    account: TradingAccount,
    assignment: StrategyAssignment,
    symbol: str,
    side: str,  # "BUY" or "SELL"
    entry_price: float,
    sl_price: float,
    tp_price: float,
    config: TrendlineBreakPocketConfig,
) -> SignalResult:
    """
    Generate a test signal with manually specified parameters.

    This allows testing the execution flow without full signal logic.
    Safety rails still apply.
    """
    # Validate side
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason=f"invalid_side:{side}",
        )

    # Check direction mode
    if config.direction_mode == "long" and side == "SELL":
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason="direction_mode_long_only",
        )
    if config.direction_mode == "short" and side == "BUY":
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason="direction_mode_short_only",
        )

    # Validate SL/TP relative to entry
    if side == "BUY":
        if sl_price >= entry_price:
            return SignalResult(
                ok=False,
                symbol=symbol,
                reason="sl_must_be_below_entry_for_buy",
            )
        if tp_price <= entry_price:
            return SignalResult(
                ok=False,
                symbol=symbol,
                reason="tp_must_be_above_entry_for_buy",
            )
    else:  # SELL
        if sl_price <= entry_price:
            return SignalResult(
                ok=False,
                symbol=symbol,
                reason="sl_must_be_above_entry_for_sell",
            )
        if tp_price >= entry_price:
            return SignalResult(
                ok=False,
                symbol=symbol,
                reason="tp_must_be_below_entry_for_sell",
            )

    # Calculate stop distance in pips
    pip_size = 0.0001  # For EURUSD, GBPUSD
    stop_distance_pips = abs(entry_price - sl_price) / pip_size

    # Calculate lot size
    risk_pct = float(strategy.risk_per_trade_pct or 0.1)
    lots = calculate_lot_size(account, risk_pct, stop_distance_pips, symbol)

    if lots <= 0:
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason="lot_size_calculation_failed",
            details={"risk_pct": risk_pct, "stop_pips": stop_distance_pips},
        )

    return SignalResult(
        ok=True,
        signal_type=side,
        symbol=symbol,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        lots=lots,
        reason="manual_test_signal",
        details={
            "risk_pct": risk_pct,
            "stop_distance_pips": stop_distance_pips,
            "calculated_lots": lots,
        },
    )


# =============================================================================
# Job Creation
# =============================================================================


def create_place_order_job(
    request: HttpRequest | None,
    strategy: Strategy,
    account: TradingAccount,
    assignment: StrategyAssignment,
    signal: SignalResult,
    user,
) -> ExecutionJob:
    """
    Create a PLACE_ORDER execution job from a signal.

    The job payload includes all information needed by the Windows bridge
    to execute the order.
    """
    # Generate correlation tag (same format as demo trades)
    # This will be updated with actual job ID after creation
    correlation_tag = f"GS{strategy.id:04d}"

    # Get windows_username from account's mt5_instance
    windows_username = None
    if account.mt5_instance:
        windows_username = getattr(account.mt5_instance, "windows_username", None)

    # Build payload
    payload = {
        "symbol": signal.symbol,
        "side": signal.signal_type,
        "lots": signal.lots,
        "entry_price": signal.entry_price,  # Optional: None = market order
        "sl_price": signal.sl_price,
        "tp_price": signal.tp_price,
        "comment": correlation_tag,
        "magic": strategy.magic_number or strategy.id,
        "is_demo": account.is_demo,
        "strategy_id": strategy.id,
        "windows_username": windows_username,
        "safety_rails": {
            "max_lots": SIGNAL_MAX_LOT_SIZE,
            "allowed_symbols": SIGNAL_ALLOWED_SYMBOLS,
            "demo_only": True,
        },
    }

    # Create the job
    job = ExecutionJob.objects.create(
        job_type=ExecutionJob.JobType.PLACE_ORDER,
        account=account,
        strategy=strategy,
        assignment=assignment,
        status=ExecutionJob.Status.PENDING,
        created_by=user,
        payload=payload,
    )

    # Update correlation tag with actual job ID
    job.payload["comment"] = f"GS{job.id:04d}"
    job.save(update_fields=["payload"])

    # Audit log
    log_signal_created(
        request=request,
        strategy_id=strategy.id,
        account_id=account.id,
        job_id=job.id,
        symbol=signal.symbol,
        side=signal.signal_type,
        lots=signal.lots,
        entry_price=signal.entry_price,
        sl_price=signal.sl_price,
        tp_price=signal.tp_price,
    )

    return job


# =============================================================================
# Main Entry Point
# =============================================================================


def run_signal_evaluation(
    request: HttpRequest | None,
    strategy: Strategy,
    account: TradingAccount,
    symbol: str,
    user,
    manual_params: Optional[dict] = None,
) -> SignalResult:
    """
    Main entry point for signal evaluation and job creation.

    Args:
        request: HTTP request (for audit logging)
        strategy: The strategy to evaluate
        account: The trading account
        symbol: Symbol to evaluate (e.g., "EURUSD")
        user: The user triggering the evaluation
        manual_params: Optional manual signal parameters for testing:
            {
                "side": "BUY" or "SELL",
                "entry_price": float,
                "sl_price": float,
                "tp_price": float,
            }

    Returns:
        SignalResult with evaluation outcome and job_id if created
    """
    # Get assignment
    assignment = StrategyAssignment.objects.filter(
        strategy=strategy,
        account=account,
        is_active=True,
    ).first()

    if not assignment:
        log_signal_rejected(
            request, strategy.id, account.id, symbol,
            reason="no_active_assignment",
        )
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason="no_active_assignment",
        )

    # Extract config
    filters = strategy.filters or {}
    template_slug = filters.get("template_slug", "")

    # Only process trendline-break-pocket-ali strategies
    if template_slug != "trendline-break-pocket-ali":
        log_signal_rejected(
            request, strategy.id, account.id, symbol,
            reason="wrong_template",
            details={"template_slug": template_slug},
        )
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason=f"wrong_template:{template_slug}",
        )

    config = TrendlineBreakPocketConfig.from_filters(filters)

    # Safety validation
    is_valid, error_reason = validate_signal_safety(
        strategy, account, assignment, symbol, config
    )
    if not is_valid:
        log_signal_rejected(
            request, strategy.id, account.id, symbol,
            reason=error_reason,
        )
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason=error_reason,
        )

    # Generate signal
    if manual_params:
        # Manual test signal
        signal = generate_manual_test_signal(
            strategy=strategy,
            account=account,
            assignment=assignment,
            symbol=symbol,
            side=manual_params.get("side", "BUY"),
            entry_price=float(manual_params.get("entry_price", 0)),
            sl_price=float(manual_params.get("sl_price", 0)),
            tp_price=float(manual_params.get("tp_price", 0)),
            config=config,
        )
    else:
        # Automatic signal evaluation
        signal = evaluate_trendline_break_pocket_signal(
            strategy=strategy,
            account=account,
            assignment=assignment,
            symbol=symbol,
            config=config,
        )

    # Log evaluation
    log_signal_evaluated(
        request, strategy.id, account.id, symbol,
        signal_result=signal.to_dict(),
    )

    # If signal generated, create job
    if signal.ok and signal.signal_type:
        job = create_place_order_job(
            request=request,
            strategy=strategy,
            account=account,
            assignment=assignment,
            signal=signal,
            user=user,
        )
        signal.job_id = job.id
        signal.reason = "job_queued"

    return signal
