"""
GuvFX Adaptive Research Recommender

Deterministic rule-based engine that converts research outputs into
structured recommendations. No ML. No auto-deployment.

Research Mode only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Recommendation structure
# ─────────────────────────────────────────────────────────────────

@dataclass
class Recommendation:
    category: str  # strategy, symbol, regime, portfolio, investigation
    priority: str  # high, medium, low
    confidence: str  # high, medium, low
    title: str
    evidence: list[str] = field(default_factory=list)
    suggested_next_action: str = ""
    source_metrics: dict = field(default_factory=dict)


@dataclass
class RecommendationResult:
    recommendations: list[Recommendation] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    data_sources_used: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────
# Strategy recommendations
# ─────────────────────────────────────────────────────────────────

def _strategy_recommendations(matrix_rows: list[dict]) -> list[Recommendation]:
    """Generate strategy-level recommendations from research matrix."""
    recs = []
    if not matrix_rows:
        return recs

    valid = [r for r in matrix_rows if r.get("trades", 0) > 5 and not r.get("error")]

    # Top performers
    strong = [r for r in valid if r.get("research_score", 0) >= 80]
    for r in strong[:5]:
        recs.append(Recommendation(
            category="strategy", priority="high", confidence="medium",
            title=f"{r['symbol']} + {r['template']} is a research candidate (score {r['research_score']})",
            evidence=[
                f"Profit factor: {r.get('profit_factor', 0):.2f}",
                f"Net profit: ${r.get('net_profit', 0):.2f}",
                f"Max drawdown: {r.get('max_drawdown', 0):.2f}%",
                f"Trade count: {r.get('trades', 0)}",
            ],
            suggested_next_action=f"Run walk-forward validation on {r['symbol']} {r['template']}",
            source_metrics={"symbol": r["symbol"], "template": r["template"],
                           "score": r.get("research_score"), "pf": r.get("profit_factor")},
        ))

    # Weak performers to avoid
    weak = [r for r in valid if r.get("research_score", 0) < 45 and r.get("trades", 0) >= 10]
    if weak:
        worst = sorted(weak, key=lambda r: r.get("research_score", 0))[:3]
        for r in worst:
            recs.append(Recommendation(
                category="strategy", priority="low", confidence="medium",
                title=f"{r['symbol']} + {r['template']} underperforms in this sample",
                evidence=[
                    f"Research score: {r.get('research_score', 0)}",
                    f"Profit factor: {r.get('profit_factor', 0):.2f}",
                    f"Net profit: ${r.get('net_profit', 0):.2f}",
                ],
                suggested_next_action="Deprioritise or investigate alternative parameters",
                source_metrics={"symbol": r["symbol"], "template": r["template"]},
            ))

    return recs


# ─────────────────────────────────────────────────────────────────
# Symbol recommendations
# ─────────────────────────────────────────────────────────────────

def _symbol_recommendations(matrix_rows: list[dict]) -> list[Recommendation]:
    """Generate symbol-level recommendations."""
    recs = []
    valid = [r for r in matrix_rows if r.get("trades", 0) > 5 and not r.get("error")]

    # Average score by symbol
    by_sym: dict[str, list[int]] = {}
    for r in valid:
        by_sym.setdefault(r["symbol"], []).append(r.get("research_score", 0))

    ranked = sorted(
        [(s, sum(scores) / len(scores), len(scores)) for s, scores in by_sym.items()],
        key=lambda x: x[1], reverse=True,
    )

    # Top symbols
    for sym, avg, cnt in ranked[:3]:
        best = max((r for r in valid if r["symbol"] == sym), key=lambda r: r.get("research_score", 0))
        recs.append(Recommendation(
            category="symbol", priority="high" if avg >= 70 else "medium",
            confidence="medium",
            title=f"{sym} is a strong research symbol (avg score {avg:.0f})",
            evidence=[
                f"Average score across {cnt} templates: {avg:.1f}",
                f"Best template: {best['template']} (score {best.get('research_score', 0)})",
            ],
            suggested_next_action=f"Run H4 timeframe validation on {sym}",
            source_metrics={"symbol": sym, "avg_score": round(avg, 1)},
        ))

    # Weakest symbols
    for sym, avg, cnt in ranked[-2:]:
        if avg < 50:
            recs.append(Recommendation(
                category="symbol", priority="low", confidence="medium",
                title=f"{sym} scores poorly across templates (avg {avg:.0f})",
                evidence=[f"Average score: {avg:.1f} across {cnt} templates"],
                suggested_next_action="Investigate whether spread or volatility causes underperformance",
                source_metrics={"symbol": sym, "avg_score": round(avg, 1)},
            ))

    return recs


# ─────────────────────────────────────────────────────────────────
# Regime recommendations
# ─────────────────────────────────────────────────────────────────

def _regime_recommendations(regime_data: dict | None, filter_data: dict | None) -> list[Recommendation]:
    """Generate regime-level recommendations."""
    recs = []

    if regime_data:
        current = regime_data.get("current_regime", "")
        if current:
            recs.append(Recommendation(
                category="regime", priority="medium", confidence="medium",
                title=f"Current market regime is {current}",
                evidence=[
                    f"Regime distribution: {regime_data.get('regime_pct', {})}",
                    f"Persistence: {regime_data.get('persistence', {})}",
                ],
                suggested_next_action=f"Favour strategies suited to {current} conditions",
                source_metrics={"current_regime": current},
            ))

    if filter_data:
        comp = filter_data.get("comparison", {})
        verdict = comp.get("filter_verdict", "")
        improvement = comp.get("improvement_pct", 0)

        if verdict == "improved" and improvement > 20:
            recs.append(Recommendation(
                category="regime", priority="high", confidence="medium",
                title=f"Regime filtering improved performance by {improvement:.0f}%",
                evidence=[
                    f"Baseline PF: {comp.get('baseline_profit_factor', 0):.2f}",
                    f"Filtered PF: {comp.get('filtered_profit_factor', 0):.2f}",
                    f"Trades skipped: {comp.get('skipped_trades', 0)}",
                    f"Drawdown reduced: {comp.get('baseline_max_drawdown', 0):.2f}% → {comp.get('filtered_max_drawdown', 0):.2f}%",
                ],
                suggested_next_action="Validate regime filter with walk-forward analysis",
                source_metrics=comp,
            ))
        elif verdict == "improved":
            recs.append(Recommendation(
                category="regime", priority="medium", confidence="low",
                title="Regime filtering shows modest improvement — requires more validation",
                evidence=[f"Improvement: {improvement:.1f}%"],
                suggested_next_action="Run on longer data or additional timeframes",
            ))

    return recs


# ─────────────────────────────────────────────────────────────────
# Portfolio recommendations
# ─────────────────────────────────────────────────────────────────

def _portfolio_recommendations(portfolios: list[dict]) -> list[Recommendation]:
    """Generate portfolio-level recommendations."""
    recs = []
    if not portfolios:
        return recs

    for p in portfolios:
        name = p.get("name", "Portfolio")
        score = p.get("portfolio_score", 0)
        label = p.get("score_label", "")
        metrics = p.get("metrics", {})
        div = p.get("diversification_score", 0)
        benefit = p.get("diversification_benefit", 0)
        worst = p.get("worst_component", "")

        if score >= 80:
            recs.append(Recommendation(
                category="portfolio", priority="high", confidence="medium",
                title=f"{name} is a strong research portfolio (score {score})",
                evidence=[
                    f"Return: {metrics.get('total_return_pct', 0):.2f}%",
                    f"Drawdown: {metrics.get('max_drawdown', 0):.2f}%",
                    f"Diversification: {div}/100",
                    f"Diversification benefit: ${benefit:.2f} vs best individual",
                ],
                suggested_next_action="Run walk-forward validation on portfolio components",
                source_metrics={"score": score, "div": div, "return": metrics.get("total_return_pct")},
            ))
        elif score >= 50:
            recs.append(Recommendation(
                category="portfolio", priority="medium", confidence="low",
                title=f"{name} shows potential but needs investigation (score {score})",
                evidence=[
                    f"Return: {metrics.get('total_return_pct', 0):.2f}%",
                    f"Worst component: {worst}",
                ],
                suggested_next_action=f"Test variant excluding {worst}" if worst else "Test alternative components",
            ))

        # Weak component warning
        components = p.get("components", [])
        for c in components:
            if c.get("net_profit", 0) < -10 and c.get("profit_factor", 1) < 0.5:
                recs.append(Recommendation(
                    category="investigation", priority="medium", confidence="medium",
                    title=f"{c['symbol']} {c['template']} is dragging {name} down",
                    evidence=[
                        f"Component net: ${c['net_profit']:.2f}",
                        f"Component PF: {c['profit_factor']:.2f}",
                    ],
                    suggested_next_action=f"Replace {c['symbol']} {c['template']} with a stronger pair or remove",
                ))

    return recs


# ─────────────────────────────────────────────────────────────────
# Investigation recommendations
# ─────────────────────────────────────────────────────────────────

def _investigation_recommendations(matrix_rows: list[dict]) -> list[Recommendation]:
    """Suggest follow-up research."""
    recs = []
    valid = [r for r in matrix_rows if r.get("trades", 0) > 5 and not r.get("error")]

    promising = [r for r in valid if 65 <= r.get("research_score", 0) < 80]
    for r in promising[:3]:
        recs.append(Recommendation(
            category="investigation", priority="medium", confidence="low",
            title=f"Investigate {r['symbol']} + {r['template']} further (score {r['research_score']})",
            evidence=[
                f"PF: {r.get('profit_factor', 0):.2f}, Net: ${r.get('net_profit', 0):.2f}",
                f"Potential to improve with parameter optimisation or regime filtering",
            ],
            suggested_next_action=f"Run optimisation + regime filter on {r['symbol']} {r['template']} H1",
        ))

    # Suggest H4 for top H1 performers
    strong_h1 = [r for r in valid if r.get("research_score", 0) >= 80 and r.get("timeframe") == "H1"]
    if strong_h1:
        recs.append(Recommendation(
            category="investigation", priority="medium", confidence="low",
            title="Validate top H1 results on H4 timeframe",
            evidence=[f"{len(strong_h1)} strategies scored 80+ on H1"],
            suggested_next_action="Run research matrix with timeframes=['H4'] for the same symbols",
        ))

    return recs


# ─────────────────────────────────────────────────────────────────
# Main recommender
# ─────────────────────────────────────────────────────────────────

def generate_recommendations(
    matrix_rows: list[dict] | None = None,
    regime_data: dict | None = None,
    filter_data: dict | None = None,
    portfolios: list[dict] | None = None,
    scope: str = "all",
) -> RecommendationResult:
    """
    Generate research recommendations from available data.

    scope: "strategy", "symbol", "regime", "portfolio", "all"
    """
    all_recs: list[Recommendation] = []
    sources: list[str] = []

    if matrix_rows and scope in ("all", "strategy"):
        all_recs.extend(_strategy_recommendations(matrix_rows))
        sources.append("research_matrix")

    if matrix_rows and scope in ("all", "symbol"):
        all_recs.extend(_symbol_recommendations(matrix_rows))

    if (regime_data or filter_data) and scope in ("all", "regime"):
        all_recs.extend(_regime_recommendations(regime_data, filter_data))
        if regime_data:
            sources.append("regime_analysis")
        if filter_data:
            sources.append("regime_filter")

    if portfolios and scope in ("all", "portfolio"):
        all_recs.extend(_portfolio_recommendations(portfolios))
        sources.append("portfolio_research")

    if matrix_rows and scope in ("all", "investigation"):
        all_recs.extend(_investigation_recommendations(matrix_rows))

    # Sort: high priority first
    priority_order = {"high": 0, "medium": 1, "low": 2}
    all_recs.sort(key=lambda r: priority_order.get(r.priority, 3))

    # Summary
    summary = {
        "total_recommendations": len(all_recs),
        "high_priority": sum(1 for r in all_recs if r.priority == "high"),
        "medium_priority": sum(1 for r in all_recs if r.priority == "medium"),
        "low_priority": sum(1 for r in all_recs if r.priority == "low"),
        "categories": list(set(r.category for r in all_recs)),
    }

    return RecommendationResult(
        recommendations=all_recs,
        summary=summary,
        data_sources_used=list(set(sources)),
    )


# ─────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────

def result_to_dict(result: RecommendationResult) -> dict:
    return {
        "recommendations": [
            {
                "category": r.category,
                "priority": r.priority,
                "confidence": r.confidence,
                "title": r.title,
                "evidence": r.evidence,
                "suggested_next_action": r.suggested_next_action,
                "source_metrics": r.source_metrics,
            }
            for r in result.recommendations
        ],
        "summary": result.summary,
        "warnings": result.warnings,
        "data_sources_used": result.data_sources_used,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "research",
        "mode_label": "Research Mode Recommendations",
        "mode_disclaimer": (
            "These recommendations are based on historical simulated data only. "
            "They are research candidates, not trading advice. Past performance "
            "does not guarantee future results. All strategies require further "
            "validation before any deployment consideration."
        ),
    }
