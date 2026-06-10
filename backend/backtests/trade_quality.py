"""
GuvFX Trade Quality Framework (B18)

Scores the DECISION quality of a research trade setup — independently of
whether the trade won or lost. A winning trade can be poor quality; a
losing trade can be high quality. This evaluates *setup quality*, never
profitability.

Deterministic, rule-based. NOT ML. NOT prediction. NOT execution.

Seven buckets (each 0-100), weighted into an overall 0-100 score:
  Market Selection 15% · Context 20% · Macro 10% · Entry 15% ·
  Risk 25% · Management 5% · Discipline 10%

Inputs are the B16/B16.5 feature_context + the strategy's intended params
(for risk/reward). Net profit / total return are deliberately NOT used.

WIMS governance: internal research scoring only. Public outputs must remain
research/rationale/education — never "AI trader" / "algorithm decided".
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WEIGHTS = {
    "market_selection": 0.15,
    "context": 0.20,
    "macro": 0.10,
    "entry": 0.15,
    "risk": 0.25,
    "management": 0.05,
    "discipline": 0.10,
}

_ASSET_LIQUIDITY = {
    "FX Major": 88, "FX Minor": 70, "Metal": 75,
    "Index": 72, "Crypto": 55, "Energy": 65, "Other": 60,
}

_NEUTRAL = 55  # used where data is genuinely unavailable (do not invent)


def _clamp(x: float) -> int:
    return int(max(0, min(100, round(x))))


def _label(score: int) -> str:
    if score >= 90:
        return "Elite"
    if score >= 80:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 60:
        return "Acceptable"
    return "Weak"


def _rr_from_params(params: dict | None) -> float | None:
    """Intended risk:reward from strategy params (decision property, not outcome)."""
    if not params:
        return None
    p = {k.lower(): v for k, v in params.items() if isinstance(v, (int, float))}
    tp = next((p[k] for k in p if "tp" in k), None)
    sl = next((p[k] for k in p if "sl" in k), None)
    if tp and sl and sl > 0:
        return round(tp / sl, 2)
    return None


# ─────────────────────────────────────────────────────────────────────
# Bucket scorers
# ─────────────────────────────────────────────────────────────────────

def _market_selection(symbol: str, fc: dict) -> tuple[int, str]:
    try:
        from backtests.research_matrix import ASSET_CLASS
    except Exception:
        ASSET_CLASS = {}
    asset = ASSET_CLASS.get(symbol, "Other")
    score = _ASSET_LIQUIDITY.get(asset, 60)
    vol = fc.get("volatility", {}).get("volatility_state", "")
    sess = fc.get("snapshot", {}).get("session_profile", "")
    if vol == "normal":
        score += 5
    elif vol == "high":
        score -= 10
    if sess in ("london_active", "london_ny_overlap"):
        score += 8
    elif sess == "ny_active":
        score += 5
    elif sess == "asian_session":
        score -= 5
    return _clamp(score), f"{asset} liquidity, {vol or 'unknown'} volatility, {sess or 'unknown'} session"


# Template → preferred context profile
def _context(template: str, fc: dict) -> tuple[int, str]:
    trend = fc.get("trend", {}).get("trend_state", "") or fc.get("snapshot", {}).get("trend_state", "")
    regime = ""  # regime not always in fc; use trend as primary
    breakout = fc.get("structure", {}).get("breakout_state", "") or fc.get("snapshot", {}).get("breakout_state", "")
    trending = trend in ("uptrend", "downtrend", "strong_uptrend", "strong_downtrend")
    strong = trend in ("strong_uptrend", "strong_downtrend")
    if template == "ema_trend":
        score = 90 if strong else 78 if trending else 40
        note = f"trend-following in {trend or 'unknown'}"
    elif template == "rsi_mean_reversion":
        score = 85 if trend == "neutral" else 50 if trending and not strong else 35
        note = f"mean-reversion in {trend or 'unknown'}"
    elif template == "atr_breakout":
        expanding = fc.get("volatility", {}).get("volatility_expansion", "") == "expanding"
        at_level = breakout != "mid_range" and breakout != ""
        score = 70 + (12 if expanding else 0) + (8 if at_level else 0)
        note = f"breakout, expansion={expanding}, level={breakout or 'none'}"
    elif template == "london_breakout":
        sess = fc.get("snapshot", {}).get("session_profile", "")
        in_london = sess in ("london_active", "london_ny_overlap")
        score = (80 if in_london else 50) + (8 if breakout not in ("mid_range", "") else 0)
        note = f"session={sess or 'unknown'}, level={breakout or 'none'}"
    else:
        score, note = _NEUTRAL, "unknown template"
    return _clamp(score), note


def _macro(fc: dict) -> tuple[int, str]:
    news = fc.get("news", {}) or {}
    impact = news.get("impact", "NONE")
    if impact == "NONE":
        return 85, "no relevant economic event nearby"
    rel = news.get("event_relevance", "NONE")
    upcoming = news.get("is_upcoming", False)
    mins = news.get("minutes_to_event", 0) if upcoming else news.get("minutes_since_event", 0)
    # base penalty by impact × relevance
    sev = {"HIGH": 50, "MEDIUM": 30, "LOW": 12}.get(impact, 0)
    rel_factor = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3, "NONE": 0.0}.get(rel, 0.0)
    penalty = sev * rel_factor
    # proximity: closer upcoming events penalise more; post-event eased
    if upcoming:
        if mins <= 30:
            penalty *= 1.4
        elif mins <= 120:
            penalty *= 1.0
        else:
            penalty *= 0.5
    else:
        penalty *= 0.5  # event already passed — residual volatility only
    score = 90 - penalty
    return _clamp(score), f"{impact} {news.get('event_type','event')} ({rel} relevance, {'in' if upcoming else 'since'} {mins}m)"


def _entry(fc: dict) -> tuple[int, str]:
    struct = fc.get("structure", {})
    breakout = struct.get("breakout_state", "") or fc.get("snapshot", {}).get("breakout_state", "")
    trend = fc.get("snapshot", {}).get("trend_state", "")
    score = 60
    confluence = 0
    if breakout not in ("mid_range", ""):
        score += 12
        confluence += 1
        # directional alignment: breakout toward trend
        if ("high" in breakout and "up" in trend) or ("low" in breakout and "down" in trend):
            score += 10
            confluence += 1
    if trend not in ("neutral", ""):
        confluence += 1
    return _clamp(score), f"breakout={breakout or 'none'}, confluence={confluence}"


def _risk(params: dict | None, fc: dict, perf: dict) -> tuple[int, str]:
    rr = _rr_from_params(params)
    if rr is not None:
        score = 90 if rr >= 2.0 else 75 if rr >= 1.5 else 60 if rr >= 1.0 else 40
        rr_note = f"RR≈{rr}"
    else:
        score = 60
        rr_note = "RR unknown (neutral)"
    norm = fc.get("normalisation", {}) or {}
    if norm.get("position_size_warning"):
        score -= 25
        rr_note += ", position-size warning"
    dd = perf.get("max_drawdown") or 0
    try:
        dd = float(dd)
    except (TypeError, ValueError):
        dd = 0
    if dd > 50:
        score -= 20
        rr_note += f", high DD {round(dd,1)}%"
    elif dd > 20:
        score -= 10
    return _clamp(score), rr_note


def _management(params: dict | None) -> tuple[int, str]:
    if not params:
        return _NEUTRAL, "no management metadata (neutral)"
    keys = {k.lower() for k in params.keys()}
    has_tp = any("tp" in k for k in keys)
    has_trail = any("trail" in k for k in keys)
    has_be = any(("be" == k or "breakeven" in k) for k in keys)
    score = 55 + (10 if has_tp else 0) + (15 if has_trail else 0) + (10 if has_be else 0)
    return _clamp(score), f"TP={has_tp}, trailing={has_trail}, BE={has_be}"


def _discipline(context_score: int, perf: dict) -> tuple[int, str]:
    # Did the setup respect the template's intended context + produce a valid signal?
    fired = (perf.get("total_trades") or 0) > 0
    base = context_score  # context alignment is the core of discipline here
    if fired:
        base = base * 0.6 + 80 * 0.4  # blend alignment with "a valid rule-based signal fired"
    else:
        base = base * 0.5
    return _clamp(base), f"context-aligned={context_score >= 70}, signal_fired={fired}"


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def score_trade_quality(
    symbol: str,
    template: str,
    timeframe: str,
    feature_context: dict | None,
    params: dict | None = None,
    perf: dict | None = None,
) -> dict:
    """Compute the 7-bucket trade-quality score for a research setup."""
    fc = feature_context or {}
    perf = perf or {}
    if not fc.get("available"):
        return {"available": False, "reason": "no feature context", "overall_score": 0, "overall_label": "Weak"}

    ms, ms_n = _market_selection(symbol, fc)
    ctx, ctx_n = _context(template, fc)
    mac, mac_n = _macro(fc)
    ent, ent_n = _entry(fc)
    rsk, rsk_n = _risk(params, fc, perf)
    mgmt, mgmt_n = _management(params)
    disc, disc_n = _discipline(ctx, perf)

    buckets = {
        "market_selection": ms, "context": ctx, "macro": mac, "entry": ent,
        "risk": rsk, "management": mgmt, "discipline": disc,
    }
    overall = _clamp(sum(buckets[k] * w for k, w in WEIGHTS.items()))
    label = _label(overall)

    return {
        "available": True,
        "overall_score": overall,
        "overall_label": label,
        "buckets": buckets,
        "weights": WEIGHTS,
        "notes": {
            "market_selection": ms_n, "context": ctx_n, "macro": mac_n,
            "entry": ent_n, "risk": rsk_n, "management": mgmt_n, "discipline": disc_n,
        },
        "what_this_means": _explain(overall, label, buckets),
        "mode": "research",
        "disclaimer": "Decision-quality of the setup, not profitability. Research only — not a trade signal or recommendation.",
    }


def _explain(overall: int, label: str, b: dict) -> list[str]:
    out = [f"Quality {overall} ({label}) — evaluates setup decision quality, not whether it profited."]
    def phr(name, score, hi, lo):
        return hi if score >= 70 else (lo if score < 55 else f"{name} acceptable.")
    out.append(phr("Context", b["context"], "Strong context alignment.", "Weak context alignment for this template."))
    out.append(phr("Macro", b["macro"], "Clean macro conditions.", "Elevated macro/event risk nearby."))
    out.append(phr("Risk", b["risk"], "Favourable risk profile.", "Risk profile is a concern (RR / position size / drawdown)."))
    out.append(phr("Market", b["market_selection"], "Suitable instrument/session.", "Instrument/session suitability is marginal."))
    return out
