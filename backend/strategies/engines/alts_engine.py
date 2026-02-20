"""
ALTS — Adaptive Liquidity Trap Scalper Engine

M5 execution timeframe, M15 regime context.

Algorithm summary:
  1. Regime filter (M15): ADX < threshold, EMA20-EMA50 within bounds,
     ATR percentile in range → RANGE mode
  2. Fractal swings (M5): pivot highs/lows with configurable strength
  3. Liquidity pool clustering: ≥2 swings within cluster_atr_mult × ATR14
  4. Sweep detection: wick breaches pool, close reclaims
  5. Displacement: large body bar opposite to sweep direction
  6. Confirmation: close beyond displacement midpoint within N bars
  7. Entry at market (next bar open)
  8. SL beyond sweep extreme + buffer
  9. TP via dynamic R:R from ATR percentile

Safety: demo only, max 0.02 lots, EURUSD/GBPUSD only, all checks via
risk_manager.check_risk_gates().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from django.utils import timezone

from strategies.indicators import (
    compute_atr,
    compute_atr_series,
    compute_adx,
    compute_ema,
    find_pivot_highs,
    find_pivot_lows,
    atr_percentile,
    body_size,
    bar_direction,
    bar_midpoint,
    range_size,
)
from strategies.execution_guards import (
    normalize_prices,
    validate_sl_tp_placement,
    validate_min_stop_distance,
    check_spread_gate,
    get_pip_size,
)
from strategies.risk_manager import (
    check_risk_gates,
    record_signal_event,
    increment_daily_trade_count,
    REGIME_NOT_RANGE,
    DATA_MISSING,
    NO_SIGNAL,
    SHOCK_CANDLE_PAUSE,
    SPREAD_TOO_WIDE,
    MIN_STOP_VIOLATION,
    ORDER_PLACED,
)
from strategies.models import StrategyRuntimeEvent

logger = logging.getLogger(__name__)

ALTS_TEMPLATE_SLUG = "adaptive-liquidity-trap-scalper"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ALTSConfig:
    """Configuration for ALTS engine, loaded from strategy.filters."""

    # Fractal detection
    fractal_left: int = 2
    fractal_right: int = 2

    # Liquidity pool clustering
    pool_cluster_atr_mult: float = 1.0
    min_pool_touches: int = 2

    # Sweep detection
    sweep_wick_breach_atr: float = 0.5
    sweep_buffer_atr: float = 0.3

    # Displacement
    displacement_body_atr: float = 1.0

    # Confirmation
    confirm_within_bars: int = 3

    # R:R (dynamic from ATR percentile)
    rr_low: float = 1.5
    rr_high: float = 2.5
    rr_min: float = 1.2
    rr_max: float = 3.0

    # Regime filter (M15)
    adx_max: float = 25.0
    ema_dist_atr_mult: float = 1.0
    atrp_min: float = 20.0
    atrp_max: float = 80.0

    # Shock candle
    shock_candle_atr_mult: float = 3.0

    # Risk limits
    max_trades_per_day: int = 10
    daily_loss_cap_r: float = 3.0
    max_concurrent_positions: int = 1
    no_trade_hours: List[int] = field(default_factory=list)

    # Pairs
    pairs_enabled: List[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD"])

    @classmethod
    def from_filters(cls, filters: dict) -> "ALTSConfig":
        """Create config from strategy.filters JSON."""
        return cls(
            fractal_left=filters.get("alts_fractal_left", 2),
            fractal_right=filters.get("alts_fractal_right", 2),
            pool_cluster_atr_mult=filters.get("alts_pool_cluster_atr_mult", 1.0),
            min_pool_touches=filters.get("alts_min_pool_touches", 2),
            sweep_wick_breach_atr=filters.get("alts_sweep_wick_breach_atr", 0.5),
            sweep_buffer_atr=filters.get("alts_sweep_buffer_atr", 0.3),
            displacement_body_atr=filters.get("alts_displacement_body_atr", 1.0),
            confirm_within_bars=filters.get("alts_confirm_within_bars", 3),
            rr_low=filters.get("alts_rr_low", 1.5),
            rr_high=filters.get("alts_rr_high", 2.5),
            rr_min=filters.get("alts_rr_min", 1.2),
            rr_max=filters.get("alts_rr_max", 3.0),
            adx_max=filters.get("alts_adx_max", 25.0),
            ema_dist_atr_mult=filters.get("alts_ema_dist_atr_mult", 1.0),
            atrp_min=filters.get("alts_atrp_min", 20.0),
            atrp_max=filters.get("alts_atrp_max", 80.0),
            shock_candle_atr_mult=filters.get("alts_shock_candle_atr_mult", 3.0),
            max_trades_per_day=filters.get("alts_max_trades_per_day", 10),
            daily_loss_cap_r=filters.get("alts_daily_loss_cap_r", 3.0),
            max_concurrent_positions=filters.get("alts_max_concurrent_positions", 1),
            no_trade_hours=filters.get("alts_no_trade_hours", []),
            pairs_enabled=filters.get("pairs_enabled", ["EURUSD", "GBPUSD"]),
        )


# ---------------------------------------------------------------------------
# Sub-routines
# ---------------------------------------------------------------------------

def _detect_regime_m15(
    m15_bars: List[Dict[str, Any]],
    config: ALTSConfig,
) -> Tuple[str, Dict[str, Any]]:
    """
    Determine M15 regime for ALTS.

    RANGE regime required: ADX < adx_max, EMA20-EMA50 distance within bounds,
    ATR percentile within [atrp_min, atrp_max].

    Returns (regime, diagnostics):
        regime: "RANGE" or "OFF"
        diagnostics: dict with indicator values for event logging
    """
    n = len(m15_bars)
    diag: Dict[str, Any] = {}

    if n < 60:
        diag["error"] = f"insufficient_m15_bars:{n}"
        return "OFF", diag

    # ADX (period=14)
    adx_series = compute_adx(m15_bars, period=14)
    latest_adx = adx_series[-1]

    if latest_adx is None:
        diag["error"] = "adx_none"
        return "OFF", diag

    adx_val = latest_adx["adx"]
    diag["adx"] = adx_val
    diag["plus_di"] = latest_adx["plus_di"]
    diag["minus_di"] = latest_adx["minus_di"]

    if adx_val >= config.adx_max:
        diag["reject"] = f"adx={adx_val:.1f} >= max={config.adx_max}"
        return "OFF", diag

    # EMA20 vs EMA50 distance
    ema20 = compute_ema(m15_bars, period=20)
    ema50 = compute_ema(m15_bars, period=50)

    if ema20[-1] is None or ema50[-1] is None:
        diag["error"] = "ema_none"
        return "OFF", diag

    ema_dist = abs(ema20[-1] - ema50[-1])
    atr14_m15 = compute_atr(m15_bars, period=14)
    diag["ema20"] = round(ema20[-1], 5)
    diag["ema50"] = round(ema50[-1], 5)
    diag["ema_dist"] = round(ema_dist, 6)
    diag["atr14_m15"] = round(atr14_m15, 6)

    if atr14_m15 > 0 and ema_dist > config.ema_dist_atr_mult * atr14_m15:
        diag["reject"] = (
            f"ema_dist={ema_dist:.6f} > "
            f"{config.ema_dist_atr_mult}*ATR={config.ema_dist_atr_mult * atr14_m15:.6f}"
        )
        return "OFF", diag

    # ATR percentile (M15)
    atr_ser = compute_atr_series(m15_bars, period=14)
    atrp = atr_percentile(atr_ser, lookback=50, idx=n - 1)
    diag["atr_percentile"] = atrp

    if atrp is not None:
        if atrp < config.atrp_min or atrp > config.atrp_max:
            diag["reject"] = (
                f"atrp={atrp:.1f} outside [{config.atrp_min}, {config.atrp_max}]"
            )
            return "OFF", diag

    diag["regime"] = "RANGE"
    return "RANGE", diag


def _find_liquidity_pools(
    bars: List[Dict[str, Any]],
    strength: int,
    atr14: float,
    cluster_mult: float,
    min_touches: int,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Find liquidity pools from fractal swings on M5.

    A liquidity pool is a cluster of ≥min_touches swing levels within
    cluster_mult × ATR14 of each other.

    Returns (high_pools, low_pools) where each pool is:
        {"level": median_price, "touches": count, "indices": [bar_indices]}
    """
    pivot_highs = find_pivot_highs(bars, strength=strength)
    pivot_lows = find_pivot_lows(bars, strength=strength)

    def _cluster(pivots: List[Tuple[int, float]], cluster_dist: float):
        if not pivots:
            return []
        # Sort by price
        sorted_pivots = sorted(pivots, key=lambda p: p[1])
        pools = []
        current_group = [sorted_pivots[0]]

        for i in range(1, len(sorted_pivots)):
            if sorted_pivots[i][1] - current_group[-1][1] <= cluster_dist:
                current_group.append(sorted_pivots[i])
            else:
                if len(current_group) >= min_touches:
                    prices = [p[1] for p in current_group]
                    median_price = sorted(prices)[len(prices) // 2]
                    pools.append({
                        "level": median_price,
                        "touches": len(current_group),
                        "indices": [p[0] for p in current_group],
                    })
                current_group = [sorted_pivots[i]]

        # Final group
        if len(current_group) >= min_touches:
            prices = [p[1] for p in current_group]
            median_price = sorted(prices)[len(prices) // 2]
            pools.append({
                "level": median_price,
                "touches": len(current_group),
                "indices": [p[0] for p in current_group],
            })

        return pools

    cluster_dist = cluster_mult * atr14
    high_pools = _cluster(pivot_highs, cluster_dist)
    low_pools = _cluster(pivot_lows, cluster_dist)

    return high_pools, low_pools


def _detect_sweep(
    bars: List[Dict[str, Any]],
    pool_level: float,
    pool_type: str,
    sweep_breach_dist: float,
    start_idx: int,
) -> Optional[Dict[str, Any]]:
    """
    Detect a liquidity sweep on the most recent bars.

    For HIGH pool (supply-side sweep = bearish trap):
        - Wick extends above pool_level by ≥ sweep_breach_dist
        - Close is below pool_level (reclaim)

    For LOW pool (demand-side sweep = bullish trap):
        - Wick extends below pool_level by ≥ sweep_breach_dist
        - Close is above pool_level (reclaim)

    Scans backward from the end of bars. Returns sweep info or None.
    """
    for i in range(len(bars) - 1, max(start_idx, 0), -1):
        bar = bars[i]
        h = float(bar["high"])
        l = float(bar["low"])
        c = float(bar["close"])

        if pool_type == "high":
            # Wick pokes above pool, close reclaims below
            if h >= pool_level + sweep_breach_dist and c < pool_level:
                return {
                    "bar_idx": i,
                    "sweep_high": h,
                    "sweep_low": l,
                    "close": c,
                    "pool_level": pool_level,
                    "pool_type": pool_type,
                }
        elif pool_type == "low":
            # Wick pokes below pool, close reclaims above
            if l <= pool_level - sweep_breach_dist and c > pool_level:
                return {
                    "bar_idx": i,
                    "sweep_high": h,
                    "sweep_low": l,
                    "close": c,
                    "pool_level": pool_level,
                    "pool_type": pool_type,
                }

    return None


def _detect_displacement(
    bars: List[Dict[str, Any]],
    sweep: Dict[str, Any],
    displacement_min_body: float,
) -> Optional[Dict[str, Any]]:
    """
    Detect displacement bar after a sweep.

    Displacement = next bar after sweep with body ≥ displacement_min_body,
    in the opposite direction of the sweep.

    After high-pool sweep (bearish trap) → expect bullish displacement (BUY).
    After low-pool sweep (bullish trap) → expect bearish displacement (SELL).
    """
    sweep_idx = sweep["bar_idx"]

    # Look at bar immediately after sweep
    if sweep_idx + 1 >= len(bars):
        return None

    disp_bar = bars[sweep_idx + 1]
    disp_body = body_size(disp_bar)
    disp_dir = bar_direction(disp_bar)

    if disp_body < displacement_min_body:
        return None

    expected_dir = "bull" if sweep["pool_type"] == "high" else "bear"
    if disp_dir != expected_dir:
        return None

    return {
        "bar_idx": sweep_idx + 1,
        "body": disp_body,
        "direction": disp_dir,
        "midpoint": bar_midpoint(disp_bar),
        "open": float(disp_bar["open"]),
        "close": float(disp_bar["close"]),
        "high": float(disp_bar["high"]),
        "low": float(disp_bar["low"]),
    }


def _detect_confirmation(
    bars: List[Dict[str, Any]],
    displacement: Dict[str, Any],
    within_bars: int,
) -> Optional[Dict[str, Any]]:
    """
    Detect confirmation: close beyond displacement midpoint within N bars.
    """
    disp_idx = displacement["bar_idx"]
    disp_mid = displacement["midpoint"]
    disp_dir = displacement["direction"]

    for i in range(disp_idx + 1, min(disp_idx + 1 + within_bars, len(bars))):
        bar = bars[i]
        c = float(bar["close"])

        if disp_dir == "bull" and c > disp_mid:
            return {"bar_idx": i, "close": c, "midpoint": disp_mid}
        if disp_dir == "bear" and c < disp_mid:
            return {"bar_idx": i, "close": c, "midpoint": disp_mid}

    return None


def _compute_dynamic_rr(
    atrp_value: Optional[float],
    config: ALTSConfig,
) -> float:
    """
    Compute dynamic R:R from ATR percentile.

    Lower ATR percentile (quieter market) → higher R:R (wider target).
    Higher ATR percentile (volatile) → lower R:R (tighter target).

    Linear interpolation between rr_low and rr_high, clamped to [rr_min, rr_max].
    """
    if atrp_value is None:
        return config.rr_low  # Default to conservative

    # Invert: low percentile → high RR
    # atrp_min=20 → rr_high, atrp_max=80 → rr_low
    pct_range = config.atrp_max - config.atrp_min
    if pct_range <= 0:
        return config.rr_low

    # Normalize to 0-1 (0 = quiet, 1 = volatile)
    norm = (atrp_value - config.atrp_min) / pct_range
    norm = max(0.0, min(1.0, norm))

    # Interpolate (inverted: quiet → high RR)
    rr = config.rr_high - norm * (config.rr_high - config.rr_low)
    return max(config.rr_min, min(config.rr_max, rr))


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate_alts(
    assignment,
    symbol: str,
    now_ts: datetime,
    bar_close_time: str = "",
) -> "SignalResult":
    """
    Evaluate ALTS signal for a symbol.

    Called by signal_engine.py dispatch or directly by M5 scheduler.

    Returns a SignalResult (imported from signal_engine to avoid circular).
    """
    # Lazy import to avoid circular dependency
    from strategies.signal_engine import (
        SignalResult,
        fetch_rates,
        RatesFetchError,
        calculate_lot_size,
    )

    strategy = assignment.strategy
    account = assignment.account
    filters = strategy.filters or {}
    config = ALTSConfig.from_filters(filters)

    risk_pct = float(
        assignment.risk_per_trade_override_pct
        or strategy.risk_per_trade_pct
        or 1.0
    )

    diag: Dict[str, Any] = {
        "engine": "alts",
        "symbol": symbol,
        "bar_close_time": bar_close_time,
    }

    # ---------------------------------------------------------------
    # Step 1: Risk gates
    # ---------------------------------------------------------------
    risk_limits = {
        "daily_loss_cap_r": config.daily_loss_cap_r,
        "daily_trade_cap": config.max_trades_per_day,
        "max_concurrent_positions": config.max_concurrent_positions,
        "no_trade_hours": config.no_trade_hours,
    }

    allowed, reason_code = check_risk_gates(
        assignment=assignment,
        strategy_key=ALTS_TEMPLATE_SLUG,
        symbol=symbol,
        config_risk_limits=risk_limits,
        now_ts=now_ts,
    )

    if not allowed:
        diag["risk_gate"] = reason_code
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_RISK_THROTTLED,
            reason_code=reason_code,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"risk_gate:{reason_code}",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 2: Fetch rates
    # ---------------------------------------------------------------
    try:
        m5_bars = fetch_rates(account, symbol, "M5", count=300)
        m15_bars = fetch_rates(account, symbol, "M15", count=100)
    except RatesFetchError as e:
        diag["error"] = f"rates_fetch:{e}"
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=DATA_MISSING,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"rates_fetch_failed:{e}",
            details=diag,
        )

    diag["m5_bars"] = len(m5_bars)
    diag["m15_bars"] = len(m15_bars)

    if len(m5_bars) < 60 or len(m15_bars) < 60:
        diag["error"] = "insufficient_bars"
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=DATA_MISSING,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason="insufficient_bars",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 3: Regime filter (M15)
    # ---------------------------------------------------------------
    regime, regime_diag = _detect_regime_m15(m15_bars, config)
    diag["regime"] = regime
    diag["regime_diag"] = regime_diag

    if regime != "RANGE":
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=REGIME_NOT_RANGE,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"regime_off:{regime_diag.get('reject', 'unknown')}",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 4: Shock candle check (M5)
    # ---------------------------------------------------------------
    atr14_m5 = compute_atr(m5_bars, period=14)
    diag["atr14_m5"] = round(atr14_m5, 6)

    if atr14_m5 > 0:
        last_bar = m5_bars[-1]
        last_range = range_size(last_bar)
        if last_range > config.shock_candle_atr_mult * atr14_m5:
            diag["shock_candle"] = {
                "range": round(last_range, 6),
                "threshold": round(config.shock_candle_atr_mult * atr14_m5, 6),
            }
            record_signal_event(
                assignment=assignment,
                strategy_key=ALTS_TEMPLATE_SLUG,
                symbol=symbol,
                event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
                reason_code=SHOCK_CANDLE_PAUSE,
                payload=diag,
                bar_close_time=bar_close_time,
            )
            return SignalResult(
                ok=True,
                signal_type=None,
                symbol=symbol,
                reason="shock_candle_pause",
                details=diag,
            )

    # ---------------------------------------------------------------
    # Step 5: Spread gate (log-only)
    # ---------------------------------------------------------------
    spread_ok, spread_reason = check_spread_gate(symbol, max_spread_pips=3.0)
    diag["spread_gate"] = spread_reason

    # ---------------------------------------------------------------
    # Step 6: Liquidity pools (M5)
    # ---------------------------------------------------------------
    if atr14_m5 <= 0:
        diag["error"] = "atr14_m5_zero"
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=DATA_MISSING,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason="atr14_m5_zero",
            details=diag,
        )

    high_pools, low_pools = _find_liquidity_pools(
        bars=m5_bars,
        strength=config.fractal_left,  # Use fractal_left for both sides
        atr14=atr14_m5,
        cluster_mult=config.pool_cluster_atr_mult,
        min_touches=config.min_pool_touches,
    )

    diag["high_pools"] = len(high_pools)
    diag["low_pools"] = len(low_pools)

    if not high_pools and not low_pools:
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=NO_SIGNAL,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason="no_liquidity_pools",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 7: Sweep → Displacement → Confirmation (scan all pools)
    # ---------------------------------------------------------------
    sweep_breach_dist = config.sweep_wick_breach_atr * atr14_m5
    displacement_min_body = config.displacement_body_atr * atr14_m5

    # Scan from most recent pools first
    best_signal = None

    # Check high pools (potential BUY after bearish trap sweep)
    for pool in sorted(high_pools, key=lambda p: max(p["indices"]), reverse=True):
        sweep = _detect_sweep(
            m5_bars, pool["level"], "high", sweep_breach_dist,
            start_idx=max(pool["indices"]) if pool["indices"] else 0,
        )
        if not sweep:
            continue

        displacement = _detect_displacement(m5_bars, sweep, displacement_min_body)
        if not displacement:
            continue

        confirmation = _detect_confirmation(m5_bars, displacement, config.confirm_within_bars)
        if not confirmation:
            continue

        best_signal = {
            "side": "BUY",
            "pool": pool,
            "sweep": sweep,
            "displacement": displacement,
            "confirmation": confirmation,
        }
        break  # Take first confirmed signal

    # Check low pools (potential SELL after bullish trap sweep)
    if not best_signal:
        for pool in sorted(low_pools, key=lambda p: max(p["indices"]), reverse=True):
            sweep = _detect_sweep(
                m5_bars, pool["level"], "low", sweep_breach_dist,
                start_idx=max(pool["indices"]) if pool["indices"] else 0,
            )
            if not sweep:
                continue

            displacement = _detect_displacement(m5_bars, sweep, displacement_min_body)
            if not displacement:
                continue

            confirmation = _detect_confirmation(m5_bars, displacement, config.confirm_within_bars)
            if not confirmation:
                continue

            best_signal = {
                "side": "SELL",
                "pool": pool,
                "sweep": sweep,
                "displacement": displacement,
                "confirmation": confirmation,
            }
            break

    if not best_signal:
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=NO_SIGNAL,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason="no_sweep_displacement_confirmation",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 8: Compute entry / SL / TP
    # ---------------------------------------------------------------
    side = best_signal["side"]
    sweep = best_signal["sweep"]
    pip = get_pip_size(symbol)

    # Entry: market at next bar open (use last bar close as proxy)
    entry_price = float(m5_bars[-1]["close"])

    # SL: beyond sweep extreme + buffer
    sl_buffer = config.sweep_buffer_atr * atr14_m5
    if side == "BUY":
        sl_price = sweep["sweep_low"] - sl_buffer
    else:
        sl_price = sweep["sweep_high"] + sl_buffer

    # Dynamic R:R from ATR percentile
    m5_atr_series = compute_atr_series(m5_bars, period=14)
    m5_atrp = atr_percentile(m5_atr_series, lookback=50, idx=len(m5_bars) - 1)
    rr = _compute_dynamic_rr(m5_atrp, config)
    diag["rr"] = round(rr, 2)
    diag["atrp_m5"] = m5_atrp

    # TP
    stop_dist = abs(entry_price - sl_price)
    if side == "BUY":
        tp_price = entry_price + rr * stop_dist
    else:
        tp_price = entry_price - rr * stop_dist

    # Normalize
    entry_price, sl_price, tp_price = normalize_prices(
        entry_price, sl_price, tp_price, side, symbol
    )

    # Validate SL/TP placement
    valid, reason = validate_sl_tp_placement(entry_price, sl_price, tp_price, side)
    if not valid:
        diag["sl_tp_error"] = reason
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=MIN_STOP_VIOLATION,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"sl_tp_invalid:{reason}",
            details=diag,
        )

    # Min stop distance
    valid_stop, stop_reason = validate_min_stop_distance(
        entry_price, sl_price, symbol, min_pips=3.0
    )
    if not valid_stop:
        diag["min_stop_error"] = stop_reason
        record_signal_event(
            assignment=assignment,
            strategy_key=ALTS_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=MIN_STOP_VIOLATION,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"min_stop_violation:{stop_reason}",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 9: Lot sizing
    # ---------------------------------------------------------------
    stop_pips = abs(entry_price - sl_price) / pip
    lots, lot_warning = calculate_lot_size(account, risk_pct, stop_pips, symbol)
    diag["lots"] = lots
    diag["stop_pips"] = round(stop_pips, 1)
    if lot_warning:
        diag["lot_warning"] = lot_warning

    # ---------------------------------------------------------------
    # Step 10: Record signal fired + increment trade count
    # ---------------------------------------------------------------
    diag["signal"] = {
        "side": side,
        "entry": entry_price,
        "sl": sl_price,
        "tp": tp_price,
        "lots": lots,
        "rr": round(rr, 2),
        "pool_level": best_signal["pool"]["level"],
        "sweep_bar": sweep["bar_idx"],
        "displacement_bar": best_signal["displacement"]["bar_idx"],
        "confirmation_bar": best_signal["confirmation"]["bar_idx"],
    }

    record_signal_event(
        assignment=assignment,
        strategy_key=ALTS_TEMPLATE_SLUG,
        symbol=symbol,
        event_type=StrategyRuntimeEvent.EVENT_SIGNAL_FIRED,
        reason_code=ORDER_PLACED,
        payload=diag,
        bar_close_time=bar_close_time,
    )

    # Increment daily trade count
    increment_daily_trade_count(assignment, ALTS_TEMPLATE_SLUG, symbol)

    return SignalResult(
        ok=True,
        signal_type=side,
        symbol=symbol,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        lots=lots,
        reason="alts_signal",
        details=diag,
    )
