"""
Execution Engine Integration Placeholder for GuvFX Strategies.

This module provides placeholder functions for strategy execution integration.
The actual execution logic will be implemented when the execution engine is built.

NOTE: This is a placeholder. No actual trades are executed.
"""

import logging
from typing import Any

from .models import Strategy

logger = logging.getLogger(__name__)


class ExecutionEngineNotImplementedError(Exception):
    """Raised when execution engine methods are called but not yet implemented."""
    pass


def validate_strategy_for_execution(strategy: Strategy) -> dict[str, Any]:
    """
    Validate that a strategy is ready for execution.

    Returns a dict with:
    - ready: bool indicating if strategy can be executed
    - errors: list of validation error messages
    - warnings: list of validation warnings

    This is a placeholder that performs basic validation.
    Full validation will be implemented with the execution engine.
    """
    errors = []
    warnings = []

    # Basic validation
    if not strategy.is_active:
        errors.append("Strategy is not active.")

    if not strategy.symbol_universe:
        errors.append("No symbols defined in strategy.")

    if not strategy.timeframe:
        errors.append("No timeframe defined in strategy.")

    risk_pct = strategy.risk_per_trade_pct
    if risk_pct is None or float(risk_pct) <= 0:
        errors.append("Risk per trade percentage must be > 0.")

    # Template-specific validation: Trendline Break Pocket (Ali)
    filters = strategy.filters or {}
    template_slug = filters.get("template_slug", "")

    if template_slug == "trendline-break-pocket-ali":
        # Validate required TBP parameters
        if not filters.get("zones"):
            errors.append("No HTF zones defined for Trendline Break Pocket strategy.")

        lookback = filters.get("trendline_lookback_bars")
        if lookback is None or lookback < 50:
            errors.append("Trendline lookback must be >= 50 bars.")

        direction_mode = filters.get("direction_mode")
        if direction_mode not in {"both", "long", "short"}:
            errors.append("Invalid direction_mode for Trendline Break Pocket.")

        # Validate zones
        zones = filters.get("zones") or {}
        for symbol, zone_list in zones.items():
            if not isinstance(zone_list, list) or len(zone_list) == 0:
                warnings.append(f"No zones defined for {symbol}.")
            else:
                for i, zone in enumerate(zone_list):
                    if not isinstance(zone, dict):
                        continue
                    low = zone.get("low")
                    high = zone.get("high")
                    if low is not None and high is not None:
                        if float(low) >= float(high):
                            errors.append(f"{symbol} zone {i+1}: low must be < high.")

    return {
        "ready": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def prepare_execution_config(strategy: Strategy) -> dict[str, Any]:
    """
    Prepare execution configuration for a strategy.

    This placeholder returns the configuration that would be sent to
    the execution engine when it is implemented.

    NOTE: This does not execute any trades.
    """
    filters = strategy.filters or {}
    template_slug = filters.get("template_slug", "")

    config = {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "template_slug": template_slug,
        "symbols": [s.strip() for s in (strategy.symbol_universe or "").split(",") if s.strip()],
        "timeframe": strategy.timeframe,
        "risk_per_trade_pct": float(strategy.risk_per_trade_pct or 0),
        "is_active": strategy.is_active,
        "execution_ready": False,  # Placeholder: always false until engine is built
        "execution_engine_status": "NOT_IMPLEMENTED",
    }

    # Add template-specific configuration
    if template_slug == "trendline-break-pocket-ali":
        config["tbp_config"] = {
            "enabled": filters.get("enabled", True),
            "direction_mode": filters.get("direction_mode", "both"),
            "htf_timeframe": filters.get("htf_timeframe", "D1"),
            "execution_timeframe": filters.get("execution_timeframe", "H4"),
            "rr_target": filters.get("rr_target", 2.0),
            "trendline_lookback_bars": filters.get("trendline_lookback_bars", 101),
            "trendline_pivot_strength": filters.get("trendline_pivot_strength", 2),
            "break_confirm_bars": filters.get("break_confirm_bars", 1),
            "swing_break_mode": filters.get("swing_break_mode", "close_break"),
            "swing_lookback": filters.get("swing_lookback", 7),
            "pocket_retest_required": filters.get("pocket_retest_required", True),
            "entry_buffer_pips": filters.get("entry_buffer_pips", {}),
            "overshoot_max_pips": filters.get("overshoot_max_pips", {}),
            "clean_air_min_pips": filters.get("clean_air_min_pips", {}),
            "max_trades_per_day": filters.get("max_trades_per_day", 1),
            "news_filter_mode": filters.get("news_filter_mode", "major_only"),
            "zones": filters.get("zones", {}),
        }

    return config


def start_strategy_execution(strategy: Strategy) -> dict[str, Any]:
    """
    Start executing a strategy.

    PLACEHOLDER: This function is not implemented.
    When called, it will log the request and raise an error.

    The actual implementation will:
    1. Validate the strategy is ready
    2. Connect to the execution engine (Windows agent / MT5)
    3. Start monitoring for entry signals
    4. Execute trades when conditions are met
    """
    logger.warning(
        f"start_strategy_execution called for strategy {strategy.id} "
        f"({strategy.name}), but execution engine is not implemented."
    )

    raise ExecutionEngineNotImplementedError(
        "Strategy execution is not yet implemented. "
        "This feature will be available in a future release."
    )


def stop_strategy_execution(strategy: Strategy) -> dict[str, Any]:
    """
    Stop executing a strategy.

    PLACEHOLDER: This function is not implemented.
    """
    logger.warning(
        f"stop_strategy_execution called for strategy {strategy.id} "
        f"({strategy.name}), but execution engine is not implemented."
    )

    raise ExecutionEngineNotImplementedError(
        "Strategy execution is not yet implemented. "
        "This feature will be available in a future release."
    )


def get_execution_status(strategy: Strategy) -> dict[str, Any]:
    """
    Get the current execution status of a strategy.

    PLACEHOLDER: Returns a default status indicating execution is not available.
    """
    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "execution_status": "NOT_AVAILABLE",
        "message": "Execution engine is not yet implemented.",
        "is_running": False,
        "last_signal": None,
        "open_positions": 0,
        "daily_trades": 0,
    }
