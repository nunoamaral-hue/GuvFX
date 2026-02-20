"""
Pure indicator functions for GuvFX strategy engines.

All functions are deterministic, side-effect-free, and operate only on
plain OHLC bar dicts (keys: time, open, high, low, close, tick_volume).
No Django, no DB, no IO.

Functions migrated from zone_generator.py:
    compute_atr, find_pivot_highs, find_pivot_lows

New functions for ALTS / SCE engines:
    compute_atr_series, compute_ema, compute_adx,
    atr_percentile, body_size, range_size, bar_midpoint, bar_direction
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------

def body_size(bar: Dict[str, Any]) -> float:
    """Absolute body size of a candle: |close - open|."""
    return abs(float(bar["close"]) - float(bar["open"]))


def range_size(bar: Dict[str, Any]) -> float:
    """Full range of a candle: high - low."""
    return float(bar["high"]) - float(bar["low"])


def bar_midpoint(bar: Dict[str, Any]) -> float:
    """Midpoint of a candle: (high + low) / 2."""
    return (float(bar["high"]) + float(bar["low"])) / 2.0


def bar_direction(bar: Dict[str, Any]) -> str:
    """
    Direction of a candle based on close vs open.

    Returns "bull" if close > open, "bear" if close < open, "doji" if equal.
    """
    c = float(bar["close"])
    o = float(bar["open"])
    if c > o:
        return "bull"
    elif c < o:
        return "bear"
    return "doji"


# ---------------------------------------------------------------------------
# ATR (Average True Range)
# ---------------------------------------------------------------------------

def compute_atr(bars: List[Dict[str, Any]], period: int = 14) -> float:
    """
    Compute Average True Range over *period* bars.

    Uses the classic Wilder smoothing:
        TR = max(high - low, |high - prev_close|, |low - prev_close|)
        ATR = SMA of first *period* TRs, then EMA thereafter.

    Returns 0.0 if insufficient data.

    This is the canonical implementation; zone_generator.py re-exports it.
    """
    if len(bars) < period + 1:
        return 0.0

    true_ranges: List[float] = []
    for i in range(1, len(bars)):
        h = float(bars[i]["high"])
        l = float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return 0.0

    # Initial SMA
    atr = sum(true_ranges[:period]) / period

    # Wilder smoothing for the rest
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


def compute_atr_series(
    bars: List[Dict[str, Any]],
    period: int = 14,
) -> List[Optional[float]]:
    """
    Compute ATR for every bar, returning an aligned list.

    Returns a list of the same length as *bars* where:
      - result[0..period] = None  (insufficient data)
      - result[period..] = ATR value at that bar

    Uses Wilder smoothing (same as compute_atr).
    """
    n = len(bars)
    result: List[Optional[float]] = [None] * n

    if n < period + 1:
        return result

    # Compute true ranges (index 0 = bar[1] vs bar[0])
    true_ranges: List[float] = []
    for i in range(1, n):
        h = float(bars[i]["high"])
        l = float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        true_ranges.append(tr)

    # Initial SMA at index=period (true_ranges[0..period-1])
    atr = sum(true_ranges[:period]) / period
    result[period] = atr

    # Wilder smoothing forward
    for i in range(period, len(true_ranges)):
        atr = (atr * (period - 1) + true_ranges[i]) / period
        result[i + 1] = atr  # i+1 because true_ranges is offset by 1

    return result


# ---------------------------------------------------------------------------
# EMA (Exponential Moving Average)
# ---------------------------------------------------------------------------

def compute_ema(
    bars: List[Dict[str, Any]],
    period: int,
    field: str = "close",
) -> List[Optional[float]]:
    """
    Compute standard EMA over *field* for every bar.

    Returns a list of the same length as *bars* where:
      - result[0..period-2] = None  (insufficient data)
      - result[period-1] = SMA of first *period* values (seed)
      - result[period..] = EMA value

    Multiplier = 2 / (period + 1).
    """
    n = len(bars)
    result: List[Optional[float]] = [None] * n

    if n < period:
        return result

    # Seed with SMA
    values = [float(bars[i][field]) for i in range(period)]
    ema = sum(values) / period
    result[period - 1] = ema

    mult = 2.0 / (period + 1)

    for i in range(period, n):
        val = float(bars[i][field])
        ema = (val - ema) * mult + ema
        result[i] = ema

    return result


# ---------------------------------------------------------------------------
# ADX (Average Directional Index)
# ---------------------------------------------------------------------------

def compute_adx(
    bars: List[Dict[str, Any]],
    period: int = 14,
) -> List[Optional[Dict[str, float]]]:
    """
    Compute ADX, +DI, -DI for every bar.

    Returns a list of the same length as *bars* where each element is either
    None (insufficient data) or {"adx": float, "plus_di": float, "minus_di": float}.

    Algorithm:
      1. Compute +DM, -DM, TR per bar
      2. Wilder smooth +DM, -DM, TR over *period*
      3. +DI = 100 × smooth(+DM) / smooth(TR)
      4. -DI = 100 × smooth(-DM) / smooth(TR)
      5. DX = 100 × |+DI - -DI| / (+DI + -DI)
      6. ADX = Wilder smooth of DX over *period*
    """
    n = len(bars)
    result: List[Optional[Dict[str, float]]] = [None] * n

    # Need at least 2*period + 1 bars to get first ADX
    if n < 2 * period + 1:
        return result

    # Step 1: +DM, -DM, TR series (index 0 = bar[1] vs bar[0])
    plus_dm_list: List[float] = []
    minus_dm_list: List[float] = []
    tr_list: List[float] = []

    for i in range(1, n):
        h = float(bars[i]["high"])
        l = float(bars[i]["low"])
        ph = float(bars[i - 1]["high"])
        pl = float(bars[i - 1]["low"])
        pc = float(bars[i - 1]["close"])

        up_move = h - ph
        down_move = pl - l

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        tr = max(h - l, abs(h - pc), abs(l - pc))

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    dm_len = len(plus_dm_list)
    if dm_len < 2 * period:
        return result

    # Step 2: Wilder smooth (initial SMA, then EMA)
    smooth_plus_dm = sum(plus_dm_list[:period]) / period
    smooth_minus_dm = sum(minus_dm_list[:period]) / period
    smooth_tr = sum(tr_list[:period]) / period

    # Compute DI and DX for bars [period .. 2*period-1]
    dx_list: List[float] = []

    def _compute_di_dx(s_pdm: float, s_mdm: float, s_tr: float):
        plus_di = 100.0 * s_pdm / s_tr if s_tr > 0 else 0.0
        minus_di = 100.0 * s_mdm / s_tr if s_tr > 0 else 0.0
        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
        return plus_di, minus_di, dx

    plus_di, minus_di, dx = _compute_di_dx(smooth_plus_dm, smooth_minus_dm, smooth_tr)
    dx_list.append(dx)

    # Continue smoothing +DM/-DM/TR and collecting DX
    for i in range(period, dm_len):
        smooth_plus_dm = (smooth_plus_dm * (period - 1) + plus_dm_list[i]) / period
        smooth_minus_dm = (smooth_minus_dm * (period - 1) + minus_dm_list[i]) / period
        smooth_tr = (smooth_tr * (period - 1) + tr_list[i]) / period

        plus_di, minus_di, dx = _compute_di_dx(smooth_plus_dm, smooth_minus_dm, smooth_tr)
        dx_list.append(dx)

        # Once we have enough DX values, compute ADX
        dx_idx = len(dx_list)
        bar_idx = i + 1  # +1 because dm_list is offset by 1 from bars

        if dx_idx == period:
            # Initial ADX = SMA of first *period* DX values
            adx = sum(dx_list[:period]) / period
            result[bar_idx] = {
                "adx": round(adx, 4),
                "plus_di": round(plus_di, 4),
                "minus_di": round(minus_di, 4),
            }
        elif dx_idx > period:
            # Wilder smooth ADX
            prev_adx = result[bar_idx - 1]
            if prev_adx is not None:
                adx = (prev_adx["adx"] * (period - 1) + dx) / period
            else:
                adx = dx
            result[bar_idx] = {
                "adx": round(adx, 4),
                "plus_di": round(plus_di, 4),
                "minus_di": round(minus_di, 4),
            }

    return result


# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------

def find_pivot_highs(
    bars: List[Dict[str, Any]],
    strength: int = 2,
) -> List[Tuple[int, float]]:
    """
    Return list of (bar_index, high_value) for confirmed pivot highs.

    A pivot high at index *i* requires:
        bars[i].high > bars[j].high   for every j in [i - strength .. i + strength], j != i

    This is the canonical implementation; zone_generator.py re-exports it.
    """
    pivots: List[Tuple[int, float]] = []
    for i in range(strength, len(bars) - strength):
        h = float(bars[i]["high"])
        is_pivot = True
        for j in range(i - strength, i + strength + 1):
            if j == i:
                continue
            if float(bars[j]["high"]) >= h:
                is_pivot = False
                break
        if is_pivot:
            pivots.append((i, h))
    return pivots


def find_pivot_lows(
    bars: List[Dict[str, Any]],
    strength: int = 2,
) -> List[Tuple[int, float]]:
    """
    Return list of (bar_index, low_value) for confirmed pivot lows.

    A pivot low at index *i* requires:
        bars[i].low < bars[j].low    for every j in [i - strength .. i + strength], j != i

    This is the canonical implementation; zone_generator.py re-exports it.
    """
    pivots: List[Tuple[int, float]] = []
    for i in range(strength, len(bars) - strength):
        l = float(bars[i]["low"])
        is_pivot = True
        for j in range(i - strength, i + strength + 1):
            if j == i:
                continue
            if float(bars[j]["low"]) <= l:
                is_pivot = False
                break
        if is_pivot:
            pivots.append((i, l))
    return pivots


# ---------------------------------------------------------------------------
# ATR percentile
# ---------------------------------------------------------------------------

def atr_percentile(
    atr_series: List[Optional[float]],
    lookback: int,
    idx: int,
) -> Optional[float]:
    """
    Compute the percentile rank (0-100) of the ATR at *idx* within the
    preceding *lookback* ATR values.

    Returns None if insufficient data.
    """
    if idx < 0 or idx >= len(atr_series):
        return None

    current = atr_series[idx]
    if current is None:
        return None

    # Gather lookback values (excluding None)
    start = max(0, idx - lookback + 1)
    window = [v for v in atr_series[start:idx + 1] if v is not None]

    if len(window) < 2:
        return None

    # Percentile rank = % of values in window that are <= current
    count_le = sum(1 for v in window if v <= current)
    return round(100.0 * count_le / len(window), 2)
