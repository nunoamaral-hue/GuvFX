"""
Auto HTF Zone Generator for Trendline Break Pocket (TBP) Strategy.

Computes D1 supply / demand / pivot zones from OHLC data.
Zones are fully deterministic given the same input bars and parameters.

Algorithm:
  1. Fetch last N D1 bars (default 120)
  2. Compute ATR(14)
  3. Find swing pivot highs / lows (pivot_strength = 2)
  4. Rank pivots by recency + swing magnitude
  5. Select up to max_zones (default 3): supply‑1, pivot, demand‑1
  6. Zone width = ATR × atr_mult (default 0.8)
     - Supply zone: [high − width, high]
     - Demand zone: [low, low + width]
     - Pivot zone: [median − 0.5 × ATR, median + 0.5 × ATR]

Usage:
    from strategies.zone_generator import generate_zones_for_symbol

    zones, meta = generate_zones_for_symbol(
        bars=d1_bars,          # list of {time, open, high, low, close, tick_volume}
        symbol="EURUSD",
        atr_period=14,
        atr_mult=0.8,
        pivot_strength=2,
        max_zones=3,
    )
"""

from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ATR (Average True Range)
# ---------------------------------------------------------------------------

def compute_atr(bars: List[Dict[str, Any]], period: int = 14) -> float:
    """
    Compute Average True Range over *period* bars.

    Uses the classic Wilder smoothing:
        TR = max(high − low, |high − prev_close|, |low − prev_close|)
        ATR = SMA of first *period* TRs, then EMA thereafter.

    Returns 0.0 if insufficient data.
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
        bars[i].high > bars[j].high   for every j in [i − strength .. i + strength], j ≠ i
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
        bars[i].low < bars[j].low    for every j in [i − strength .. i + strength], j ≠ i
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
# Pivot ranking
# ---------------------------------------------------------------------------

def _rank_pivots(
    pivots: List[Tuple[int, float]],
    total_bars: int,
) -> List[Tuple[int, float, float]]:
    """
    Rank pivots by a composite score: recency + swing magnitude.

    Score = 0.6 × recency_norm  +  0.4 × magnitude_norm
    where recency_norm = bar_index / (total_bars − 1)
          magnitude_norm = |value − median_value| / max_deviation (if > 1 pivot)

    Returns [(bar_index, value, score), ...] sorted descending by score.
    """
    if not pivots:
        return []

    if len(pivots) == 1:
        return [(pivots[0][0], pivots[0][1], 1.0)]

    values = [v for _, v in pivots]
    med = statistics.median(values)
    max_dev = max(abs(v - med) for v in values) or 1.0
    max_idx = total_bars - 1 or 1

    scored = []
    for idx, val in pivots:
        recency = idx / max_idx
        magnitude = abs(val - med) / max_dev
        score = 0.6 * recency + 0.4 * magnitude
        scored.append((idx, val, score))

    scored.sort(key=lambda x: x[2], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_zones_for_symbol(
    bars: List[Dict[str, Any]],
    symbol: str,
    atr_period: int = 14,
    atr_mult: float = 0.8,
    pivot_strength: int = 2,
    max_zones: int = 3,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Generate supply / demand / pivot zones from D1 bars for one symbol.

    Parameters
    ----------
    bars : list of OHLC dicts (keys: time, open, high, low, close, tick_volume)
           Must be sorted oldest → newest.
    symbol : e.g. "EURUSD"
    atr_period : ATR lookback (default 14)
    atr_mult : zone width = ATR × atr_mult (default 0.8)
    pivot_strength : number of bars each side for pivot detection (default 2)
    max_zones : maximum total zones (default 3 → 1 supply + 1 pivot + 1 demand)

    Returns
    -------
    (zones, meta) where
      zones = list of zone dicts:
        {"zone_name": "...", "zone_type": "supply|demand|pivot",
         "low": float, "high": float, "source": "auto"}
      meta = dict with generation metadata
    """
    if not bars or len(bars) < atr_period + pivot_strength + 1:
        logger.warning(
            "[AUTO_ZONES] %s insufficient bars: %d (need >= %d)",
            symbol, len(bars), atr_period + pivot_strength + 1,
        )
        return [], {"error": "insufficient_bars", "bars_available": len(bars)}

    # 1. ATR
    atr = compute_atr(bars, period=atr_period)
    if atr <= 0:
        logger.warning("[AUTO_ZONES] %s ATR=0, cannot generate zones", symbol)
        return [], {"error": "zero_atr"}

    width = atr * atr_mult
    digits = 5 if "JPY" not in symbol else 3

    # 2. Pivots
    pivot_highs = find_pivot_highs(bars, strength=pivot_strength)
    pivot_lows = find_pivot_lows(bars, strength=pivot_strength)

    ranked_highs = _rank_pivots(pivot_highs, len(bars))
    ranked_lows = _rank_pivots(pivot_lows, len(bars))

    zones: List[Dict[str, Any]] = []

    # 3a. Supply zone (from best pivot high)
    supply_count = 0
    max_supply = max(1, max_zones // 3) if max_zones >= 3 else (1 if max_zones >= 1 else 0)
    for _, high_val, _ in ranked_highs:
        if supply_count >= max_supply:
            break
        supply_count += 1
        z_high = round(high_val, digits)
        z_low = round(high_val - width, digits)
        zones.append({
            "zone_name": f"Supply {supply_count}",
            "zone_type": "supply",
            "low": z_low,
            "high": z_high,
            "source": "auto",
        })

    # 3b. Demand zone (from best pivot low)
    demand_count = 0
    max_demand = max(1, max_zones // 3) if max_zones >= 3 else (1 if max_zones >= 1 else 0)
    for _, low_val, _ in ranked_lows:
        if demand_count >= max_demand:
            break
        demand_count += 1
        z_low = round(low_val, digits)
        z_high = round(low_val + width, digits)
        zones.append({
            "zone_name": f"Demand {demand_count}",
            "zone_type": "demand",
            "low": z_low,
            "high": z_high,
            "source": "auto",
        })

    # 3c. Pivot zone (median of last 60 closes or all if <60)
    remaining = max_zones - len(zones)
    if remaining > 0:
        tail = bars[-60:] if len(bars) >= 60 else bars
        closes = [float(b["close"]) for b in tail]
        med_close = statistics.median(closes)
        half_atr = atr * 0.5
        zones.append({
            "zone_name": "Pivot",
            "zone_type": "pivot",
            "low": round(med_close - half_atr, digits),
            "high": round(med_close + half_atr, digits),
            "source": "auto",
        })

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "auto",
        "timeframe": "D1",
        "bars": len(bars),
        "atr_period": atr_period,
        "atr_value": round(atr, digits),
        "atr_mult": atr_mult,
        "pivot_strength": pivot_strength,
        "max_zones": max_zones,
        "pivot_highs_found": len(pivot_highs),
        "pivot_lows_found": len(pivot_lows),
    }

    return zones, meta


def is_zones_stale(filters: dict, max_age_hours: int = 24) -> bool:
    """
    Check if auto_zones are stale (missing or older than max_age_hours).

    Reads filters.zones_meta.generated_at (ISO string).
    Returns True if stale or missing.
    """
    zones_meta = (filters or {}).get("zones_meta")
    if not zones_meta or zones_meta.get("source") != "auto":
        return True

    generated_at_str = zones_meta.get("generated_at")
    if not generated_at_str:
        return True

    try:
        generated_at = datetime.fromisoformat(
            generated_at_str.replace("Z", "+00:00")
        )
        age = datetime.now(timezone.utc) - generated_at
        return age.total_seconds() > max_age_hours * 3600
    except (ValueError, TypeError):
        return True


def resolve_zones(filters: dict) -> dict:
    """
    Resolve which zones to use for signal evaluation.

    Rules:
      - If filters.auto_zones_enabled == True AND filters.auto_zones exists
        → return filters.auto_zones  (dict of symbol → zone list)
      - Otherwise
        → return filters.zones        (manual/seeded zones)

    This keeps manual zones completely untouched when auto is off.
    """
    if not filters:
        return {}

    auto_enabled = filters.get("auto_zones_enabled") is True
    auto_zones = filters.get("auto_zones") or {}

    if auto_enabled and auto_zones:
        return auto_zones

    return filters.get("zones") or {}
