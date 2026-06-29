"""
GuvFX Market State Engine (B20.6)

Deterministically classifies the current market state for a symbol from the
existing regime engine + feature framework (trend / volatility / structure)
+ economic event context. NO ML — a priority-ordered rule tree.

States:
  TREND_EXPANSION, TREND_EXHAUSTION, RANGE_COMPRESSION, RANGE_EXPANSION,
  VOLATILITY_EXPANSION, VOLATILITY_CONTRACTION, RISK_ON, RISK_OFF, NEWS_SHOCK

Returns: current_state, confidence (low/medium/high), supporting_evidence.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

STATES = [
    "TREND_EXPANSION", "TREND_EXHAUSTION", "RANGE_COMPRESSION", "RANGE_EXPANSION",
    "VOLATILITY_EXPANSION", "VOLATILITY_CONTRACTION", "RISK_ON", "RISK_OFF", "NEWS_SHOCK",
]


def _conf(n_signals: int) -> str:
    return "high" if n_signals >= 3 else "medium" if n_signals == 2 else "low"


def classify_market_state(bars, symbol: str = "", timeframe: str = "") -> dict:
    """Classify the current market state. Deterministic priority tree."""
    try:
        from backtests.feature_extractor import extract_feature_context
        from backtests.regime_engine import classify_regimes, RegimeParams
        from backtests.research_matrix import ASSET_CLASS
    except Exception as e:  # pragma: no cover
        return {"current_state": "UNKNOWN", "confidence": "low", "supporting_evidence": [f"engine import failed: {e}"]}

    if not bars or len(bars) < 55:
        return {"current_state": "UNKNOWN", "confidence": "low",
                "supporting_evidence": [f"insufficient bars ({len(bars) if bars else 0})"]}

    fc = extract_feature_context(bars, symbol=symbol, timeframe=timeframe)
    trend = fc.get("trend", {})
    vol = fc.get("volatility", {})
    struct = fc.get("structure", {})
    news = fc.get("news", {})
    snap = fc.get("snapshot", {})

    trend_state = snap.get("trend_state", "neutral")
    vol_state = vol.get("volatility_state", "normal")
    vol_exp = vol.get("volatility_expansion", "stable")
    breakout = struct.get("breakout_state", "mid_range")
    trend_strength = trend.get("trend_strength", 0)

    try:
        regime = classify_regimes(bars, RegimeParams()).current_regime
    except Exception:
        regime = ""

    asset_class = (ASSET_CLASS or {}).get(symbol, "Other")
    is_trending = trend_state in ("uptrend", "downtrend", "strong_uptrend", "strong_downtrend")
    is_strong = trend_state in ("strong_uptrend", "strong_downtrend")
    near_extreme = breakout in ("near_50_high", "near_50_low", "near_20_high", "near_20_low")

    ev: list[str] = []
    state = None

    # 1. NEWS_SHOCK — imminent/just-passed high-impact relevant event
    mins_to = news.get("minutes_to_event", 0) if news.get("is_upcoming") else 0
    mins_since = news.get("minutes_since_event", 0) if not news.get("is_upcoming") else 0
    if news.get("impact") == "HIGH" and news.get("event_relevance") in ("HIGH", "MEDIUM") and (
        (news.get("is_upcoming") and mins_to <= 30) or (not news.get("is_upcoming") and mins_since <= 30)
    ):
        state = "NEWS_SHOCK"
        ev.append(f"HIGH-impact {news.get('event_type','event')} within 30 min ({'in ' + str(mins_to) if news.get('is_upcoming') else str(mins_since) + ' min ago'})")

    # 2. Volatility expansion / contraction
    if state is None and vol_exp == "expanding" and vol_state == "high":
        state = "VOLATILITY_EXPANSION"
        ev += ["ATR expanding", "volatility state high"]
    if state is None and vol_exp == "contracting" and vol_state == "low" and not is_trending:
        state = "VOLATILITY_CONTRACTION"
        ev += ["ATR contracting", "volatility state low", "no clear trend"]

    # 3. Trend states
    if state is None and is_trending:
        if (vol_exp in ("expanding", "stable") and vol_state in ("normal", "high")) and trend_strength >= 0.5:
            state = "TREND_EXPANSION"
            ev += [f"{trend_state} with trend strength {trend_strength}", f"volatility {vol_state}/{vol_exp}"]
        elif vol_exp == "contracting" or (near_extreme and vol_state != "high"):
            state = "TREND_EXHAUSTION"
            ev += [f"{trend_state} but volatility {vol_exp}", f"price {breakout.replace('_',' ')}" if near_extreme else "momentum fading"]
        else:
            state = "TREND_EXPANSION"
            ev += [f"{trend_state}", f"trend strength {trend_strength}"]

    # 4. Range states (neutral trend)
    if state is None and not is_trending:
        if vol_state == "high" or vol_exp == "expanding":
            state = "RANGE_EXPANSION"
            ev += ["neutral trend", f"volatility {vol_state}/{vol_exp}"]
        else:
            state = "RANGE_COMPRESSION"
            ev += ["neutral trend", f"volatility {vol_state}/{vol_exp}"]

    # 5. Risk tone overlay (secondary signal; only becomes primary if nothing else fired)
    risk_proxy = {"Index": "risk_on", "Crypto": "risk_on", "Metal": "safe_haven"}.get(asset_class)
    risk_tone = None
    if risk_proxy == "risk_on":
        risk_tone = "RISK_ON" if "up" in trend_state else ("RISK_OFF" if "down" in trend_state else None)
    elif risk_proxy == "safe_haven":
        risk_tone = "RISK_OFF" if "up" in trend_state else ("RISK_ON" if "down" in trend_state else None)
    if state is None and risk_tone:
        state = risk_tone
        ev.append(f"{asset_class} ({risk_proxy}) {trend_state}")

    if state is None:
        state = "RANGE_COMPRESSION"
        ev.append("default: balanced conditions")

    return {
        "current_state": state,
        "confidence": _conf(len(ev)),
        "supporting_evidence": ev,
        "context": {
            "symbol": symbol, "timeframe": timeframe, "asset_class": asset_class,
            "regime": regime, "trend_state": trend_state, "volatility_state": vol_state,
            "volatility_expansion": vol_exp, "breakout_state": breakout,
            "risk_tone": risk_tone, "news_impact": news.get("impact", "NONE"),
        },
        "mode": "research",
    }
