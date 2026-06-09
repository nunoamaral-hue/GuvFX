"""
GuvFX Feature Attribution Layer (B17)

Deterministic statistical attribution over the Research Knowledge Base.
Answers "which market contexts are associated with strong or weak research
outcomes?" by grouping ResearchObservation rows by categorical context
features and computing descriptive statistics per group.

This is NOT machine learning. NOT prediction. NOT live trading. NOT
auto-deployment. Pure descriptive grouping + cautious, rule-based insights.

Research Mode only.
"""
from __future__ import annotations

import logging
import statistics

logger = logging.getLogger(__name__)

# Strong / weak thresholds (per B17 spec)
STRONG_SCORE = 80
WEAK_SCORE = 50
STRONG_LABEL = "STRONG"
WEAK_LABEL = "WEAK"

# Categorical context features available for attribution
CATEGORICAL_FEATURES = [
    "regime_at_observation",
    "trend_state",
    "volatility_state",
    "session_bucket",
    "breakout_state",
    "news_impact",
    "event_relevance",
    "position_size_warning",
]

NEWS_FEATURES = ["news_impact", "event_relevance", "news_type", "news_currency"]

# Fields pulled from the DB
_VALUE_FIELDS = [
    "research_score", "robustness_label", "profit_factor", "max_drawdown",
    "net_profit", "total_return_pct", "win_rate", "total_trades", "expectancy",
    "symbol", "template", "timeframe",
    "regime_at_observation", "trend_state", "volatility_state",
    "session_bucket", "breakout_state", "position_size_warning",
    "news_impact", "news_type", "news_currency", "event_relevance", "minutes_to_event",
]

_UNKNOWN = "(unknown)"

# Friendly template names for insight text
_TEMPLATE_NAMES = {
    "ema_trend": "EMA Trend",
    "rsi_mean_reversion": "RSI Mean Reversion",
    "atr_breakout": "ATR Breakout",
    "london_breakout": "London Breakout",
}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _is_strong(row: dict) -> bool:
    return (row.get("research_score") or 0) >= STRONG_SCORE or row.get("robustness_label") == STRONG_LABEL


def _is_weak(row: dict) -> bool:
    return (row.get("research_score") or 0) < WEAK_SCORE or row.get("robustness_label") == WEAK_LABEL


