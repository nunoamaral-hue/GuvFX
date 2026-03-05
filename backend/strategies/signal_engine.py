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
- Max trades per day per account+strategy+symbol: 10
- Max concurrent positions per symbol: 1

AUTO MODE:
When manual_params is None, the engine fetches H4/D1 OHLC data from the Windows
agent and evaluates the full TBP signal logic deterministically.
"""

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, List, Dict, Any

from django.http import HttpRequest
from django.utils import timezone

from execution.models import (
    ExecutionJob,
    SIGNAL_ALLOWED_SYMBOLS,
    SIGNAL_MAX_LOT_SIZE,
    SIGNAL_MAX_TRADES_PER_DAY,
    SIGNAL_MAX_CONCURRENT_POSITIONS,
)
from strategies.models import Strategy, StrategyAssignment
from strategies.zone_generator import resolve_zones
from trading.models import TradingAccount, Trade
from core.audit import log_signal_evaluated, log_signal_rejected, log_signal_created

logger = logging.getLogger(__name__)

# =============================================================================
# TBP Debug Branch Trace (env-gated, production-safe)
# =============================================================================
TBP_DEBUG = os.getenv("GUVFX_TBP_DEBUG") == "1"


def _tbp_debug(msg: str) -> None:
    """Emit a TBP branch-trace line. Only runs when GUVFX_TBP_DEBUG=1."""
    if TBP_DEBUG:
        logger.info(msg)


# =============================================================================
# OHLC Data Fetching from Windows Agent
# =============================================================================


class RatesFetchError(Exception):
    """Exception raised when fetching rates from Windows agent fails."""
    pass


def fetch_rates(
    account,
    symbol: str,
    timeframe: str,
    count: int = 300,
) -> List[Dict[str, Any]]:
    """
    Fetch OHLC rates from the Windows MT5 agent.

    Args:
        account: TradingAccount with windows_username
        symbol: Trading symbol (e.g., "EURUSD")
        timeframe: Timeframe string ("H4", "D1", etc.)
        count: Number of bars to fetch (max 500)

    Returns:
        List of OHLC dicts with keys: time, open, high, low, close, tick_volume

    Raises:
        RatesFetchError: If the request fails or returns invalid data
    """
    # Get OHLC agent URL - prefer dedicated OHLC endpoint (8788), fallback to legacy (8787)
    # GUVFX_WINDOWS_AGENT_BASE_URL = OHLC agent on port 8788 (/mt5/snapshots/rates)
    # GUVFX_AGENT_URL / WINDOWS_AGENT_BASE = deals agent on port 8787 (legacy fallback)
    agent_url = (
        os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL")
        or os.getenv("GUVFX_AGENT_URL")
        or os.getenv("WINDOWS_AGENT_BASE")
        or ""
    ).rstrip("/")
    agent_token = (os.getenv("GUVFX_AGENT_TOKEN") or os.getenv("WINDOWS_AGENT_TOKEN") or "").strip()

    if not agent_url:
        raise RatesFetchError("GUVFX_WINDOWS_AGENT_BASE_URL not configured")

    # Build URL with query params
    params = f"symbol={symbol}&timeframe={timeframe}&count={count}"
    url = f"{agent_url}/mt5/snapshots/rates?{params}"

    # Build request
    headers = {"Content-Type": "application/json"}
    if agent_token:
        headers["X-GuvFX-Agent-Token"] = agent_token

    req = urllib.request.Request(url, method="GET", headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            if not raw:
                raise RatesFetchError("Empty response from agent")

            data = json.loads(raw)

            if not data.get("ok"):
                error = data.get("error", "unknown error")
                raise RatesFetchError(f"Agent error: {error}")

            rates = data.get("data", [])
            if not isinstance(rates, list):
                raise RatesFetchError("Invalid data format: expected list")

            return rates

    except urllib.error.URLError as e:
        raise RatesFetchError(f"Connection failed: {e}")
    except urllib.error.HTTPError as e:
        raise RatesFetchError(f"HTTP error {e.code}: {e.reason}")
    except json.JSONDecodeError as e:
        raise RatesFetchError(f"Invalid JSON response: {e}")
    except Exception as e:
        raise RatesFetchError(f"Unexpected error: {e}")


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
    max_trades_per_day: int = 10
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
            max_trades_per_day=filters.get("max_trades_per_day", 10),
            max_concurrent_positions=filters.get("max_concurrent_positions", 1),
            news_filter_mode=filters.get("news_filter_mode", "major_only"),
            zones=resolve_zones(filters),
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


SIGNAL_MIN_LOT_SIZE = 0.01  # Hard floor for lot sizing


def calculate_lot_size(
    account: TradingAccount,
    risk_pct: float,
    stop_distance_pips: float,
    symbol: str,
) -> tuple[float, str | None]:
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

    Returns:
        (lots, warning) - lots is always >= 0.01, warning is set if fallback used
    """
    warning = None

    # Get balance - for MVP, use a default if not available
    balance = getattr(account, "balance", None)
    if balance is None or float(balance) <= 0:
        # Fallback: use minimum lot size instead of failing
        return SIGNAL_MIN_LOT_SIZE, "no_balance_fallback_to_min_lots"

    balance_float = float(balance)
    risk_amount = balance_float * (risk_pct / 100.0)

    # Pip value per lot for USD quote pairs
    pip_value_per_lot = 10.0

    # Calculate lots
    if stop_distance_pips <= 0:
        return SIGNAL_MIN_LOT_SIZE, "invalid_stop_pips_fallback_to_min_lots"

    lots = risk_amount / (stop_distance_pips * pip_value_per_lot)

    # Apply hard cap
    if lots > SIGNAL_MAX_LOT_SIZE:
        lots = SIGNAL_MAX_LOT_SIZE
        warning = f"lot_size_capped_to_max:{SIGNAL_MAX_LOT_SIZE}"

    # Round to 2 decimal places (standard lot precision)
    lots = round(lots, 2)

    # Minimum lot size
    if lots < SIGNAL_MIN_LOT_SIZE:
        lots = SIGNAL_MIN_LOT_SIZE
        warning = f"lot_size_raised_to_min:{SIGNAL_MIN_LOT_SIZE}"

    return lots, warning


