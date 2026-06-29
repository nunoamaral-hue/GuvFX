"""
SCE — Structural Continuation Engine

H1 execution timeframe, H4 bias context.

Algorithm summary:
  1. Bias detection (H4): fractal swings (3/3) → HH+HL=Bull, LH+LL=Bear;
     close vs EMA50; ADX ≥ adx_min.  Else NONE → skip.
  2. Break of Structure (H1): latest swing H/L (fractal 3/3);
     Bull BOS: close > swing_high + buffer × ATR14_H1
     Bear BOS: close < swing_low - buffer × ATR14_H1
  3. BOS must align with bias (bull + bull or bear + bear)
  4. Pullback: Fib retrace 38-62% of impulse leg; must hold EMA20
  5. Rejection candle: body ≥ threshold × ATR14, aligns with bias
  6. Entry at market (next H1 bar open)
  7. SL beyond pullback extreme + buffer
  8. TP via R:R (base 2.0, strong 3.0 if ADX ≥ 30)

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
    compute_adx,
    compute_ema,
    find_pivot_highs,
    find_pivot_lows,
    body_size,
    bar_direction,
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
    NO_BIAS,
    NO_BOS,
    BOS_CONFLICTS_BIAS,
    NO_PULLBACK,
    NO_REJECTION,
    DATA_MISSING,
    NO_SIGNAL,
    MIN_STOP_VIOLATION,
    ORDER_PLACED,
)
from strategies.models import StrategyRuntimeEvent

logger = logging.getLogger(__name__)

SCE_TEMPLATE_SLUG = "structural-continuation-engine"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SCEConfig:
    """Configuration for SCE engine, loaded from strategy.filters."""

    # Fractal detection
    fractal_left: int = 3
    fractal_right: int = 3

    # BOS
    bos_buffer_atr: float = 0.3

    # Pullback / Fibonacci
    fib_retrace_min: float = 0.38
    fib_retrace_max: float = 0.62

    # Rejection candle
    rejection_requires_body_atr: float = 0.5

    # Pullback SL buffer
    pullback_buffer_atr: float = 0.5

    # Regime / bias (H4)
    adx_min: float = 22.0

    # R:R
    rr_base: float = 2.0
    rr_strong: float = 3.0
    adx_strong_threshold: float = 30.0

    # Risk limits
    max_trades_per_day: int = 4
    daily_loss_cap_r: float = 3.0
    weekly_loss_cap_r: float = 6.0
    max_concurrent_positions: int = 1
    consecutive_loss_pause: int = 3
    cooldown_minutes: int = 120
    no_trade_hours: List[int] = field(default_factory=list)

    # Pairs
    pairs_enabled: List[str] = field(default_factory=lambda: ["EURUSD", "GBPUSD"])

    @classmethod
    def from_filters(cls, filters: dict) -> "SCEConfig":
        """Create config from strategy.filters JSON."""
        return cls(
            fractal_left=filters.get("sce_fractal_left", 3),
            fractal_right=filters.get("sce_fractal_right", 3),
            bos_buffer_atr=filters.get("sce_bos_buffer_atr", 0.3),
            fib_retrace_min=filters.get("sce_fib_retrace_min", 0.38),
            fib_retrace_max=filters.get("sce_fib_retrace_max", 0.62),
            rejection_requires_body_atr=filters.get("sce_rejection_body_atr", 0.5),
            pullback_buffer_atr=filters.get("sce_pullback_buffer_atr", 0.5),
            adx_min=filters.get("sce_adx_min", 22.0),
            rr_base=filters.get("sce_rr_base", 2.0),
            rr_strong=filters.get("sce_rr_strong", 3.0),
            adx_strong_threshold=filters.get("sce_adx_strong_threshold", 30.0),
            max_trades_per_day=filters.get("sce_max_trades_per_day", 4),
            daily_loss_cap_r=filters.get("sce_daily_loss_cap_r", 3.0),
            weekly_loss_cap_r=filters.get("sce_weekly_loss_cap_r", 6.0),
            max_concurrent_positions=filters.get("sce_max_concurrent_positions", 1),
            consecutive_loss_pause=filters.get("sce_consecutive_loss_pause", 3),
            cooldown_minutes=filters.get("sce_cooldown_minutes", 120),
            no_trade_hours=filters.get("sce_no_trade_hours", []),
            pairs_enabled=filters.get("pairs_enabled", ["EURUSD", "GBPUSD"]),
        )


# ---------------------------------------------------------------------------
# Sub-routines
# ---------------------------------------------------------------------------

def _detect_bias_h4(
    h4_bars: List[Dict[str, Any]],
    config: SCEConfig,
) -> Tuple[str, Dict[str, Any]]:
    """
    Detect structural bias on H4 using fractal swings + EMA + ADX.

    BULL bias: HH + HL pattern, close above EMA50, ADX ≥ adx_min
    BEAR bias: LH + LL pattern, close below EMA50, ADX ≥ adx_min
    Otherwise: NONE

    Returns (bias, diagnostics).
    """
    n = len(h4_bars)
    diag: Dict[str, Any] = {}

    if n < 60:
        diag["error"] = f"insufficient_h4_bars:{n}"
        return "NONE", diag

    # Fractal swings (strength=3 for H4)
    pivot_highs = find_pivot_highs(h4_bars, strength=config.fractal_left)
    pivot_lows = find_pivot_lows(h4_bars, strength=config.fractal_left)

    diag["pivot_highs"] = len(pivot_highs)
    diag["pivot_lows"] = len(pivot_lows)

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        diag["reject"] = "insufficient_pivots"
        return "NONE", diag

    # Get last 2 swing highs and lows
    last_2_highs = pivot_highs[-2:]
    last_2_lows = pivot_lows[-2:]

    h1_idx, h1_val = last_2_highs[0]
    h2_idx, h2_val = last_2_highs[1]
    l1_idx, l1_val = last_2_lows[0]
    l2_idx, l2_val = last_2_lows[1]

    diag["swing_highs"] = [
        {"idx": h1_idx, "val": round(h1_val, 5)},
        {"idx": h2_idx, "val": round(h2_val, 5)},
    ]
    diag["swing_lows"] = [
        {"idx": l1_idx, "val": round(l1_val, 5)},
        {"idx": l2_idx, "val": round(l2_val, 5)},
    ]

    # HH + HL = bullish, LH + LL = bearish
    hh = h2_val > h1_val  # Higher High
    hl = l2_val > l1_val  # Higher Low
    lh = h2_val < h1_val  # Lower High
    ll = l2_val < l1_val  # Lower Low

    structure_bias = "NONE"
    if hh and hl:
        structure_bias = "BULL"
    elif lh and ll:
        structure_bias = "BEAR"

    diag["structure"] = {
        "hh": hh, "hl": hl, "lh": lh, "ll": ll,
        "bias": structure_bias,
    }

    if structure_bias == "NONE":
        diag["reject"] = "no_clear_structure"
        return "NONE", diag

    # EMA50 check
    ema50 = compute_ema(h4_bars, period=50)
    latest_ema50 = ema50[-1]
    latest_close = float(h4_bars[-1]["close"])

    diag["ema50"] = round(latest_ema50, 5) if latest_ema50 else None
    diag["close"] = round(latest_close, 5)

    if latest_ema50 is None:
        diag["reject"] = "ema50_none"
        return "NONE", diag

    if structure_bias == "BULL" and latest_close <= latest_ema50:
        diag["reject"] = "close_below_ema50_for_bull"
        return "NONE", diag
    if structure_bias == "BEAR" and latest_close >= latest_ema50:
        diag["reject"] = "close_above_ema50_for_bear"
        return "NONE", diag

    # ADX check
    adx_series = compute_adx(h4_bars, period=14)
    latest_adx = adx_series[-1]

    if latest_adx is None:
        diag["reject"] = "adx_none"
        return "NONE", diag

    adx_val = latest_adx["adx"]
    diag["adx"] = adx_val

    if adx_val < config.adx_min:
        diag["reject"] = f"adx={adx_val:.1f} < min={config.adx_min}"
        return "NONE", diag

    diag["bias"] = structure_bias
    return structure_bias, diag


def _detect_bos_h1(
    h1_bars: List[Dict[str, Any]],
    bias: str,
    atr14_h1: float,
    config: SCEConfig,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Detect Break of Structure on H1.

    Bull BOS: close > last_swing_high + bos_buffer × ATR14
    Bear BOS: close < last_swing_low - bos_buffer × ATR14

    Returns (bos_info, diagnostics).
    bos_info is None if no BOS detected.
    """
    diag: Dict[str, Any] = {}

    pivot_highs = find_pivot_highs(h1_bars, strength=config.fractal_left)
    pivot_lows = find_pivot_lows(h1_bars, strength=config.fractal_left)

    diag["h1_pivot_highs"] = len(pivot_highs)
    diag["h1_pivot_lows"] = len(pivot_lows)

    buffer = config.bos_buffer_atr * atr14_h1

    if bias == "BULL":
        if not pivot_highs:
            diag["reject"] = "no_h1_pivot_highs"
            return None, diag

        # Find the latest swing high
        last_sh_idx, last_sh_val = pivot_highs[-1]
        diag["last_swing_high"] = {"idx": last_sh_idx, "val": round(last_sh_val, 5)}
        diag["bos_threshold"] = round(last_sh_val + buffer, 5)

        # Scan for close breaking above
        for i in range(len(h1_bars) - 1, last_sh_idx, -1):
            close = float(h1_bars[i]["close"])
            if close > last_sh_val + buffer:
                bos = {
                    "bar_idx": i,
                    "close": close,
                    "swing_idx": last_sh_idx,
                    "swing_val": last_sh_val,
                    "direction": "BULL",
                }
                diag["bos"] = bos
                return bos, diag

    elif bias == "BEAR":
        if not pivot_lows:
            diag["reject"] = "no_h1_pivot_lows"
            return None, diag

        last_sl_idx, last_sl_val = pivot_lows[-1]
        diag["last_swing_low"] = {"idx": last_sl_idx, "val": round(last_sl_val, 5)}
        diag["bos_threshold"] = round(last_sl_val - buffer, 5)

        for i in range(len(h1_bars) - 1, last_sl_idx, -1):
            close = float(h1_bars[i]["close"])
            if close < last_sl_val - buffer:
                bos = {
                    "bar_idx": i,
                    "close": close,
                    "swing_idx": last_sl_idx,
                    "swing_val": last_sl_val,
                    "direction": "BEAR",
                }
                diag["bos"] = bos
                return bos, diag

    diag["reject"] = "no_bos_detected"
    return None, diag


