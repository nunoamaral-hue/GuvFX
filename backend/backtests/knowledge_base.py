"""
B14 — Research Knowledge Base Engine.

Persists research observations and computes long-term aggregated metrics.
Research Mode only — no live execution side effects.
No ML.  No automatic deployment.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from django.db.models import Avg, Count, Max, Min, StdDev, Q

logger = logging.getLogger(__name__)


# =========================================================================
# Observation capture — called from research API views
# =========================================================================


def record_observation(
    *,
    symbol: str,
    template: str,
    timeframe: str,
    parameters: dict | None = None,
    research_score: int = 0,
    robustness_label: str = "",
    profit_factor: float = 0.0,
    max_drawdown: float = 0.0,
    net_profit: float = 0.0,
    total_return_pct: float = 0.0,
    win_rate: float = 0.0,
    total_trades: int = 0,
    expectancy: float = 0.0,
    regime_at_observation: str = "",
    walk_forward_degradation: float | None = None,
    walk_forward_robust: bool | None = None,
    bar_count: int = 0,
    data_quality_status: str = "OK",
    feature_context: dict | None = None,
    source: str = "strategy_lab",
):
    """
    Record a single research observation into the Knowledge Base.

    This is a fire-and-forget write — errors are logged but never raised
    to avoid disrupting the calling research endpoint.
    """
    from backtests.models import ResearchObservation

    # B16 — derive flat feature fields from the snapshot for fast filtering
    fc = feature_context or {}
    snap = fc.get("snapshot", {}) if isinstance(fc, dict) else {}
    norm = fc.get("normalisation", {}) if isinstance(fc, dict) else {}

    try:
        obs = ResearchObservation.objects.create(
            symbol=symbol,
            template=template,
            timeframe=timeframe,
            parameters=parameters or {},
            research_score=research_score,
            robustness_label=robustness_label,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            net_profit=net_profit,
            total_return_pct=total_return_pct,
            win_rate=win_rate,
            total_trades=total_trades,
            expectancy=expectancy,
            regime_at_observation=regime_at_observation,
            walk_forward_degradation=walk_forward_degradation,
            walk_forward_robust=walk_forward_robust,
            bar_count=bar_count,
            data_quality_status=data_quality_status,
            feature_context=fc,
            trend_state=snap.get("trend_state", ""),
            volatility_state=snap.get("volatility_state", ""),
            session_bucket=snap.get("session_profile", "") or fc.get("session", {}).get("session_bucket", ""),
            breakout_state=snap.get("breakout_state", ""),
            position_size_warning=bool(norm.get("position_size_warning", snap.get("position_size_warning", False))),
            source=source,
        )
        return obs
    except Exception as e:
        logger.warning("Knowledge base: failed to record observation: %s", e)
        return None


def record_from_matrix_row(row: dict, *, source: str = "research_matrix"):
    """Record from a MatrixRow dict (as returned by matrix_to_dict)."""
    if row.get("error"):
        return None
    return record_observation(
        symbol=row.get("symbol", ""),
        template=row.get("template", ""),
        timeframe=row.get("timeframe", ""),
        research_score=int(row.get("research_score", 0)),
        robustness_label=row.get("robustness_label", ""),
        profit_factor=float(row.get("profit_factor", 0)),
        max_drawdown=float(row.get("max_drawdown", 0)),
        net_profit=float(row.get("net_profit", 0)),
        win_rate=float(row.get("win_rate", 0)),
        total_trades=int(row.get("trades", 0)),
        expectancy=float(row.get("expectancy", 0)),
        regime_at_observation=row.get("current_regime", ""),
        bar_count=int(row.get("bar_count", 0)),
        feature_context=row.get("feature_context"),
        source=source,
    )


def record_from_backtest_metrics(
    *,
    symbol: str,
    template: str,
    timeframe: str,
    metrics: dict,
    params: dict | None = None,
    regime: str = "",
    bar_count: int = 0,
    feature_context: dict | None = None,
    source: str = "strategy_lab",
):
    """Record from a backtest metrics dict (as returned by compute_metrics)."""
    from backtests.research_matrix import compute_research_score

    score, label = compute_research_score(metrics)
    return record_observation(
        symbol=symbol,
        template=template,
        timeframe=timeframe,
        parameters=params or {},
        research_score=score,
        robustness_label=label,
        profit_factor=float(metrics.get("profit_factor", 0)),
        max_drawdown=float(metrics.get("max_drawdown", 0)),
        net_profit=float(metrics.get("net_profit", 0)),
        total_return_pct=float(metrics.get("total_return_pct", 0)),
        win_rate=float(metrics.get("win_rate", 0)),
        total_trades=int(metrics.get("total_trades", 0)),
        expectancy=float(metrics.get("expectancy", 0)),
        regime_at_observation=regime,
        bar_count=bar_count,
        feature_context=feature_context if feature_context is not None else metrics.get("feature_context"),
        source=source,
    )


# =========================================================================
# Aggregation — compute long-term confidence from accumulated observations
# =========================================================================


@dataclass
class CombinationSummary:
    """Aggregated stats for one (symbol, template, timeframe) combination."""
    symbol: str
    template: str
    timeframe: str
    run_count: int = 0
    avg_score: float = 0.0
    avg_pf: float = 0.0
    avg_drawdown: float = 0.0
    avg_return: float = 0.0
    avg_win_rate: float = 0.0
    avg_trades: float = 0.0
    score_stddev: float = 0.0
    best_score: int = 0
    worst_score: int = 0
    last_score: int = 0
    last_observed: str = ""
    robustness_pct: float = 0.0  # % of observations that were STRONG or PROMISING
    confidence: str = "low"      # low / medium / high
    confidence_score: int = 0    # 0-100

    # Walk-forward stats (from observations that had WF data)
    wf_run_count: int = 0
    wf_robust_pct: float = 0.0
    avg_wf_degradation: float = 0.0

    # Regime breakdown
    regime_distribution: dict = field(default_factory=dict)

    # B16 — feature-context aggregation
    dominant_trend_state: str = ""
    dominant_volatility_state: str = ""
    position_size_warning_rate: float = 0.0   # % of observations flagged
    latest_feature_snapshot: dict = field(default_factory=dict)


@dataclass
class KnowledgeBaseResult:
    """Full knowledge base query result."""
    strongest: list  # CombinationSummary dicts
    weakest: list
    most_tested: list
    highest_confidence: list
    total_observations: int = 0
    total_combinations: int = 0
    warnings: list = field(default_factory=list)
    mode: str = "research"
    mode_disclaimer: str = (
        "Knowledge Base contains historical research observations only. "
        "Past simulated performance does not predict future results. "
        "Not financial advice."
    )


def _compute_confidence(summary: CombinationSummary) -> tuple[str, int]:
    """
    Compute confidence level and score (0-100) for a combination.

    Factors:
    - Run count (more observations = higher confidence)
    - Score consistency (low stddev = higher confidence)
    - Robustness rate (more STRONG/PROMISING = higher)
    - Walk-forward data (available + robust = higher)
    """
    score = 0

    # Run count contribution (0-30)
    if summary.run_count >= 20:
        score += 30
    elif summary.run_count >= 10:
        score += 25
    elif summary.run_count >= 5:
        score += 18
    elif summary.run_count >= 3:
        score += 10
    else:
        score += 3

    # Average research score contribution (0-25)
    if summary.avg_score >= 70:
        score += 25
    elif summary.avg_score >= 50:
        score += 18
    elif summary.avg_score >= 30:
        score += 10
    else:
        score += 3

    # Score consistency (low stddev = good) (0-20)
    if summary.run_count >= 3:
        if summary.score_stddev <= 8:
            score += 20
        elif summary.score_stddev <= 15:
            score += 14
        elif summary.score_stddev <= 25:
            score += 7
        else:
            score += 2

    # Robustness rate (0-15)
    if summary.robustness_pct >= 80:
        score += 15
    elif summary.robustness_pct >= 60:
        score += 10
    elif summary.robustness_pct >= 40:
        score += 5

    # Walk-forward bonus (0-10)
    if summary.wf_run_count >= 2:
        if summary.wf_robust_pct >= 80:
            score += 10
        elif summary.wf_robust_pct >= 50:
            score += 6
        else:
            score += 2

    # Clamp
    score = min(score, 100)

    if score >= 70:
        level = "high"
    elif score >= 40:
        level = "medium"
    else:
        level = "low"

    return level, score


def get_combination_summary(
    symbol: str, template: str, timeframe: str
) -> Optional[CombinationSummary]:
    """Get aggregated summary for a specific combination."""
    from backtests.models import ResearchObservation

    qs = ResearchObservation.objects.filter(
        symbol=symbol, template=template, timeframe=timeframe
    )

    if not qs.exists():
        return None

    aggs = qs.aggregate(
        run_count=Count("id"),
        avg_score=Avg("research_score"),
        avg_pf=Avg("profit_factor"),
        avg_drawdown=Avg("max_drawdown"),
        avg_return=Avg("total_return_pct"),
        avg_win_rate=Avg("win_rate"),
        avg_trades=Avg("total_trades"),
        score_stddev=StdDev("research_score"),
        best_score=Max("research_score"),
        worst_score=Min("research_score"),
    )

    # Last observation
    latest = qs.order_by("-observed_at").first()

    # Robustness rate: count STRONG + PROMISING out of total
    robust_count = qs.filter(
        Q(robustness_label="STRONG") | Q(robustness_label="PROMISING")
    ).count()
    robustness_pct = (robust_count / aggs["run_count"] * 100) if aggs["run_count"] else 0

    # Walk-forward stats
    wf_qs = qs.exclude(walk_forward_robust__isnull=True)
    wf_count = wf_qs.count()
    wf_robust_count = wf_qs.filter(walk_forward_robust=True).count()
    wf_robust_pct = (wf_robust_count / wf_count * 100) if wf_count else 0
    wf_aggs = wf_qs.aggregate(avg_deg=Avg("walk_forward_degradation"))

    # Regime distribution
    regime_counts = {}
    for regime_val in ["BULL", "BEAR", "SIDEWAYS"]:
        c = qs.filter(regime_at_observation=regime_val).count()
        if c > 0:
            regime_counts[regime_val] = c

    # B16 — feature-context aggregation
    def _dominant(field_name):
        counts = {}
        for v in qs.exclude(**{field_name: ""}).values_list(field_name, flat=True):
            counts[v] = counts.get(v, 0) + 1
        return max(counts, key=counts.get) if counts else ""

    dominant_trend = _dominant("trend_state")
    dominant_vol = _dominant("volatility_state")
    warn_count = qs.filter(position_size_warning=True).count()
    warn_rate = (warn_count / aggs["run_count"] * 100) if aggs["run_count"] else 0
    latest_snapshot = (latest.feature_context or {}).get("snapshot", {}) if latest else {}

    summary = CombinationSummary(
        symbol=symbol,
        template=template,
        timeframe=timeframe,
        run_count=aggs["run_count"],
        avg_score=round(aggs["avg_score"] or 0, 1),
        avg_pf=round(aggs["avg_pf"] or 0, 2),
        avg_drawdown=round(aggs["avg_drawdown"] or 0, 2),
        avg_return=round(aggs["avg_return"] or 0, 2),
        avg_win_rate=round(aggs["avg_win_rate"] or 0, 1),
        avg_trades=round(aggs["avg_trades"] or 0, 1),
        score_stddev=round(aggs["score_stddev"] or 0, 1),
        best_score=aggs["best_score"] or 0,
        worst_score=aggs["worst_score"] or 0,
        last_score=latest.research_score if latest else 0,
        last_observed=latest.observed_at.isoformat() if latest else "",
        robustness_pct=round(robustness_pct, 1),
        wf_run_count=wf_count,
        wf_robust_pct=round(wf_robust_pct, 1),
        avg_wf_degradation=round(wf_aggs["avg_deg"] or 0, 1),
        regime_distribution=regime_counts,
        dominant_trend_state=dominant_trend,
        dominant_volatility_state=dominant_vol,
        position_size_warning_rate=round(warn_rate, 1),
        latest_feature_snapshot=latest_snapshot,
    )

    level, conf_score = _compute_confidence(summary)
    summary.confidence = level
    summary.confidence_score = conf_score

    return summary


def query_knowledge_base(
    *,
    symbol: str | None = None,
    template: str | None = None,
    timeframe: str | None = None,
    min_runs: int = 1,
    top_n: int = 10,
) -> KnowledgeBaseResult:
    """
    Query the knowledge base for aggregated combination summaries.

    Returns strongest, weakest, most tested, and highest confidence combos.
    """
    from backtests.models import ResearchObservation

    # Base queryset with optional filters
    base_qs = ResearchObservation.objects.all()
    if symbol:
        base_qs = base_qs.filter(symbol=symbol)
    if template:
        base_qs = base_qs.filter(template=template)
    if timeframe:
        base_qs = base_qs.filter(timeframe=timeframe)

    total_observations = base_qs.count()

    if total_observations == 0:
        return KnowledgeBaseResult(
            strongest=[], weakest=[], most_tested=[],
            highest_confidence=[], total_observations=0,
            total_combinations=0,
            warnings=["No research observations found. Run Strategy Lab or Research Matrix to populate the Knowledge Base."],
        )

    # Get distinct combinations
    combos = (
        base_qs.values("symbol", "template", "timeframe")
        .annotate(run_count=Count("id"))
        .filter(run_count__gte=min_runs)
        .order_by("-run_count")
    )

    # Build summaries for all qualifying combos
    summaries: list[CombinationSummary] = []
    for combo in combos:
        s = get_combination_summary(
            combo["symbol"], combo["template"], combo["timeframe"]
        )
        if s:
            summaries.append(s)

    total_combinations = len(summaries)

    # Convert to dicts for serialization
    def to_dict(s: CombinationSummary) -> dict:
        return {
            "symbol": s.symbol,
            "template": s.template,
            "timeframe": s.timeframe,
            "run_count": s.run_count,
            "avg_score": s.avg_score,
            "avg_pf": s.avg_pf,
            "avg_drawdown": s.avg_drawdown,
            "avg_return": s.avg_return,
            "avg_win_rate": s.avg_win_rate,
            "avg_trades": s.avg_trades,
            "score_stddev": s.score_stddev,
            "best_score": s.best_score,
            "worst_score": s.worst_score,
            "last_score": s.last_score,
            "last_observed": s.last_observed,
            "robustness_pct": s.robustness_pct,
            "confidence": s.confidence,
            "confidence_score": s.confidence_score,
            "wf_run_count": s.wf_run_count,
            "wf_robust_pct": s.wf_robust_pct,
            "avg_wf_degradation": s.avg_wf_degradation,
            "regime_distribution": s.regime_distribution,
            "dominant_trend_state": s.dominant_trend_state,
            "dominant_volatility_state": s.dominant_volatility_state,
            "position_size_warning_rate": s.position_size_warning_rate,
            "latest_feature_snapshot": s.latest_feature_snapshot,
        }

    # Sort for different views
    strongest = sorted(summaries, key=lambda s: s.avg_score, reverse=True)[:top_n]
    weakest = sorted(summaries, key=lambda s: s.avg_score)[:top_n]
    most_tested = sorted(summaries, key=lambda s: s.run_count, reverse=True)[:top_n]
    highest_confidence = sorted(
        summaries, key=lambda s: s.confidence_score, reverse=True
    )[:top_n]

    warnings = []
    low_data = [s for s in summaries if s.run_count < 3]
    if low_data:
        warnings.append(
            f"{len(low_data)} combination(s) have fewer than 3 observations — "
            "confidence metrics may be unreliable."
        )

    return KnowledgeBaseResult(
        strongest=[to_dict(s) for s in strongest],
        weakest=[to_dict(s) for s in weakest],
        most_tested=[to_dict(s) for s in most_tested],
        highest_confidence=[to_dict(s) for s in highest_confidence],
        total_observations=total_observations,
        total_combinations=total_combinations,
        warnings=warnings,
    )


def result_to_dict(result: KnowledgeBaseResult) -> dict:
    """Serialize KnowledgeBaseResult for API response."""
    return {
        "strongest": result.strongest,
        "weakest": result.weakest,
        "most_tested": result.most_tested,
        "highest_confidence": result.highest_confidence,
        "total_observations": result.total_observations,
        "total_combinations": result.total_combinations,
        "warnings": result.warnings,
        "mode": result.mode,
        "mode_disclaimer": result.mode_disclaimer,
    }