# =============================================================================
# TBP Signal Analysis Functions
# =============================================================================


def find_pivot_highs(rates: List[Dict], strength: int = 2) -> List[Dict]:
    """
    Find pivot highs in OHLC data.

    A pivot high is a bar where the high is higher than `strength` bars on each side.

    Returns list of {index, time, price} for each pivot.
    """
    pivots = []
    n = len(rates)

    for i in range(strength, n - strength):
        high = rates[i]["high"]
        is_pivot = True

        # Check left side
        for j in range(1, strength + 1):
            if rates[i - j]["high"] >= high:
                is_pivot = False
                break

        # Check right side
        if is_pivot:
            for j in range(1, strength + 1):
                if rates[i + j]["high"] >= high:
                    is_pivot = False
                    break

        if is_pivot:
            pivots.append({
                "index": i,
                "time": rates[i]["time"],
                "price": high,
            })

    return pivots


def find_pivot_lows(rates: List[Dict], strength: int = 2) -> List[Dict]:
    """
    Find pivot lows in OHLC data.

    A pivot low is a bar where the low is lower than `strength` bars on each side.

    Returns list of {index, time, price} for each pivot.
    """
    pivots = []
    n = len(rates)

    for i in range(strength, n - strength):
        low = rates[i]["low"]
        is_pivot = True

        # Check left side
        for j in range(1, strength + 1):
            if rates[i - j]["low"] <= low:
                is_pivot = False
                break

        # Check right side
        if is_pivot:
            for j in range(1, strength + 1):
                if rates[i + j]["low"] <= low:
                    is_pivot = False
                    break

        if is_pivot:
            pivots.append({
                "index": i,
                "time": rates[i]["time"],
                "price": low,
            })

    return pivots


