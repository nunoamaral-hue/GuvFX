"""
GuvFX Portfolio Research Engine

Combines individual strategy backtests into portfolio-level analysis
with correlation, diversification scoring, and candidate ranking.

Research Mode only — no live execution, no portfolio deployment.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Optional

from backtests.engine import Bar, fetch_bars, run_template_backtest
from backtests.research_matrix import get_pip_config, ASSET_CLASS

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────

@dataclass
class PortfolioComponent:
    symbol: str
    template: str
    timeframe: str
    weight: float  # 0-1
    net_profit: float = 0
    max_drawdown: float = 0
    profit_factor: float = 0
    trades: int = 0
    returns_series: list[float] = field(default_factory=list)  # per-bar returns


@dataclass
class PortfolioResult:
    name: str
    components: list[PortfolioComponent]
    # Portfolio-level metrics
    net_profit: float = 0
    total_return_pct: float = 0
    max_drawdown: float = 0
    profit_factor: float = 0
    total_trades: int = 0
    equity_curve: list[dict] = field(default_factory=list)
    # Diversification
    correlation_matrix: list[list[float]] = field(default_factory=list)
    avg_correlation: float = 0
    diversification_score: float = 0  # 0-100
    # Scoring
    portfolio_score: int = 0
    score_label: str = ""
    # Analysis
    best_component: str = ""
    worst_component: str = ""
    diversification_benefit: float = 0  # vs best individual
    error: str = ""


# ─────────────────────────────────────────────────────────────────
# Correlation
# ─────────────────────────────────────────────────────────────────

def compute_correlation(series_a: list[float], series_b: list[float]) -> float:
    """Pearson correlation between two return series."""
    n = min(len(series_a), len(series_b))
    if n < 10:
        return 0.0

    a = series_a[:n]
    b = series_b[:n]
    mean_a = sum(a) / n
    mean_b = sum(b) / n

    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / (n - 1)
    std_a = math.sqrt(sum((x - mean_a) ** 2 for x in a) / (n - 1))
    std_b = math.sqrt(sum((x - mean_b) ** 2 for x in b) / (n - 1))

    if std_a < 1e-10 or std_b < 1e-10:
        return 0.0

    return max(-1.0, min(1.0, cov / (std_a * std_b)))


def build_correlation_matrix(components: list[PortfolioComponent]) -> list[list[float]]:
    """Build NxN correlation matrix from component return series."""
    n = len(components)
    matrix = [[0.0] * n for _ in range(n)]

    for i in range(n):
        matrix[i][i] = 1.0
        for j in range(i + 1, n):
            corr = compute_correlation(
                components[i].returns_series,
                components[j].returns_series,
            )
            matrix[i][j] = round(corr, 3)
            matrix[j][i] = round(corr, 3)

    return matrix


# ─────────────────────────────────────────────────────────────────
# Portfolio construction
# ─────────────────────────────────────────────────────────────────

def build_portfolio(
    specs: list[dict],  # [{"symbol": "EURUSD", "template": "rsi_mean_reversion", "timeframe": "H1"}]
    bar_count: int = 1000,
    initial_balance: float = 10000.0,
    weighting: str = "equal",
    name: str = "Portfolio",
) -> PortfolioResult:
    """
    Build a portfolio from component specifications.

    Each component is backtested independently, then combined
    using weighted equity curves.
    """
    n = len(specs)
    if n == 0:
        return PortfolioResult(name=name, components=[], error="No components")

    # Assign weights
    if weighting == "equal":
        weights = [1.0 / n] * n
    else:
        weights = [1.0 / n] * n  # default fallback

    # Run individual backtests
    components: list[PortfolioComponent] = []
    all_equity_series: list[list[float]] = []

    for i, spec in enumerate(specs):
        sym = spec["symbol"]
        tmpl = spec["template"]
        tf = spec.get("timeframe", "H1")
        params = spec.get("params", {})

        try:
            bars = fetch_bars(sym, tf, count=bar_count)
            cfg = get_pip_config(sym)

            bt = run_template_backtest(
                bars, tmpl, params=params,
                symbol=sym, timeframe=tf,
                initial_balance=initial_balance * weights[i],
                lots=0.01,
                spread_pips=cfg["spread_points"],
                pip_value=cfg["tick_value"],
                pip_size=cfg["tick_size"],
            )

            m = bt.metrics

            # Extract per-bar returns from equity curve
            eq_values = [p["equity"] for p in bt.equity_curve] if bt.equity_curve else []
            returns = []
            for j in range(1, len(eq_values)):
                if eq_values[j - 1] > 0:
                    returns.append((eq_values[j] - eq_values[j - 1]) / eq_values[j - 1])
                else:
                    returns.append(0)

            comp = PortfolioComponent(
                symbol=sym, template=tmpl, timeframe=tf,
                weight=weights[i],
                net_profit=m.get("net_profit", 0),
                max_drawdown=m.get("max_drawdown", 0),
                profit_factor=m.get("profit_factor", 0),
                trades=m.get("total_trades", 0),
                returns_series=returns,
            )
            components.append(comp)
            all_equity_series.append(eq_values)

        except Exception as e:
            logger.warning(f"Portfolio component error: {sym} {tmpl}: {e}")
            components.append(PortfolioComponent(
                symbol=sym, template=tmpl, timeframe=tf, weight=weights[i],
            ))
            all_equity_series.append([])

    # ── Combined equity curve ──
    if all_equity_series:
        max_len = max(len(s) for s in all_equity_series)
        combined_equity = []

        for j in range(max_len):
            total = 0
            for s in all_equity_series:
                if j < len(s):
                    total += s[j]
                elif s:
                    total += s[-1]  # hold last value
            combined_equity.append(round(total, 2))
    else:
        combined_equity = []

    # ── Portfolio metrics ──
    port_initial = initial_balance
    port_final = combined_equity[-1] if combined_equity else initial_balance
    port_net = round(port_final - port_initial, 2)
    port_return = round((port_final - port_initial) / port_initial * 100, 2) if port_initial > 0 else 0

    # Portfolio max drawdown
    peak = port_initial
    port_dd = 0
    for eq in combined_equity:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > port_dd:
            port_dd = dd
    port_dd = round(port_dd, 2)

    # Portfolio PF (sum of wins / sum of losses across components)
    total_profit = sum(max(c.net_profit, 0) for c in components)
    total_loss = sum(abs(min(c.net_profit, 0)) for c in components)
    port_pf = round(total_profit / total_loss, 2) if total_loss > 0 else (999.99 if total_profit > 0 else 0)

    total_trades = sum(c.trades for c in components)

    # ── Correlation matrix ──
    valid_components = [c for c in components if len(c.returns_series) > 10]
    corr_matrix = build_correlation_matrix(valid_components) if len(valid_components) > 1 else []

    # Average pairwise correlation
    if corr_matrix:
        n_corr = len(corr_matrix)
        pairs = []
        for ci in range(n_corr):
            for cj in range(ci + 1, n_corr):
                pairs.append(abs(corr_matrix[ci][cj]))
        avg_corr = round(sum(pairs) / len(pairs), 3) if pairs else 0
    else:
        avg_corr = 0

    # Diversification score: 0-100 (lower correlation = higher score)
    div_score = max(0, min(100, round((1 - avg_corr) * 100)))

    # ── Diversification benefit ──
    best_individual_net = max(c.net_profit for c in components) if components else 0
    div_benefit = round(port_net - best_individual_net, 2)

    # Best/worst components
    best_comp = max(components, key=lambda c: c.net_profit).symbol if components else ""
    worst_comp = min(components, key=lambda c: c.net_profit).symbol if components else ""

    # ── Portfolio score ──
    score = _compute_portfolio_score(port_pf, port_net, port_dd, total_trades, div_score)
    label = "STRONG" if score >= 80 else "PROMISING" if score >= 65 else "WATCHLIST" if score >= 50 else "WEAK"

    # Build timestamped equity curve
    eq_curve = [{"step": i, "equity": v} for i, v in enumerate(combined_equity)]

    return PortfolioResult(
        name=name, components=components,
        net_profit=port_net, total_return_pct=port_return,
        max_drawdown=port_dd, profit_factor=port_pf,
        total_trades=total_trades, equity_curve=eq_curve,
        correlation_matrix=corr_matrix, avg_correlation=avg_corr,
        diversification_score=div_score, portfolio_score=score,
        score_label=label, best_component=best_comp,
        worst_component=worst_comp, diversification_benefit=div_benefit,
    )


def _compute_portfolio_score(pf, net, dd, trades, div_score) -> int:
    """Portfolio research score (0-100)."""
    # PF (0-30)
    pf_s = 30 if pf >= 2 else 25 if pf >= 1.5 else 20 if pf >= 1.2 else 15 if pf >= 1 else 5 if pf >= 0.8 else 0
    # Net (0-15)
    net_s = 15 if net > 0 else 5 if net > -10 else 0
    # Drawdown (0-20)
    dd_s = 20 if dd < 2 else 15 if dd < 5 else 10 if dd < 10 else 5
    # Trades (0-10)
    tr_s = 10 if trades >= 40 else 5 if trades >= 20 else 0
    # Diversification (0-25)
    dv_s = round(div_score * 0.25)

    return max(0, min(100, pf_s + net_s + dd_s + tr_s + dv_s))


# ─────────────────────────────────────────────────────────────────
# Auto-candidate generation
# ─────────────────────────────────────────────────────────────────

def generate_candidates(
    matrix_rows: list[dict],
    max_portfolios: int = 5,
) -> list[list[dict]]:
    """Generate portfolio candidates from research matrix results."""
    valid = [r for r in matrix_rows if r.get("research_score", 0) >= 50 and r.get("trades", 0) > 5]
    if len(valid) < 2:
        return []

    candidates = []

    # A: Top 4 by score
    top4 = sorted(valid, key=lambda r: r["research_score"], reverse=True)[:4]
    if len(top4) >= 2:
        candidates.append(top4)

    # B: Best per asset class
    by_class: dict[str, dict] = {}
    for r in valid:
        ac = ASSET_CLASS.get(r["symbol"], "Other")
        if ac not in by_class or r["research_score"] > by_class[ac]["research_score"]:
            by_class[ac] = r
    diversified = list(by_class.values())[:4]
    if len(diversified) >= 2:
        candidates.append(diversified)

    # C: Lowest avg correlation (greedy)
    # Pick first by score, then add lowest-correlated
    if len(valid) >= 4:
        selected = [valid[0]]
        remaining = valid[1:]
        while len(selected) < 4 and remaining:
            # Pick the one with most different symbol from selected
            best_pick = None
            best_diversity = -1
            for r in remaining:
                # Simple diversity: count unique symbols already in selected
                sym_overlap = sum(1 for s in selected if s["symbol"] == r["symbol"])
                tmpl_overlap = sum(1 for s in selected if s["template"] == r["template"])
                diversity = 10 - sym_overlap * 5 - tmpl_overlap * 2
                if diversity > best_diversity:
                    best_diversity = diversity
                    best_pick = r
            if best_pick:
                selected.append(best_pick)
                remaining.remove(best_pick)
        if len(selected) >= 2:
            candidates.append(selected)

    return candidates[:max_portfolios]


# ─────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────

def portfolio_to_dict(p: PortfolioResult) -> dict:
    comp_labels = [f"{c.symbol} {c.template.split('_')[0]}" for c in p.components]
    return {
        "name": p.name,
        "components": [
            {"symbol": c.symbol, "template": c.template, "timeframe": c.timeframe,
             "weight": round(c.weight, 2), "net_profit": c.net_profit,
             "profit_factor": c.profit_factor, "trades": c.trades,
             "max_drawdown": c.max_drawdown}
            for c in p.components
        ],
        "metrics": {
            "net_profit": p.net_profit, "total_return_pct": p.total_return_pct,
            "max_drawdown": p.max_drawdown, "profit_factor": p.profit_factor,
            "total_trades": p.total_trades,
        },
        "correlation": {
            "matrix": p.correlation_matrix,
            "labels": comp_labels,
            "avg_correlation": p.avg_correlation,
        },
        "diversification_score": p.diversification_score,
        "portfolio_score": p.portfolio_score,
        "score_label": p.score_label,
        "best_component": p.best_component,
        "worst_component": p.worst_component,
        "diversification_benefit": p.diversification_benefit,
        "equity_curve_length": len(p.equity_curve),
        "mode": "research",
        "mode_label": "Research Mode Portfolio Analysis",
    }