def _detect_pullback(
    h1_bars: List[Dict[str, Any]],
    bos: Dict[str, Any],
    config: SCEConfig,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Detect a Fibonacci pullback into the impulse leg after BOS.

    Impulse leg: from swing point to BOS close.
    Pullback must retrace to fib_retrace_min..fib_retrace_max of the leg
    and hold EMA20 on the correct side.

    Returns (pullback_info, diagnostics).
    """
    diag: Dict[str, Any] = {}

    bos_idx = bos["bar_idx"]
    bos_close = bos["close"]
    swing_val = bos["swing_val"]
    direction = bos["direction"]

    # Impulse leg
    if direction == "BULL":
        impulse_start = swing_val  # swing low before BOS
        impulse_end = bos_close
    else:
        impulse_start = swing_val  # swing high before BOS
        impulse_end = bos_close

    impulse_range = abs(impulse_end - impulse_start)
    if impulse_range == 0:
        diag["reject"] = "zero_impulse_range"
        return None, diag

    diag["impulse_start"] = round(impulse_start, 5)
    diag["impulse_end"] = round(impulse_end, 5)
    diag["impulse_range"] = round(impulse_range, 5)

    # Fib levels
    if direction == "BULL":
        fib_38 = impulse_end - config.fib_retrace_min * impulse_range
        fib_62 = impulse_end - config.fib_retrace_max * impulse_range
        fib_zone_high = fib_38  # 38.2% retrace is higher
        fib_zone_low = fib_62   # 61.8% retrace is lower
    else:
        fib_38 = impulse_end + config.fib_retrace_min * impulse_range
        fib_62 = impulse_end + config.fib_retrace_max * impulse_range
        fib_zone_low = fib_38   # 38.2% retrace is lower
        fib_zone_high = fib_62  # 61.8% retrace is higher

    diag["fib_zone"] = {
        "low": round(fib_zone_low, 5),
        "high": round(fib_zone_high, 5),
    }

    # EMA20 for pullback validation
    ema20 = compute_ema(h1_bars, period=20)

    # Scan for pullback into fib zone after BOS
    for i in range(bos_idx + 1, len(h1_bars)):
        bar = h1_bars[i]
        low = float(bar["low"])
        high = float(bar["high"])
        close = float(bar["close"])

        # Check if price touched the fib zone
        bar_touched_zone = False
        if direction == "BULL":
            bar_touched_zone = low <= fib_zone_high and close >= fib_zone_low
        else:
            bar_touched_zone = high >= fib_zone_low and close <= fib_zone_high

        if not bar_touched_zone:
            continue

        # Check EMA20 hold
        ema20_val = ema20[i]
        if ema20_val is None:
            continue

        ema_hold = False
        if direction == "BULL":
            ema_hold = close >= ema20_val  # Must hold above EMA20
        else:
            ema_hold = close <= ema20_val  # Must hold below EMA20

        if not ema_hold:
            continue

        pullback = {
            "bar_idx": i,
            "low": low,
            "high": high,
            "close": close,
            "ema20": ema20_val,
            "direction": direction,
        }
        diag["pullback"] = pullback
        return pullback, diag

    diag["reject"] = "no_pullback_in_fib_zone"
    return None, diag


def _detect_rejection(
    h1_bars: List[Dict[str, Any]],
    pullback: Dict[str, Any],
    atr14_h1: float,
    config: SCEConfig,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Detect a rejection candle at the pullback zone.

    Rejection = body ≥ rejection_requires_body_atr × ATR14, direction
    aligns with bias.

    Scans from pullback bar forward.
    """
    diag: Dict[str, Any] = {}

    pb_idx = pullback["bar_idx"]
    direction = pullback["direction"]
    min_body = config.rejection_requires_body_atr * atr14_h1

    diag["min_body"] = round(min_body, 6)

    # Check pullback bar itself and the next few bars
    for i in range(pb_idx, min(pb_idx + 3, len(h1_bars))):
        bar = h1_bars[i]
        b_size = body_size(bar)
        b_dir = bar_direction(bar)

        # Bull bias → need bull rejection candle
        expected_dir = "bull" if direction == "BULL" else "bear"

        if b_size >= min_body and b_dir == expected_dir:
            rejection = {
                "bar_idx": i,
                "body": round(b_size, 6),
                "direction": b_dir,
                "open": float(bar["open"]),
                "close": float(bar["close"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
            }
            diag["rejection"] = rejection
            return rejection, diag

    diag["reject"] = "no_rejection_candle"
    return None, diag


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate_sce(
    assignment,
    symbol: str,
    now_ts: datetime,
    bar_close_time: str = "",
) -> "SignalResult":
    """
    Evaluate SCE signal for a symbol.

    Called by signal_engine.py dispatch or directly by H1 scheduler.

    Returns a SignalResult (imported from signal_engine to avoid circular).
    """
    from strategies.signal_engine import (
        SignalResult,
        fetch_rates,
        RatesFetchError,
        calculate_lot_size,
    )

    strategy = assignment.strategy
    account = assignment.account
    filters = strategy.filters or {}
    config = SCEConfig.from_filters(filters)

    risk_pct = float(
        assignment.risk_per_trade_override_pct
        or strategy.risk_per_trade_pct
        or 1.0
    )

    diag: Dict[str, Any] = {
        "engine": "sce",
        "symbol": symbol,
        "bar_close_time": bar_close_time,
    }

    # ---------------------------------------------------------------
    # Step 1: Risk gates
    # ---------------------------------------------------------------
    risk_limits = {
        "daily_loss_cap_r": config.daily_loss_cap_r,
        "daily_trade_cap": config.max_trades_per_day,
        "weekly_loss_cap_r": config.weekly_loss_cap_r,
        "max_concurrent_positions": config.max_concurrent_positions,
        "consecutive_loss_pause": config.consecutive_loss_pause,
        "cooldown_minutes": config.cooldown_minutes,
        "no_trade_hours": config.no_trade_hours,
    }

    allowed, reason_code = check_risk_gates(
        assignment=assignment,
        strategy_key=SCE_TEMPLATE_SLUG,
        symbol=symbol,
        config_risk_limits=risk_limits,
        now_ts=now_ts,
    )

    if not allowed:
        diag["risk_gate"] = reason_code
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
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
        h1_bars = fetch_rates(account, symbol, "H1", count=200)
        h4_bars = fetch_rates(account, symbol, "H4", count=100)
    except RatesFetchError as e:
        diag["error"] = f"rates_fetch:{e}"
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
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

    diag["h1_bars"] = len(h1_bars)
    diag["h4_bars"] = len(h4_bars)

    if len(h1_bars) < 60 or len(h4_bars) < 60:
        diag["error"] = "insufficient_bars"
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
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
    # Step 3: Bias detection (H4)
    # ---------------------------------------------------------------
    bias, bias_diag = _detect_bias_h4(h4_bars, config)
    diag["bias"] = bias
    diag["bias_diag"] = bias_diag

    if bias == "NONE":
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=NO_BIAS,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"no_bias:{bias_diag.get('reject', 'unknown')}",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 4: Break of Structure (H1)
    # ---------------------------------------------------------------
    atr14_h1 = compute_atr(h1_bars, period=14)
    diag["atr14_h1"] = round(atr14_h1, 6)

    if atr14_h1 <= 0:
        diag["error"] = "atr14_h1_zero"
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
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
            reason="atr14_h1_zero",
            details=diag,
        )

    bos, bos_diag = _detect_bos_h1(h1_bars, bias, atr14_h1, config)
    diag["bos_diag"] = bos_diag

    if bos is None:
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=NO_BOS,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"no_bos:{bos_diag.get('reject', 'unknown')}",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 5: BOS must align with bias
    # ---------------------------------------------------------------
    if bos["direction"] != bias:
        diag["bos_conflict"] = f"bos={bos['direction']} vs bias={bias}"
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=BOS_CONFLICTS_BIAS,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"bos_conflicts_bias:{bos['direction']}_vs_{bias}",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 6: Pullback (Fib retrace 38-62%)
    # ---------------------------------------------------------------
    pullback, pb_diag = _detect_pullback(h1_bars, bos, config)
    diag["pullback_diag"] = pb_diag

    if pullback is None:
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=NO_PULLBACK,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"no_pullback:{pb_diag.get('reject', 'unknown')}",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 7: Rejection candle
    # ---------------------------------------------------------------
    rejection, rej_diag = _detect_rejection(h1_bars, pullback, atr14_h1, config)
    diag["rejection_diag"] = rej_diag

    if rejection is None:
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
            symbol=symbol,
            event_type=StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            reason_code=NO_REJECTION,
            payload=diag,
            bar_close_time=bar_close_time,
        )
        return SignalResult(
            ok=True,
            signal_type=None,
            symbol=symbol,
            reason=f"no_rejection:{rej_diag.get('reject', 'unknown')}",
            details=diag,
        )

    # ---------------------------------------------------------------
    # Step 8: Compute entry / SL / TP
    # ---------------------------------------------------------------
    side = "BUY" if bias == "BULL" else "SELL"
    pip = get_pip_size(symbol)

    # Entry: market at next H1 bar open (use last bar close as proxy)
    entry_price = float(h1_bars[-1]["close"])

    # SL: beyond pullback extreme + buffer
    sl_buffer = config.pullback_buffer_atr * atr14_h1
    if side == "BUY":
        sl_price = pullback["low"] - sl_buffer
    else:
        sl_price = pullback["high"] + sl_buffer

    # R:R: base=2.0, strong=3.0 if ADX ≥ 30
    adx_val = bias_diag.get("adx", 0)
    rr = config.rr_strong if adx_val >= config.adx_strong_threshold else config.rr_base
    diag["rr"] = rr
    diag["adx_for_rr"] = adx_val

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
            strategy_key=SCE_TEMPLATE_SLUG,
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
        entry_price, sl_price, symbol, min_pips=5.0  # SCE uses wider stops
    )
    if not valid_stop:
        diag["min_stop_error"] = stop_reason
        record_signal_event(
            assignment=assignment,
            strategy_key=SCE_TEMPLATE_SLUG,
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

    # Spread gate (log-only)
    spread_ok, spread_reason = check_spread_gate(symbol, max_spread_pips=3.0)
    diag["spread_gate"] = spread_reason

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
        "rr": rr,
        "bias": bias,
        "bos_bar": bos["bar_idx"],
        "pullback_bar": pullback["bar_idx"],
        "rejection_bar": rejection["bar_idx"],
    }

    record_signal_event(
        assignment=assignment,
        strategy_key=SCE_TEMPLATE_SLUG,
        symbol=symbol,
        event_type=StrategyRuntimeEvent.EVENT_SIGNAL_FIRED,
        reason_code=ORDER_PLACED,
        payload=diag,
        bar_close_time=bar_close_time,
    )

    increment_daily_trade_count(assignment, SCE_TEMPLATE_SLUG, symbol)

    return SignalResult(
        ok=True,
        signal_type=side,
        symbol=symbol,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        lots=lots,
        reason="sce_signal",
        details=diag,
    )