def compute_trendline_from_pivots(
    pivots: List[Dict],
    lookback_bars: int,
    rates_len: int,
) -> Optional[Dict]:
    """
    Compute a deterministic trendline from pivot points.

    For bullish (demand zone): Uses pivot lows, line connects most recent valid pivots
    For bearish (supply zone): Uses pivot highs, line connects most recent valid pivots

    Returns {slope, intercept, start_idx, end_idx, start_price, end_price} or None
    """
    if len(pivots) < 2:
        return None

    # Only use pivots within lookback window
    min_idx = rates_len - lookback_bars
    valid_pivots = [p for p in pivots if p["index"] >= min_idx]

    if len(valid_pivots) < 2:
        return None

    # Use the two most recent pivots for deterministic line
    p1 = valid_pivots[-2]
    p2 = valid_pivots[-1]

    # Calculate slope (price per bar)
    idx_diff = p2["index"] - p1["index"]
    if idx_diff == 0:
        return None

    slope = (p2["price"] - p1["price"]) / idx_diff
    intercept = p1["price"] - slope * p1["index"]

    return {
        "slope": slope,
        "intercept": intercept,
        "start_idx": p1["index"],
        "end_idx": p2["index"],
        "start_price": p1["price"],
        "end_price": p2["price"],
    }


def get_trendline_value_at(trendline: Dict, bar_index: int) -> float:
    """Get the trendline price at a specific bar index."""
    return trendline["slope"] * bar_index + trendline["intercept"]


def is_price_in_zone(price: float, zone: Dict) -> bool:
    """Check if price is within a zone's low/high range."""
    return zone["low"] <= price <= zone["high"]


def find_swing_high(rates: List[Dict], lookback: int) -> Optional[float]:
    """Find the highest high in the last `lookback` bars."""
    if len(rates) < lookback:
        return None
    return max(r["high"] for r in rates[-lookback:])


def find_swing_low(rates: List[Dict], lookback: int) -> Optional[float]:
    """Find the lowest low in the last `lookback` bars."""
    if len(rates) < lookback:
        return None
    return min(r["low"] for r in rates[-lookback:])


def _evaluate_branch(
    direction: str,
    zone_type: Optional[str],
    direction_mode: str,
    h4_rates: List[Dict],
    current_close: float,
    entry_buffer: float,
    trendline_lookback_bars: int,
    trendline_pivot_strength: int,
    swing_lookback: int,
    active_zone: Optional[Dict],
    pip_size: float,
    rr_target: float,
) -> str:
    """
    Evaluate one direction branch and return a stable reason code.

    This is a PURE diagnostic function — it does NOT create signals or jobs.
    Used only for TBP_DEBUG branch-trace logging.

    Returns one of the stable reason codes:
        price_not_in_zone, direction_mode_excluded, zone_type_mismatch,
        no_trendline, no_trendline_break_up, no_trendline_break_down,
        no_swing_break_up, no_swing_break_down, sl_tp_invalid, signal_ready
    """
    if not active_zone:
        return "price_not_in_zone"

    # Direction mode filter
    if direction == "BUY" and direction_mode == "short":
        return "direction_mode_excluded"
    if direction == "SELL" and direction_mode == "long":
        return "direction_mode_excluded"

    # Zone type compatibility
    if direction == "BUY" and zone_type == "supply":
        return "zone_type_mismatch"
    if direction == "SELL" and zone_type == "demand":
        return "zone_type_mismatch"

    # Trendline computation
    if direction == "BUY":
        pivots = find_pivot_highs(h4_rates[:-1], trendline_pivot_strength)
        trendline = compute_trendline_from_pivots(pivots, trendline_lookback_bars, len(h4_rates) - 1)
        if not trendline:
            return "no_trendline"
        tl_val = get_trendline_value_at(trendline, len(h4_rates) - 2)
        if current_close < tl_val + entry_buffer:
            return "no_trendline_break_up"
    else:
        pivots = find_pivot_lows(h4_rates[:-1], trendline_pivot_strength)
        trendline = compute_trendline_from_pivots(pivots, trendline_lookback_bars, len(h4_rates) - 1)
        if not trendline:
            return "no_trendline"
        tl_val = get_trendline_value_at(trendline, len(h4_rates) - 2)
        if current_close > tl_val - entry_buffer:
            return "no_trendline_break_down"

    # Swing structure break
    if direction == "BUY":
        swing_high = find_swing_high(h4_rates[:-2], swing_lookback)
        if swing_high and current_close <= swing_high:
            return "no_swing_break_up"
    else:
        swing_low = find_swing_low(h4_rates[:-2], swing_lookback)
        if swing_low and current_close >= swing_low:
            return "no_swing_break_down"

    # SL/TP feasibility
    if direction == "BUY":
        sl_price = active_zone["low"] - (5 * pip_size)
        sl_dist = current_close - sl_price
    else:
        sl_price = active_zone["high"] + (5 * pip_size)
        sl_dist = sl_price - current_close

    if sl_dist <= 0:
        return "sl_tp_invalid"

    return "signal_ready"


