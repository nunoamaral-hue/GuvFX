"""
GuvFX Research Feature Framework (B16)

Extracts NORMALISED MARKET CONTEXT from OHLC bars so every research
observation records the *conditions* under which a strategy performed —
not just the performance result.

Controlled feature groups only (no random indicators):
  1. Trend           — ema_slope, ema_distance, trend_strength, trend_state
  2. Volatility      — atr_value, atr_percentile, volatility_state, volatility_expansion
  3. Session         — session_bucket, asian_range, london_range, ny_range
  4. Market structure— distance_to_{20,50}_{high,low}, breakout_state
  5. Normalisation   — tick_size, tick_value, contract_size, notional_per_001_lot,
                       pnl_model, position_size_warning

Research Mode ONLY. This module is pure analysis of historical bars —
it records context, it does NOT change trade logic, place orders, or
build prediction models. No ML.

Self-contained indicator math (no imports from strategy_templates/engine)
to avoid import cycles. Symbol metadata is imported lazily.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Threshold constants (documented, controlled)
_TREND_STRONG = 1.5      # |ema_distance| in ATR units → strong trend
_TREND_MILD = 0.3        # |ema_distance| in ATR units → mild trend
_VOL_HIGH_PCT = 70       # atr percentile → high volatility
_VOL_LOW_PCT = 30        # atr percentile → low volatility
_VOL_EXPAND = 1.15       # recent/overall ATR ratio → expanding
_VOL_CONTRACT = 0.85     # recent/overall ATR ratio → contracting
_BREAKOUT_NEAR_ATR = 0.30  # within this many ATR of an extreme → "near"

# Position-size warning thresholds
_WARN_TICK_VALUE = 5.0       # $/tick/lot at/above this is "large" vs FX (=1.0)
_WARN_NOTIONAL = 5000.0      # notional per 0.01 lot above this is large
_WARN_ATR_DOLLARS = 20.0     # $ risk of one ATR move per 0.01 lot above this is large
_WARN_DRAWDOWN_PCT = 50.0    # backtest max drawdown above this → normalise before comparison

WARNING_TEXT = (
    "Research result may require position-size normalization before comparison."
)


# ─────────────────────────────────────────────────────────────────────
# Self-contained indicator helpers (operate on plain float lists / bars)
# ─────────────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> list[float | None]:
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    out: list[float | None] = [None] * len(values)
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _atr(bars, period: int = 14) -> list[float | None]:
    n = len(bars)
    if n < 2:
        return [None] * n
    trs = [0.0]
    for i in range(1, n):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    seed = sum(trs[1:period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def _last(series: list[float | None]) -> float | None:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _percentile_rank(series: list[float | None], value: float) -> float:
    vals = [v for v in series if v is not None]
    if not vals or value is None:
        return 0.0
    below = sum(1 for v in vals if v <= value)
    return round(100.0 * below / len(vals), 1)


# ─────────────────────────────────────────────────────────────────────
# Normalisation: contract sizes (best-effort, MT5-typical)
# ─────────────────────────────────────────────────────────────────────

def _contract_size(symbol: str, asset_class: str) -> tuple[float, str]:
    """Return (contract_size, source). Best-effort; broker-dependent."""
    special = {
        "XAUUSD": 100.0,     # troy oz
        "XAGUSD": 5000.0,    # troy oz
    }
    if symbol in special:
        return special[symbol], "known"
    by_class = {
        "FX Major": 100000.0, "FX Minor": 100000.0,
        "Metal": 100.0,
        "Index": 1.0,
        "Crypto": 1.0,
        "Energy": 1000.0,
    }
    if asset_class in by_class:
        return by_class[asset_class], "by_asset_class"
    return 100000.0, "estimate"


def _session_of_hour(h: int) -> str:
    # UTC session buckets
    if h >= 22 or h < 7:
        return "asian"
    if 7 <= h < 12:
        return "london"
    if 12 <= h < 16:
        return "london_ny_overlap"
    return "ny"  # 16-22


_SESSION_PROFILE = {
    "asian": "asian_session",
    "london": "london_active",
    "london_ny_overlap": "london_ny_overlap",
    "ny": "ny_active",
}


# ─────────────────────────────────────────────────────────────────────
# Main extractor
# ─────────────────────────────────────────────────────────────────────

def extract_feature_context(bars, symbol: str = "", timeframe: str = "") -> dict:
    """
    Build the normalised market-context feature dict for a set of OHLC bars.

    Returns a dict with groups: trend / volatility / session / structure /
    normalisation, plus a compact ``snapshot`` (regime-compatible) and a
    structural ``position_size_warning``. Drawdown-based warnings are folded
    in later via :func:`apply_risk_warnings`.
    """
    # Lazy import to avoid cycles (research_matrix imports engine)
    try:
        from backtests.research_matrix import SYMBOL_METADATA, ASSET_CLASS, get_pip_config
    except Exception:  # pragma: no cover
        SYMBOL_METADATA, ASSET_CLASS = {}, {}
        get_pip_config = None

    n = len(bars)
    asset_class = (ASSET_CLASS or {}).get(symbol, "Other")

    if n < 55:
        return {
            "available": False,
            "reason": f"insufficient bars ({n}) for feature extraction (need >=55)",
            "snapshot": {
                "trend_state": "unknown", "volatility_state": "unknown",
                "session_profile": "unknown", "breakout_state": "unknown",
                "position_size_warning": False,
            },
            "mode": "research",
        }

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    price = closes[-1]

    # ── Volatility ──
    atr_series = _atr(bars, 14)
    atr_value = _last(atr_series) or 0.0
    atr_pct = _percentile_rank(atr_series, atr_value)
    recent = [v for v in atr_series[-5:] if v is not None]
    overall = [v for v in atr_series if v is not None]
    ratio = (sum(recent) / len(recent)) / (sum(overall) / len(overall)) if recent and overall else 1.0
    if ratio >= _VOL_EXPAND:
        vol_expansion = "expanding"
    elif ratio <= _VOL_CONTRACT:
        vol_expansion = "contracting"
    else:
        vol_expansion = "stable"
    if atr_pct >= _VOL_HIGH_PCT:
        vol_state = "high"
    elif atr_pct <= _VOL_LOW_PCT:
        vol_state = "low"
    else:
        vol_state = "normal"

    safe_atr = atr_value if atr_value > 0 else (price * 1e-4 or 1e-6)

    # ── Trend ──
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    e20, e50 = _last(ema20), _last(ema50)
    ema_distance = (e20 - e50) if (e20 is not None and e50 is not None) else 0.0
    ema_distance_atr = round(ema_distance / safe_atr, 3)
    # slope of ema20 over last 5 bars, in ATR units per bar
    slope_raw = 0.0
    vals20 = [v for v in ema20 if v is not None]
    if len(vals20) >= 6:
        slope_raw = (vals20[-1] - vals20[-6]) / 5.0
    ema_slope = round(slope_raw / safe_atr, 4)
    trend_strength = round(abs(ema_distance_atr), 3)
    sep = ema_distance_atr
    if sep > _TREND_STRONG:
        trend_state = "strong_uptrend"
    elif sep > _TREND_MILD:
        trend_state = "uptrend"
    elif sep < -_TREND_STRONG:
        trend_state = "strong_downtrend"
    elif sep < -_TREND_MILD:
        trend_state = "downtrend"
    else:
        trend_state = "neutral"

    # ── Market structure ──
    high20, low20 = max(highs[-20:]), min(lows[-20:])
    high50, low50 = max(highs[-50:]), min(lows[-50:])
    d20h = round((high20 - price) / safe_atr, 3)
    d20l = round((price - low20) / safe_atr, 3)
    d50h = round((high50 - price) / safe_atr, 3)
    d50l = round((price - low50) / safe_atr, 3)
    # nearest extreme within threshold
    cands = [
        ("near_50_high", d50h), ("near_50_low", d50l),
        ("near_20_high", d20h), ("near_20_low", d20l),
    ]
    near = [(label, d) for label, d in cands if 0 <= d <= _BREAKOUT_NEAR_ATR]
    breakout_state = min(near, key=lambda x: x[1])[0] if near else "mid_range"

    # ── Session ──
    def _hour(b):
        return datetime.fromtimestamp(b.time, tz=timezone.utc).hour

    sess_ranges: dict[str, list[float]] = {"asian": [], "london": [], "ny": []}
    for b in bars:
        h = _hour(b)
        rng = b.high - b.low
        if h >= 22 or h < 7:
            sess_ranges["asian"].append(rng)
        if 7 <= h < 16:
            sess_ranges["london"].append(rng)
        if 12 <= h < 22:
            sess_ranges["ny"].append(rng)

    def _avg_pts(lst):
        if not lst:
            return 0.0
        return round((sum(lst) / len(lst)) / (safe_atr if safe_atr else 1), 3)

    session_bucket = _session_of_hour(_hour(bars[-1]))
    session = {
        "session_bucket": session_bucket,
        "asian_range": _avg_pts(sess_ranges["asian"]),   # avg bar range in ATR units
        "london_range": _avg_pts(sess_ranges["london"]),
        "ny_range": _avg_pts(sess_ranges["ny"]),
        "range_unit": "atr_multiples",
    }

    # ── Normalisation ──
    meta = (SYMBOL_METADATA or {}).get(symbol)
    if meta:
        tick_size, tick_value = meta["tick_size"], meta["tick_value"]
        meta_source = "mt5_metadata"
    elif get_pip_config:
        pc = get_pip_config(symbol)
        tick_size, tick_value = pc["tick_size"], pc["tick_value"]
        meta_source = pc.get("source", "fallback")
    else:
        tick_size, tick_value, meta_source = 0.00001, 1.0, "default"
    contract_size, cs_source = _contract_size(symbol, asset_class)
    notional_per_001 = round(price * contract_size * 0.01, 2)
    atr_dollars_per_001 = round((atr_value / tick_size) * tick_value * 0.01, 2) if tick_size else 0.0

    struct_reasons = []
    if tick_value >= _WARN_TICK_VALUE:
        struct_reasons.append(f"large tick_value (${tick_value}/tick/lot vs $1 FX)")
    if notional_per_001 > _WARN_NOTIONAL:
        struct_reasons.append(f"high notional (~${notional_per_001} per 0.01 lot)")
    if atr_dollars_per_001 > _WARN_ATR_DOLLARS:
        struct_reasons.append(f"large $ risk per ATR (~${atr_dollars_per_001} on 0.01 lot)")
    structural_warning = len(struct_reasons) > 0

    normalisation = {
        "tick_size": tick_size,
        "tick_value": tick_value,
        "contract_size": contract_size,
        "contract_size_source": cs_source,
        "notional_per_001_lot": notional_per_001,
        "atr_dollars_per_001_lot": atr_dollars_per_001,
        "pnl_model": "pnl = (price_delta / tick_size) * tick_value * lots",
        "metadata_source": meta_source,
        "position_size_warning": structural_warning,
        "position_size_warning_reasons": struct_reasons,
        "warning_text": WARNING_TEXT if structural_warning else "",
    }

    snapshot = {
        "trend_state": trend_state,
        "volatility_state": vol_state,
        "session_profile": _SESSION_PROFILE.get(session_bucket, session_bucket),
        "breakout_state": breakout_state,
        "position_size_warning": structural_warning,
    }

    return {
        "available": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "asset_class": asset_class,
        "trend": {
            "ema_slope": ema_slope,
            "ema_distance": ema_distance_atr,
            "trend_strength": trend_strength,
            "trend_state": trend_state,
        },
        "volatility": {
            "atr_value": round(atr_value, 6),
            "atr_percentile": atr_pct,
            "volatility_state": vol_state,
            "volatility_expansion": vol_expansion,
        },
        "session": session,
        "structure": {
            "distance_to_20_high": d20h,
            "distance_to_20_low": d20l,
            "distance_to_50_high": d50h,
            "distance_to_50_low": d50l,
            "breakout_state": breakout_state,
            "distance_unit": "atr_multiples",
        },
        "normalisation": normalisation,
        "snapshot": snapshot,
        "mode": "research",
    }


def apply_risk_warnings(feature_context: dict, metrics: dict) -> dict:
    """
    Fold backtest-derived risk signals (max drawdown) into the structural
    position-size warning. Mutates and returns the feature_context.
    """
    if not feature_context or not feature_context.get("available"):
        return feature_context
    norm = feature_context.setdefault("normalisation", {})
    reasons = list(norm.get("position_size_warning_reasons", []))
    try:
        dd = float(metrics.get("max_drawdown", 0) or 0)
    except (TypeError, ValueError):
        dd = 0.0
    if dd > _WARN_DRAWDOWN_PCT:
        reasons.append(f"max_drawdown {round(dd, 1)}% exceeds {int(_WARN_DRAWDOWN_PCT)}%")
    warning = len(reasons) > 0
    norm["position_size_warning"] = warning
    norm["position_size_warning_reasons"] = reasons
    norm["warning_text"] = WARNING_TEXT if warning else ""
    feature_context.setdefault("snapshot", {})["position_size_warning"] = warning
    return feature_context