def _norm_value(feature: str, val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if val is None or val == "":
        return _UNKNOWN
    return str(val)


def _avg(vals: list[float]) -> float:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def group_stats(rows: list[dict], min_count: int = 3) -> dict:
    """Descriptive statistics for a group of observations."""
    n = len(rows)
    scores = [r.get("research_score") or 0 for r in rows]
    strong_count = sum(1 for r in rows if _is_strong(r))
    weak_count = sum(1 for r in rows if _is_weak(r))
    return {
        "observation_count": n,
        "avg_score": round(sum(scores) / n, 1) if n else 0.0,
        "median_score": round(statistics.median(scores), 1) if n else 0.0,
        "avg_profit_factor": _avg([r.get("profit_factor") for r in rows]),
        "avg_max_drawdown": _avg([r.get("max_drawdown") for r in rows]),
        "avg_net_profit": _avg([r.get("net_profit") for r in rows]),
        "win_rate_avg": _avg([r.get("win_rate") for r in rows]),
        "strong_count": strong_count,
        "weak_count": weak_count,
        "strong_rate": round(100.0 * strong_count / n, 1) if n else 0.0,
        "weak_rate": round(100.0 * weak_count / n, 1) if n else 0.0,
        "insufficient_sample": n < min_count,
    }


def attribute_by_feature(rows: list[dict], feature: str, min_count: int = 3) -> dict:
    """Group rows by a categorical feature and compute per-group stats."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(_norm_value(feature, r.get(feature)), []).append(r)
    return {val: group_stats(grp, min_count) for val, grp in sorted(groups.items())}


# ─────────────────────────────────────────────────────────────────────
# Insight generation (deterministic, cautious language)
# ─────────────────────────────────────────────────────────────────────

def _confidence(n: int, min_count: int) -> str:
    if n >= 15:
        return "high"
    if n >= 8:
        return "medium"
    return "low"


def _tname(t: str) -> str:
    return _TEMPLATE_NAMES.get(t, t)


def _feature_label(feature: str) -> str:
    return feature.replace("_", " ").replace("regime at observation", "regime")


def generate_insights(rows: list[dict], template_attr: dict, min_count: int) -> list[dict]:
    """Emit cautious, evidence-backed insight objects. No predictive claims."""
    insights: list[dict] = []

    # Per-template, per-feature divergence from the template baseline
    for template, feats in template_attr.items():
        t_rows = [r for r in rows if r.get("template") == template]
        if len(t_rows) < min_count:
            continue
        base_avg = round(sum((r.get("research_score") or 0) for r in t_rows) / len(t_rows), 1)
        for feature, groups in feats.items():
            if feature == "position_size_warning":
                continue  # handled separately in normalisation attribution
            for val, st in groups.items():
                if val == _UNKNOWN or st["insufficient_sample"]:
                    continue
                diff = round(st["avg_score"] - base_avg, 1)
                # Stronger-in-context
                if diff >= 20:
                    insights.append({
                        "category": feature,
                        "template": template,
                        "title": f"{_tname(template)} appears stronger when {_feature_label(feature)} = {val} (in this sample).",
                        "evidence": {
                            "group_avg_score": st["avg_score"], "template_avg_score": base_avg,
                            "score_diff": diff, "strong_rate": st["strong_rate"],
                        },
                        "confidence": _confidence(st["observation_count"], min_count),
                        "sample_count": st["observation_count"],
                        "caution": "Associative only; not predictive. Requires more observations to confirm.",
                    })
                # Frequent underperformance
                if st["weak_rate"] >= 70:
                    insights.append({
                        "category": feature,
                        "template": template,
                        "title": f"{_tname(template)} frequently underperforms when {_feature_label(feature)} = {val} (in this sample).",
                        "evidence": {
                            "weak_rate": st["weak_rate"], "group_avg_score": st["avg_score"],
                            "template_avg_score": base_avg,
                        },
                        "confidence": _confidence(st["observation_count"], min_count),
                        "sample_count": st["observation_count"],
                        "caution": "Associative only; may indicate a context the strategy dislikes. Not a prediction.",
                    })
    # Sort: high confidence + larger sample first
    order = {"high": 0, "medium": 1, "low": 2}
    insights.sort(key=lambda i: (order.get(i["confidence"], 3), -i["sample_count"]))
    return insights


def _strong_weak_feature_tally(rows: list[dict]) -> dict:
    """Which context feature values appear most often in strong vs weak observations."""
    strong = [r for r in rows if _is_strong(r)]
    weak = [r for r in rows if _is_weak(r)]

    def tally(subset):
        out = {}
        for feature in CATEGORICAL_FEATURES:
            counts: dict[str, int] = {}
            for r in subset:
                v = _norm_value(feature, r.get(feature))
                if v == _UNKNOWN:
                    continue
                counts[f"{feature}={v}"] = counts.get(f"{feature}={v}", 0) + 1
            for k, c in counts.items():
                out[k] = out.get(k, 0) + c
        return dict(sorted(out.items(), key=lambda x: -x[1])[:10])

    return {
        "strong_observations": len(strong),
        "weak_observations": len(weak),
        "top_features_in_strong": tally(strong),
        "top_features_in_weak": tally(weak),
    }


# ─────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────

def run_attribution(
    *,
    template: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    min_count: int = 3,
    feature: str | None = None,
) -> dict:
    """Build the full attribution result over the (optionally filtered) KB."""
    from backtests.models import ResearchObservation

    qs = ResearchObservation.objects.all()
    if template:
        qs = qs.filter(template=template)
    if symbol:
        qs = qs.filter(symbol=symbol)
    if timeframe:
        qs = qs.filter(timeframe=timeframe)

    rows = list(qs.values(*_VALUE_FIELDS))
    total = len(rows)

    warnings: list[str] = []
    if total == 0:
        return {
            "summary": {"total_observations": 0},
            "global_attribution": {}, "template_attribution": {},
            "symbol_attribution": {}, "news_attribution": {},
            "normalisation_attribution": {}, "insights": [],
            "warnings": ["No observations match the filter."],
            "mode": "research",
            "mode_disclaimer": _DISCLAIMER,
        }

    # Context coverage (Phase 1 inventory)
    with_features = sum(1 for r in rows if (r.get("trend_state") or r.get("volatility_state")))
    with_news = sum(1 for r in rows if (r.get("news_impact") and r.get("news_impact") != "NONE"))
    missing_context = total - with_features
    if missing_context:
        warnings.append(
            f"{missing_context}/{total} observations have no B16 feature context "
            "(pre-B16 rows) — context-feature attribution is limited; regime/performance "
            "attribution is unaffected."
        )
    if with_news == 0:
        warnings.append("No observations carry a non-NONE economic event context yet — news attribution is informational only.")

    # Determine which features to attribute
    feats = [feature] if feature and feature in CATEGORICAL_FEATURES else CATEGORICAL_FEATURES

    global_attr = {f: attribute_by_feature(rows, f, min_count) for f in feats}

    # Template-specific
    template_attr: dict = {}
    for t in sorted(set(r["template"] for r in rows)):
        t_rows = [r for r in rows if r["template"] == t]
        template_attr[t] = {f: attribute_by_feature(t_rows, f, min_count) for f in feats}

    # Symbol-specific (templates ranked + context among strong runs)
    symbol_attr: dict = {}
    for s in sorted(set(r["symbol"] for r in rows)):
        s_rows = [r for r in rows if r["symbol"] == s]
        by_template = {}
        for t in sorted(set(r["template"] for r in s_rows)):
            tr = [r for r in s_rows if r["template"] == t]
            by_template[t] = group_stats(tr, min_count)
        strong_rows = [r for r in s_rows if _is_strong(r)]
        strong_ctx = {}
        for f in ["regime_at_observation", "trend_state", "volatility_state", "session_bucket"]:
            vals = {}
            for r in strong_rows:
                v = _norm_value(f, r.get(f))
                if v == _UNKNOWN:
                    continue
                vals[v] = vals.get(v, 0) + 1
            if vals:
                strong_ctx[f] = dict(sorted(vals.items(), key=lambda x: -x[1]))
        symbol_attr[s] = {
            "observation_count": len(s_rows),
            "by_template": by_template,
            "context_in_strong_runs": strong_ctx,
            "insufficient_sample": len(s_rows) < min_count,
        }

    # News attribution
    news_attr = {f: attribute_by_feature(rows, f, min_count) for f in NEWS_FEATURES}

    # Normalisation attribution
    norm_attr = attribute_by_feature(rows, "position_size_warning", min_count)
    norm_insight = _normalisation_insight(norm_attr, min_count)

    insights = generate_insights(rows, template_attr, min_count)
    if norm_insight:
        insights.insert(0, norm_insight)
    tally = _strong_weak_feature_tally(rows)

    return {
        "summary": {
            "total_observations": total,
            "with_feature_context": with_features,
            "with_news_context": with_news,
            "missing_context": missing_context,
            "strong_observations": tally["strong_observations"],
            "weak_observations": tally["weak_observations"],
            "min_count": min_count,
            "filters": {"template": template, "symbol": symbol, "timeframe": timeframe, "feature": feature},
        },
        "global_attribution": global_attr,
        "template_attribution": template_attr,
        "symbol_attribution": symbol_attr,
        "news_attribution": news_attr,
        "normalisation_attribution": norm_attr,
        "feature_tally": tally,
        "insights": insights,
        "warnings": warnings,
        "mode": "research",
        "mode_disclaimer": _DISCLAIMER,
    }


def _normalisation_insight(norm_attr: dict, min_count: int) -> dict | None:
    t = norm_attr.get("true")
    f = norm_attr.get("false")
    if not t or not f or t["insufficient_sample"] or f["insufficient_sample"]:
        return None
    dd_t, dd_f = t["avg_max_drawdown"], f["avg_max_drawdown"]
    materially_higher = dd_t >= dd_f * 2 or (dd_t - dd_f) >= 20
    if materially_higher:
        return {
            "category": "normalisation",
            "template": None,
            "title": "High-notional / high-tick-value instruments (position-size warning) show materially higher drawdown in this sample.",
            "evidence": {
                "avg_drawdown_warning_true": dd_t, "avg_drawdown_warning_false": dd_f,
                "avg_score_warning_true": t["avg_score"], "avg_score_warning_false": f["avg_score"],
                "weak_rate_warning_true": t["weak_rate"],
            },
            "confidence": _confidence(min(t["observation_count"], f["observation_count"]), min_count),
            "sample_count": t["observation_count"] + f["observation_count"],
            "caution": "Flag only — may indicate scaling distortion. Do NOT exclude these instruments automatically.",
        }
    return None


_DISCLAIMER = (
    "Statistical attribution over historical research observations only. "
    "Associations are descriptive, not predictive, and must not be used for "
    "automatic deployment or parameter changes. Past simulated performance "
    "does not predict future results."
)