def evaluate_trendline_break_pocket_signal(
    strategy: Strategy,
    account: TradingAccount,
    assignment: StrategyAssignment,
    symbol: str,
    config: TrendlineBreakPocketConfig,
    current_price: Optional[float] = None,
    h4_rates: Optional[List[Dict]] = None,
    d1_rates: Optional[List[Dict]] = None,
) -> SignalResult:
    """
    Evaluate whether a TBP signal should be generated for the given symbol.

    FULL AUTO MODE:
    When h4_rates and d1_rates are provided, evaluates the complete signal logic:
    1. Check if current price is inside a D1 zone
    2. Compute trendline from H4 pivot points
    3. Check for H4 close break of trendline
    4. Confirm swing structure break
    5. Optionally wait for pocket retest

    MANUAL/FALLBACK MODE:
    When rates are not provided, returns no_signal.
    """
    symbol_zones = config.zones.get(symbol, [])
    if not symbol_zones:
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason="no_zones_available",
        )

    # If no rates provided, can't evaluate (manual mode required)
    if not h4_rates or len(h4_rates) < config.trendline_lookback_bars:
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason="insufficient_h4_data",
            details={"bars_needed": config.trendline_lookback_bars, "bars_available": len(h4_rates) if h4_rates else 0},
        )

    if not d1_rates or len(d1_rates) < 20:
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason="insufficient_d1_data",
            details={"bars_needed": 20, "bars_available": len(d1_rates) if d1_rates else 0},
        )

    # Get current H4 bar (most recent closed bar is index -2, current is -1)
    # We evaluate on the CLOSED bar, not the forming bar
    current_bar = h4_rates[-2] if len(h4_rates) >= 2 else h4_rates[-1]
    current_close = current_bar["close"]
    current_high = current_bar["high"]
    current_low = current_bar["low"]

    # Get pip size for the symbol
    pip_size = 0.0001 if "JPY" not in symbol else 0.01
    entry_buffer_pips = config.entry_buffer_pips.get(symbol, 2)
    entry_buffer = entry_buffer_pips * pip_size

    # Step 1: Find which zone (if any) price is in
    active_zone = None
    zone_type = None  # "demand" or "supply"

    for zone in symbol_zones:
        if is_price_in_zone(current_close, zone):
            active_zone = zone
            zone_type = zone.get("zone_type", "demand")  # Default to demand
            break

    # -----------------------------------------------------------------
    # TBP Debug: branch-coverage trace (only when GUVFX_TBP_DEBUG=1)
    # -----------------------------------------------------------------
    if TBP_DEBUG:
        _zone_name = active_zone.get("name", f"{zone_type}_{symbol}") if active_zone else "none"
        _bar_ts = current_bar.get("time", "?")
        _in_zone = active_zone is not None
        _tbp_debug(
            f"[TBP_DEBUG] bar={_bar_ts} sym={symbol} mode={config.direction_mode} "
            f"in_zone={str(_in_zone).lower()} zone={_zone_name} zone_type={zone_type or 'none'} "
            f"close={current_close}"
        )
        _branch_args = dict(
            zone_type=zone_type,
            direction_mode=config.direction_mode,
            h4_rates=h4_rates,
            current_close=current_close,
            entry_buffer=entry_buffer,
            trendline_lookback_bars=config.trendline_lookback_bars,
            trendline_pivot_strength=config.trendline_pivot_strength,
            swing_lookback=config.swing_lookback,
            active_zone=active_zone,
            pip_size=pip_size,
            rr_target=config.rr_target,
        )
        if config.direction_mode == "both":
            _buy_reason = _evaluate_branch(direction="BUY", **_branch_args)
            _sell_reason = _evaluate_branch(direction="SELL", **_branch_args)
            _tbp_debug(
                f"[TBP_DEBUG] bar={_bar_ts} sym={symbol} branch=BUY "
                f"pass={str(_buy_reason == 'signal_ready').lower()} reason={_buy_reason}"
            )
            _tbp_debug(
                f"[TBP_DEBUG] bar={_bar_ts} sym={symbol} branch=SELL "
                f"pass={str(_sell_reason == 'signal_ready').lower()} reason={_sell_reason}"
            )
        elif config.direction_mode == "long":
            _buy_reason = _evaluate_branch(direction="BUY", **_branch_args)
            _tbp_debug(
                f"[TBP_DEBUG] bar={_bar_ts} sym={symbol} branch=BUY "
                f"pass={str(_buy_reason == 'signal_ready').lower()} reason={_buy_reason}"
            )
        elif config.direction_mode == "short":
            _sell_reason = _evaluate_branch(direction="SELL", **_branch_args)
            _tbp_debug(
                f"[TBP_DEBUG] bar={_bar_ts} sym={symbol} branch=SELL "
                f"pass={str(_sell_reason == 'signal_ready').lower()} reason={_sell_reason}"
            )

    if not active_zone:
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason="price_not_in_zone",
            details={"current_close": current_close, "zones_checked": len(symbol_zones)},
        )

    # Determine signal direction based on zone type
    if zone_type == "demand":
        signal_direction = "BUY"
        if config.direction_mode == "short":
            return SignalResult(ok=True, signal_type=None, symbol=symbol, reason="direction_mode_excludes_long")
    elif zone_type == "supply":
        signal_direction = "SELL"
        if config.direction_mode == "long":
            return SignalResult(ok=True, signal_type=None, symbol=symbol, reason="direction_mode_excludes_short")
    else:
        # Pivot zone - check price direction relative to zone center
        zone_mid = (active_zone["low"] + active_zone["high"]) / 2
        if current_close > zone_mid:
            signal_direction = "SELL" if config.direction_mode != "long" else None
        else:
            signal_direction = "BUY" if config.direction_mode != "short" else None

        if not signal_direction:
            return SignalResult(ok=True, signal_type=None, symbol=symbol, reason="pivot_zone_direction_filtered")

    # Step 2: Compute trendline
    if signal_direction == "BUY":
        # For BUY: Look for downtrend line from pivot highs, break upward
        pivots = find_pivot_highs(h4_rates[:-1], config.trendline_pivot_strength)
        trendline = compute_trendline_from_pivots(pivots, config.trendline_lookback_bars, len(h4_rates) - 1)

        if not trendline:
            return SignalResult(
                ok=True,
                signal_type=None,
                symbol=symbol,
                reason="no_valid_downtrend_line",
                details={"pivot_count": len(pivots)},
            )

        # Check for upward break: close above trendline + buffer
        trendline_value = get_trendline_value_at(trendline, len(h4_rates) - 2)
        break_threshold = trendline_value + entry_buffer

        if current_close < break_threshold:
            return SignalResult(
                ok=True,
                signal_type=None,
                symbol=symbol,
                reason="no_trendline_break",
                details={"close": current_close, "trendline": trendline_value, "threshold": break_threshold},
            )

    else:  # SELL
        # For SELL: Look for uptrend line from pivot lows, break downward
        pivots = find_pivot_lows(h4_rates[:-1], config.trendline_pivot_strength)
        trendline = compute_trendline_from_pivots(pivots, config.trendline_lookback_bars, len(h4_rates) - 1)

        if not trendline:
            return SignalResult(
                ok=True,
                signal_type=None,
                symbol=symbol,
                reason="no_valid_uptrend_line",
                details={"pivot_count": len(pivots)},
            )

        # Check for downward break: close below trendline - buffer
        trendline_value = get_trendline_value_at(trendline, len(h4_rates) - 2)
        break_threshold = trendline_value - entry_buffer

        if current_close > break_threshold:
            return SignalResult(
                ok=True,
                signal_type=None,
                symbol=symbol,
                reason="no_trendline_break",
                details={"close": current_close, "trendline": trendline_value, "threshold": break_threshold},
            )

    # Step 3: Confirm swing structure break
    swing_lookback = config.swing_lookback

    if signal_direction == "BUY":
        # BUY: Need close above recent swing high
        swing_high = find_swing_high(h4_rates[:-2], swing_lookback)
        if swing_high and current_close <= swing_high:
            return SignalResult(
                ok=True,
                signal_type=None,
                symbol=symbol,
                reason="swing_break_not_confirmed",
                details={"close": current_close, "swing_high": swing_high},
            )
    else:
        # SELL: Need close below recent swing low
        swing_low = find_swing_low(h4_rates[:-2], swing_lookback)
        if swing_low and current_close >= swing_low:
            return SignalResult(
                ok=True,
                signal_type=None,
                symbol=symbol,
                reason="swing_break_not_confirmed",
                details={"close": current_close, "swing_low": swing_low},
            )

    # Step 4: Pocket retest (if required) - simplified: entry at current close
    # Full implementation would track runtime_state for multi-bar retest
    entry_price = current_close

    # Step 5: Calculate SL and TP
    rr_target = config.rr_target

    if signal_direction == "BUY":
        # SL below zone low with buffer
        sl_price = active_zone["low"] - (5 * pip_size)  # 5 pip buffer
        sl_distance = entry_price - sl_price
        tp_price = entry_price + (sl_distance * rr_target)
    else:
        # SL above zone high with buffer
        sl_price = active_zone["high"] + (5 * pip_size)  # 5 pip buffer
        sl_distance = sl_price - entry_price
        tp_price = entry_price - (sl_distance * rr_target)

    # Calculate lot size
    stop_distance_pips = abs(entry_price - sl_price) / pip_size
    risk_pct = float(strategy.risk_per_trade_pct or 1.0)
    lots, lot_warning = calculate_lot_size(account, risk_pct, stop_distance_pips, symbol)

    zone_name = active_zone.get("name", f"{zone_type}_{symbol}")

    return SignalResult(
        ok=True,
        signal_type=signal_direction,
        symbol=symbol,
        entry_price=round(entry_price, 5),
        sl_price=round(sl_price, 5),
        tp_price=round(tp_price, 5),
        lots=lots,
        reason="trendline_break_pocket_signal",
        details={
            "zone_name": zone_name,
            "zone_type": zone_type,
            "zone_low": active_zone["low"],
            "zone_high": active_zone["high"],
            "trendline_value": round(trendline_value, 5),
            "rr_target": rr_target,
            "risk_pct": risk_pct,
            "stop_distance_pips": round(stop_distance_pips, 1),
            "lot_warning": lot_warning,
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
    override_lots: float | None = None,
) -> SignalResult:
    """
    Generate a test signal with manually specified parameters.

    This allows testing the execution flow without full signal logic.
    Safety rails still apply.

    Args:
        override_lots: Optional explicit lot size (must be 0.01 <= lots <= 0.02).
                      If provided, bypasses risk-based calculation.
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

    # Determine lot size
    warning = None
    risk_pct = float(strategy.risk_per_trade_pct or 1.0)

    if override_lots is not None:
        # Validate explicit lots override (hard caps enforced)
        try:
            lots = float(override_lots)
        except (TypeError, ValueError):
            return SignalResult(
                ok=False,
                symbol=symbol,
                reason="invalid_lots_value",
                details={"lots": override_lots},
            )

        if lots < SIGNAL_MIN_LOT_SIZE:
            return SignalResult(
                ok=False,
                symbol=symbol,
                reason=f"lots_below_minimum:{SIGNAL_MIN_LOT_SIZE}",
                details={"lots": lots, "min": SIGNAL_MIN_LOT_SIZE},
            )
        if lots > SIGNAL_MAX_LOT_SIZE:
            return SignalResult(
                ok=False,
                symbol=symbol,
                reason=f"lots_above_maximum:{SIGNAL_MAX_LOT_SIZE}",
                details={"lots": lots, "max": SIGNAL_MAX_LOT_SIZE},
            )
        # Round to 2 decimals
        lots = round(lots, 2)
    else:
        # Calculate lot size from risk (with safe fallback)
        lots, warning = calculate_lot_size(account, risk_pct, stop_distance_pips, symbol)

    details = {
        "risk_pct": risk_pct,
        "stop_distance_pips": stop_distance_pips,
        "lots": lots,
        "lots_source": "override" if override_lots is not None else "calculated",
    }
    if warning:
        details["warning"] = warning

    return SignalResult(
        ok=True,
        signal_type=side,
        symbol=symbol,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        lots=lots,
        reason="manual_test_signal",
        details=details,
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
    bar_close_time: Optional[str] = None,
) -> ExecutionJob:
    """
    Create a PLACE_ORDER execution job from a signal.

    The job payload includes all information needed by the Windows bridge
    to execute the order.

    Args:
        bar_close_time: Optional ISO timestamp of the H4 bar close being evaluated.
                       Used for idempotency in auto-evaluation mode.
    """
    # Generate correlation tag (same format as demo trades)
    # This will be updated with actual job ID after creation
    correlation_tag = f"GS{strategy.id:04d}"

    # Get windows_username from account's mt5_instance
    windows_username = None
    if account.mt5_instance:
        windows_username = getattr(account.mt5_instance, "windows_username", None)

    # Extract zone info from signal details
    signal_details = signal.details or {}
    zone_name = signal_details.get("zone_name", "")
    signal_reason = signal.reason or "signal"

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
        "zone_name": zone_name,
        "signal_reason": signal_reason,
        "assignment_stage": getattr(assignment, "stage", "UNKNOWN"),
        "safety_rails": {
            "max_lots": SIGNAL_MAX_LOT_SIZE,
            "allowed_symbols": SIGNAL_ALLOWED_SYMBOLS,
            "demo_only": True,
        },
    }

    # Include bar_close_time for idempotency (H4 auto-evaluation mode)
    if bar_close_time:
        payload["bar_close_time"] = bar_close_time

    # Macro audit for hybrid wrapper (only hybrid signals carry portfolio details)
    portfolio = (signal.details or {}).get("portfolio")
    if portfolio:
        payload["macro_label"] = portfolio.get("macro_regime_label") or "UNKNOWN"
        payload["macro_provider"] = "MACRO_PROVIDER_V2"

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
    bar_close_time: Optional[str] = None,
    dry_run: bool = False,
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
        bar_close_time: Optional ISO timestamp for H4 auto-evaluation mode.
                       Included in job payload for idempotency.

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

    # Route by template slug
    if template_slug == "trendline-break-pocket-ali":
        pass  # Fall through to TBP logic below
    elif template_slug == "adaptive-liquidity-trap-scalper":
        # ALTS engine — dispatches to dedicated module
        from strategies.engines.alts_engine import evaluate_alts
        return evaluate_alts(
            assignment=assignment,
            symbol=symbol,
            now_ts=timezone.now(),
            bar_close_time=bar_close_time or "",
        )
    elif template_slug == "structural-continuation-engine":
        # SCE engine — dispatches to dedicated module (milestone 3)
        from strategies.engines.sce_engine import evaluate_sce
        return evaluate_sce(
            assignment=assignment,
            symbol=symbol,
            now_ts=timezone.now(),
            bar_close_time=bar_close_time or "",
        )
    elif template_slug == "tc1-engine-v1":
        # TC1 engine — trend continuation v1 (job creation inside engine)
        from strategies.engines.tc1_engine_v1 import evaluate_tc1_engine_v1, TC1Config
        tc1_cfg = TC1Config.from_filters(filters)
        return evaluate_tc1_engine_v1(
            strategy=strategy,
            account=account,
            assignment=assignment,
            symbol=symbol,
            config=tc1_cfg,
            now_ts=timezone.now(),
            bar_close_time=bar_close_time or "",
            dry_run=dry_run,
        )
    elif template_slug == "tbp-v3-hybrid-sleeve-v1":
        # Hybrid wrapper — CORE (TBP) + SLEEVE (TC1) with macro gating
        from strategies.engines.tbp_v3_hybrid_sleeve_v1 import (
            evaluate_tbp_v3_hybrid_sleeve_v1,
        )
        signal = evaluate_tbp_v3_hybrid_sleeve_v1(
            strategy=strategy,
            account=account,
            assignment=assignment,
            symbol=symbol,
            now_ts=timezone.now(),
            bar_close_time=bar_close_time or "",
            dry_run=dry_run,
        )
        # CORE (TBP) results have no job_id — dispatcher creates job.
        # SLEEVE (TC1) results already have job_id from internal creation.
        if (
            signal.ok
            and signal.signal_type
            and not signal.job_id
            and not dry_run
        ):
            job = create_place_order_job(
                request=request,
                strategy=strategy,
                account=account,
                assignment=assignment,
                signal=signal,
                user=user,
                bar_close_time=bar_close_time,
            )
            signal.job_id = job.id
            signal.reason = "job_queued"
            # Annotate job with portfolio audit from wrapper
            if signal.details and signal.details.get("portfolio"):
                try:
                    portfolio = signal.details["portfolio"]
                    job.payload["portfolio"] = portfolio
                    job.payload["macro_label"] = (
                        portfolio.get("macro_regime_label") or "UNKNOWN"
                    )
                    job.payload["macro_provider"] = "MACRO_PROVIDER_V2"
                    job.save(update_fields=["payload"])
                except Exception:
                    logger.warning(
                        "[HYBRID] Failed to annotate CORE job %s with portfolio audit",
                        job.id,
                    )
        return signal
    else:
        log_signal_rejected(
            request, strategy.id, account.id, symbol,
            reason="unknown_template",
            details={"template_slug": template_slug},
        )
        return SignalResult(
            ok=False,
            symbol=symbol,
            reason=f"unknown_template:{template_slug}",
        )

    # === TBP path (trendline-break-pocket-ali) ===
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
        override_lots = manual_params.get("lots")  # Optional explicit lots
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
            override_lots=override_lots,
        )
    else:
        # AUTO MODE: Fetch rates and evaluate full signal logic
        h4_rates = None
        d1_rates = None

        try:
            # Fetch H4 rates (need at least lookback_bars + buffer)
            h4_count = config.trendline_lookback_bars + 50
            h4_rates = fetch_rates(account, symbol, "H4", count=min(h4_count, 300))
            logger.info(f"Fetched {len(h4_rates)} H4 bars for {symbol}")

            # Fetch D1 rates for zone context
            d1_rates = fetch_rates(account, symbol, "D1", count=100)
            logger.info(f"Fetched {len(d1_rates)} D1 bars for {symbol}")

        except RatesFetchError as e:
            logger.warning(f"Failed to fetch rates for {symbol}: {e}")
            # Return no_signal with error details
            signal = SignalResult(
                ok=True,
                signal_type=None,
                symbol=symbol,
                reason="rates_fetch_failed",
                details={"error": str(e)},
            )
            log_signal_evaluated(
                request, strategy.id, account.id, symbol,
                signal_result=signal.to_dict(),
            )
            return signal

        # Evaluate with fetched rates
        signal = evaluate_trendline_break_pocket_signal(
            strategy=strategy,
            account=account,
            assignment=assignment,
            symbol=symbol,
            config=config,
            h4_rates=h4_rates,
            d1_rates=d1_rates,
        )

    # Log evaluation
    log_signal_evaluated(
        request, strategy.id, account.id, symbol,
        signal_result=signal.to_dict(),
    )

    # If signal generated, create job (skip in dry-run mode)
    if signal.ok and signal.signal_type and not dry_run:
        job = create_place_order_job(
            request=request,
            strategy=strategy,
            account=account,
            assignment=assignment,
            signal=signal,
            user=user,
            bar_close_time=bar_close_time,
        )
        signal.job_id = job.id
        signal.reason = "job_queued"

    return signal
