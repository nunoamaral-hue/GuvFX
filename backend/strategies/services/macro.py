"""
Macro regime classifier v2.

Computes a deterministic regime label from EURUSD, GBPUSD, XAUUSD daily bars.

FX trend   – EMA-20 vs EMA-60 crossover on EUR and GBP closes.
Gold stress – ATR-14 / close vs rolling median(stress, 120) * 1.25.

Decision table (gold stress takes priority):
    gold_stress_high              → STRONG_RISK_OFF
    fx_score == +2 (both up)      → STRONG_RISK_ON
    fx_score ==  0 (mixed)        → MILD_RISK_OFF
    fx_score == -2 (both down)    → STRONG_RISK_OFF
    insufficient data             → UNKNOWN

Lazy-imports fetch_rates so the module can be unit-tested in isolation
(pass account=None → returns UNKNOWN without touching the network).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MACRO_PROVIDER_VERSION = "MACRO_PROVIDER_V2"

# ── Valid regime labels ──────────────────────────────────────────────
VALID_LABELS = frozenset({
    "STRONG_RISK_ON",
    "MILD_RISK_ON",
    "MILD_RISK_OFF",
    "STRONG_RISK_OFF",
    "UNKNOWN",
})

# ── Tuning constants ────────────────────────────────────────────────
_MACRO_SYMBOLS = ("EURUSD", "GBPUSD", "XAUUSD")
_D1_BAR_COUNT = 250          # bars to request (need ≥200)
_EMA_FAST = 20
_EMA_SLOW = 60
_ATR_PERIOD = 14
_STRESS_LOOKBACK = 120       # rolling window for median stress
_STRESS_MULTIPLIER = 1.25


# ── Helpers ──────────────────────────────────────────────────────────

def _ema(values: List[float], period: int) -> List[float]:
    """Exponential moving average.  NaN-padded before the first valid bar."""
    n = len(values)
    if n < period:
        return [float("nan")] * n

    out = [float("nan")] * n
    # SMA seed
    out[period - 1] = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1.0 - k)
    return out


def _atr(bars: List[Dict[str, Any]], period: int) -> List[float]:
    """Wilder's ATR.  NaN-padded before the first valid bar."""
    n = len(bars)
    if n < period:
        return [float("nan")] * n

    # True range
    tr = [0.0] * n
    tr[0] = bars[0]["high"] - bars[0]["low"]
    for i in range(1, n):
        h = bars[i]["high"]
        lo = bars[i]["low"]
        pc = bars[i - 1]["close"]
        tr[i] = max(h - lo, abs(h - pc), abs(lo - pc))

    # Wilder smoothing (RMA): SMA seed then (prev*(N-1) + tr) / N
    out = [float("nan")] * n
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _median(values: List[float]) -> float:
    """Median of non-NaN values.  Returns NaN if empty."""
    clean = sorted(v for v in values if not math.isnan(v))
    if not clean:
        return float("nan")
    mid = len(clean) // 2
    if len(clean) % 2 == 0:
        return (clean[mid - 1] + clean[mid]) / 2.0
    return clean[mid]


# ── Public API ───────────────────────────────────────────────────────

def get_macro_regime_label(
    now_ts: datetime,
    account: Any = None,
    assignment: Any = None,
) -> str:
    """
    Return the current macro regime label.

    Parameters
    ----------
    now_ts      : evaluation timestamp (reserved for future caching)
    account     : TradingAccount — required for fetch_rates
    assignment  : StrategyAssignment (unused)

    Returns
    -------
    One of VALID_LABELS.
    """
    if account is None:
        logger.warning("[MACRO] No account provided; returning UNKNOWN")
        return "UNKNOWN"

    # Lazy import keeps the module unit-testable without Django
    from strategies.signal_engine import fetch_rates, RatesFetchError

    # ── 1. Fetch D1 bars ─────────────────────────────────────────────
    bars: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    for sym in _MACRO_SYMBOLS:
        try:
            bars[sym] = fetch_rates(account, sym, "D1", count=_D1_BAR_COUNT)
        except RatesFetchError as exc:
            logger.warning("[MACRO] fetch_rates(%s, D1) failed: %s", sym, exc)
            bars[sym] = None

    eur_bars = bars.get("EURUSD")
    gbp_bars = bars.get("GBPUSD")
    xau_bars = bars.get("XAUUSD")

    if (
        not eur_bars or len(eur_bars) < _EMA_SLOW
        or not gbp_bars or len(gbp_bars) < _EMA_SLOW
        or not xau_bars or len(xau_bars) < _ATR_PERIOD
    ):
        logger.info("[MACRO] Insufficient D1 data; returning UNKNOWN")
        return "UNKNOWN"

    # ── 2. FX trend (EUR + GBP) ──────────────────────────────────────
    eur_close = [b["close"] for b in eur_bars]
    gbp_close = [b["close"] for b in gbp_bars]

    eur_fast = _ema(eur_close, _EMA_FAST)[-1]
    eur_slow = _ema(eur_close, _EMA_SLOW)[-1]
    gbp_fast = _ema(gbp_close, _EMA_FAST)[-1]
    gbp_slow = _ema(gbp_close, _EMA_SLOW)[-1]

    if any(math.isnan(v) for v in (eur_fast, eur_slow, gbp_fast, gbp_slow)):
        logger.info("[MACRO] EMA values contain NaN; returning UNKNOWN")
        return "UNKNOWN"

    eur_trend_up = eur_fast > eur_slow
    gbp_trend_up = gbp_fast > gbp_slow
    fx_score = (1 if eur_trend_up else -1) + (1 if gbp_trend_up else -1)

    # ── 3. Gold stress ───────────────────────────────────────────────
    xau_atr = _atr(xau_bars, _ATR_PERIOD)
    xau_close = [b["close"] for b in xau_bars]

    stress = []
    for a, c in zip(xau_atr, xau_close):
        if math.isnan(a) or c <= 0:
            stress.append(float("nan"))
        else:
            stress.append(a / c)

    stress_last = stress[-1]
    if math.isnan(stress_last):
        logger.info("[MACRO] Gold stress is NaN; returning UNKNOWN")
        return "UNKNOWN"

    stress_median = _median(stress[-_STRESS_LOOKBACK:])
    if math.isnan(stress_median):
        logger.info("[MACRO] Stress median is NaN; returning UNKNOWN")
        return "UNKNOWN"

    stress_threshold = stress_median * _STRESS_MULTIPLIER
    gold_stress_high = stress_last > stress_threshold

    # ── 4. Decision table ────────────────────────────────────────────
    if gold_stress_high:
        label = "STRONG_RISK_OFF"
    elif fx_score == 2:
        label = "STRONG_RISK_ON"
    elif fx_score == 0:
        label = "MILD_RISK_OFF"
    else:  # fx_score == -2
        label = "STRONG_RISK_OFF"

    logger.info(
        "[MACRO] regime=%s  fx_score=%+d  eur_up=%s  gbp_up=%s  "
        "gold_stressed=%s  (stress=%.6f  threshold=%.6f)",
        label, fx_score, eur_trend_up, gbp_trend_up,
        gold_stress_high, stress_last, stress_threshold,
    )
    return label
