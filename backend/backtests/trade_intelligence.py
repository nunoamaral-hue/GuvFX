"""
GuvFX Trade Intelligence Record (B19)

Deterministically converts a research observation (+ Knowledge Base evidence,
feature context, and trade-quality score) into a STRUCTURED EXPLANATION object
that answers "Why was this setup interesting?" and "What were the main risks?".

Generator-only — NO persistence, NO LLM, NO external calls, NO prediction,
NO execution. Pure deterministic templates over existing data.

This is the bridge toward B20 Narrative Layer / WIMS content adapters, but it
does NOT generate social posts. Public-facing text is governed: it must never
expose the machinery ("AI trader", "algorithm decided", "machine prediction").
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── Public content governance ──
BLOCKED_PATTERNS = [
    (r"\bai trader\b", "strategy criteria"),
    (r"\bautonomous trader\b", "systematic strategy"),
    (r"\brobot trader\b", "systematic strategy"),
    (r"\balgorithm decided\b", "the strategy criteria were met"),
    (r"\balgorithm chose\b", "the strategy criteria selected"),
    (r"\bmachine prediction\b", "research context"),
    (r"\bmodel predicted\b", "research observations indicate"),
    (r"\bai predicted\b", "research observations indicate"),
    (r"\bguarantee[ds]?\b", "historically observed"),
    (r"\bwill profit\b", "has historically been observed"),
]

_TEMPLATE_NAMES = {
    "ema_trend": "EMA Trend",
    "rsi_mean_reversion": "RSI Mean Reversion",
    "atr_breakout": "ATR Breakout",
    "london_breakout": "London Breakout",
}
_TEMPLATE_SETUP = {
    "ema_trend": "trend-continuation",
    "rsi_mean_reversion": "mean-reversion",
    "atr_breakout": "volatility-breakout",
    "london_breakout": "session-breakout",
}


def _tname(t: str) -> str:
    return _TEMPLATE_NAMES.get(t, t)


def _public_language_pass(record: dict) -> tuple[bool, list[str]]:
    """Scan all string fields for blocked phrases; sanitise + flag. Returns (clean, warnings)."""
    warnings: list[str] = []

    def scrub(text: str) -> str:
        out = text
        for pat, repl in BLOCKED_PATTERNS:
            if re.search(pat, out, flags=re.IGNORECASE):
                warnings.append(f"Removed non-public phrase matching '{pat}'.")
                out = re.sub(pat, repl, out, flags=re.IGNORECASE)
        return out

    def walk(obj):
        if isinstance(obj, str):
            return scrub(obj)
        if isinstance(obj, list):
            return [walk(x) for x in obj]
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        return obj

    cleaned = walk(record)
    return (len(warnings) == 0), warnings, cleaned


# ── Evidence aggregation ──

def _evidence(symbol: str, template: str, timeframe: str) -> dict:
    from backtests.models import ResearchObservation
    from backtests.attribution import _is_strong, _is_weak

    qs = ResearchObservation.objects.filter(symbol=symbol, template=template, timeframe=timeframe)
    rows = list(qs.values("research_score", "quality_score", "profit_factor", "max_drawdown", "robustness_label"))
    n = len(rows)
    if n == 0:
        return {"similar_observation_count": 0}

    def avg(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    strong = sum(1 for r in rows if _is_strong(r))
    weak = sum(1 for r in rows if _is_weak(r))
    return {
        "similar_observation_count": n,
        "avg_research_score": avg("research_score"),
        "avg_quality_score": avg("quality_score"),
        "avg_profit_factor": avg("profit_factor"),
        "avg_drawdown": avg("max_drawdown"),
        "strong_rate": round(100.0 * strong / n, 1),
        "weak_rate": round(100.0 * weak / n, 1),
    }


def _direction(template: str, trend_state: str) -> str:
    if template in ("ema_trend", "atr_breakout", "london_breakout"):
        if "up" in trend_state:
            return "long-bias"
        if "down" in trend_state:
            return "short-bias"
        return "context-dependent"
    return "context-dependent (mean-reversion)"


# ── Main generator ──

def generate_record(
    *,
    observation_id: int | None = None,
    symbol: str | None = None,
    template: str | None = None,
    timeframe: str | None = None,
) -> dict:
    from backtests.models import ResearchObservation

    warnings: list[str] = []
    obs = None

    if observation_id is not None:
        obs = ResearchObservation.objects.filter(id=observation_id).first()
        if not obs:
            return {"ok": True, "record": None, "warnings": [f"Observation {observation_id} not found."]}
        symbol, template, timeframe = obs.symbol, obs.template, obs.timeframe
    elif symbol and template:
        timeframe = timeframe or "H1"
        obs = (ResearchObservation.objects
               .filter(symbol=symbol, template=template, timeframe=timeframe)
               .order_by("-observed_at").first())
        if not obs:
            warnings.append("insufficient historical evidence")
    else:
        return {"ok": True, "record": None, "warnings": ["Provide observation_id or symbol+template."]}

    fc = (obs.feature_context if obs else {}) or {}
    snap = fc.get("snapshot", {})
    news = fc.get("news", {})
    tq = fc.get("trade_quality", {})

    # Evidence
    ev = _evidence(symbol, template, timeframe) if (symbol and template and timeframe) else {"similar_observation_count": 0}
    if ev.get("similar_observation_count", 0) < 3:
        warnings.append("insufficient historical evidence (small sample) — interpret cautiously")

    trend = (obs.trend_state if obs else "") or snap.get("trend_state", "")
    vol = (obs.volatility_state if obs else "") or snap.get("volatility_state", "")
    session = (obs.session_bucket if obs else "") or snap.get("session_profile", "")
    breakout = (obs.breakout_state if obs else "") or snap.get("breakout_state", "")
    regime = (obs.regime_at_observation if obs else "")
    buckets = (obs.quality_buckets if obs else {}) or tq.get("buckets", {})
    qscore = obs.quality_score if obs else tq.get("overall_score")
    qlabel = obs.quality_label if obs else tq.get("overall_label", "")

    # Confidence from KB combination summary
    confidence = None
    try:
        from backtests.knowledge_base import get_combination_summary
        s = get_combination_summary(symbol, template, timeframe)
        if s:
            confidence = s.confidence
    except Exception:
        pass

    # ── Sections ──
    identity = {
        "symbol": symbol, "direction": _direction(template, trend),
        "template": template, "template_name": _tname(template),
        "timeframe": timeframe, "setup_type": _TEMPLATE_SETUP.get(template, "research setup"),
        "source_observation_id": obs.id if obs else None,
    }
    market_context = {
        "regime": regime or "unknown", "trend_state": trend or "unknown",
        "volatility_state": vol or "unknown", "session_bucket": session or "unknown",
        "breakout_state": breakout or "unknown",
        "news_context": {
            "impact": news.get("impact", "NONE"), "event_type": news.get("event_type"),
            "event_relevance": news.get("event_relevance", "NONE"),
            "minutes_to_event": news.get("minutes_to_event"),
        },
    }
    quality = {
        "quality_score": qscore, "quality_label": qlabel,
        "quality_buckets": buckets, "confidence_level": confidence,
    }

    # Thesis
    thesis = (
        f"{_tname(template)} on {symbol} ({timeframe}) looks for {_TEMPLATE_SETUP.get(template,'research')} "
        f"entries. Current market conditions show a {trend or 'neutral'} trend with {vol or 'unspecified'} "
        f"volatility during the {session or 'unspecified'} session"
        + (f", with the price {breakout.replace('_',' ')}" if breakout and breakout != "mid_range" else "")
        + ". This setup is recorded because the strategy's criteria align with these conditions."
    )

    # Supporting factors (from buckets + context)
    supporting: list[str] = []
    if buckets.get("context", 0) >= 70:
        supporting.append(f"Market context aligns with the {_tname(template)} strategy's design ({trend or 'context'}).")
    if buckets.get("risk", 0) >= 70:
        supporting.append("Risk profile is favourable (reward-to-risk and position sizing).")
    if buckets.get("macro", 0) >= 75:
        supporting.append("Clean macro window — no major economic event interfering.")
    if buckets.get("market_selection", 0) >= 80:
        supporting.append("Liquid instrument traded in a suitable session.")
    if ev.get("strong_rate") is not None and ev["strong_rate"] >= 30:
        supporting.append(f"Historically, {ev['strong_rate']}% of similar observations were strong (n={ev['similar_observation_count']}).")
    if not supporting:
        supporting.append("No standout supporting factors in this sample.")

    # Risk factors
    risk: list[str] = []
    norm = fc.get("normalisation", {})
    if norm.get("position_size_warning") or (obs and obs.position_size_warning):
        risk.append("High-notional / high-tick-value instrument — research drawdown may be distorted; position-size normalization advised.")
    if vol == "high":
        risk.append("Elevated volatility can reduce the reliability of this setup.")
    if buckets.get("context", 100) < 55:
        risk.append(f"Market context is not well aligned with a {_TEMPLATE_SETUP.get(template,'')} strategy ({trend or 'unclear trend'}).")
    if news.get("impact") in ("HIGH", "MEDIUM") and news.get("event_relevance") in ("HIGH", "MEDIUM"):
        mins = news.get("minutes_to_event")
        risk.append(f"{news.get('impact')}-impact {news.get('event_type','event')} nearby ({mins} min) — elevated event risk.")
    if ev.get("weak_rate") is not None and ev["weak_rate"] >= 40:
        risk.append(f"Historically, {ev['weak_rate']}% of similar observations were weak — caution warranted.")
    if not risk:
        risk.append("No major risk flags in this sample, but historical evidence is limited.")

    # Decision notes
    decision: list[str] = []
    low_buckets = sorted([(k, v) for k, v in buckets.items()], key=lambda x: x[1])[:2] if buckets else []
    improve_map = {
        "context": "clearer alignment between trend/regime and the strategy",
        "macro": "more distance from high-impact economic events",
        "risk": "improved reward-to-risk or reduced position-size exposure",
        "entry": "entry nearer a clean structural level with confluence",
        "market_selection": "a more liquid instrument or a more active session",
        "discipline": "stricter adherence to the strategy's context requirements",
        "management": "defined take-profit / break-even / trailing management",
    }
    if low_buckets:
        decision.append("Quality would improve with: " + "; ".join(improve_map.get(k, k) for k, _ in low_buckets) + ".")
    if ev.get("similar_observation_count", 0) < 5:
        decision.append(f"Confidence is limited by a small historical sample ({ev.get('similar_observation_count', 0)} observations).")
    if confidence and confidence != "high":
        decision.append(f"Knowledge-base confidence for this combination is currently '{confidence}'.")

    # Audience-safe summary
    summary = (
        f"On {symbol} ({timeframe}), the {_tname(template)} strategy's criteria are met within a "
        f"{trend or 'neutral'} trend and {vol or 'unspecified'} volatility. "
        + (f"Across {ev['similar_observation_count']} similar research observations, the average research score is "
           f"{ev.get('avg_research_score')} and average setup quality is {ev.get('avg_quality_score')}. "
           if ev.get("similar_observation_count", 0) >= 3 else "Historical evidence for this exact setup is limited. ")
        + (f"The main risk factor noted is: {risk[0]} " if risk else "")
        + "This is research context based on historical observations and strategy criteria — not a prediction or a recommendation to trade."
    )

    record = {
        "identity": identity,
        "market_context": market_context,
        "quality": quality,
        "historical_evidence": ev,
        "trade_thesis": thesis,
        "supporting_factors": supporting,
        "risk_factors": risk,
        "decision_notes": decision,
        "audience_safe_summary": summary,
        "content_safety_mode": "public_safe",
    }

    clean, lang_warnings, record = _public_language_pass(record)
    record["public_language_pass"] = clean
    warnings.extend(lang_warnings)

    return {"ok": True, "record": record, "warnings": warnings}
