"""
GuvFX Parameter Optimiser + Walk-Forward Validation

Grid-search parameter optimisation with train/validation split
to detect overfitting.  Research Mode only — no live execution.

Usage:
    from backtests.optimiser import run_optimisation
    result = run_optimisation(
        bars=bars, template_name="rsi_mean_reversion",
        param_grid={"rsi_period": [10, 14, 21], "sl_pips": [20, 25, 30]},
        score_metric="profit_factor",
    )
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Optional

from backtests.engine import Bar, BacktestResult, run_template_backtest

logger = logging.getLogger(__name__)

MAX_COMBINATIONS = 500  # Hard cap to prevent overload


# ─────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────

@dataclass
class ParamSetResult:
    """Result of a single parameter combination."""
    params: dict
    metrics: dict
    score: float
    trade_count: int


@dataclass
class WalkForwardResult:
    """Result of walk-forward validation for one parameter set."""
    params: dict
    train_score: float
    validation_score: float
    degradation_pct: float  # how much worse validation is vs training
    train_trades: int
    validation_trades: int
    train_metrics: dict
    validation_metrics: dict
    robust: bool  # True if validation score >= 50% of training score


@dataclass
class OptimisationResult:
    """Complete optimisation result."""
    template_name: str
    symbol: str
    timeframe: str
    score_metric: str
    total_combinations: int
    completed_combinations: int
    # Top results from full-data optimisation
    top_results: list[ParamSetResult] = field(default_factory=list)
    # Walk-forward validation (if enabled)
    walk_forward: list[WalkForwardResult] = field(default_factory=list)
    train_bars: int = 0
    validation_bars: int = 0
    # Metadata
    error: str = ""
    warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────

SCORE_METRICS = {
    "profit_factor": lambda m: m.get("profit_factor", 0),
    "net_profit": lambda m: m.get("net_profit", 0),
    "max_drawdown": lambda m: -m.get("max_drawdown", 100),  # lower is better → negate
    "expectancy": lambda m: m.get("expectancy", 0),
    "return_drawdown_ratio": lambda m: (
        m.get("total_return_pct", 0) / max(m.get("max_drawdown", 0.01), 0.01)
    ),
    "win_rate": lambda m: m.get("win_rate", 0),
}


def score_result(metrics: dict, metric_name: str) -> float:
    """Extract score from metrics using the specified metric."""
    scorer = SCORE_METRICS.get(metric_name, SCORE_METRICS["profit_factor"])
    try:
        return scorer(metrics)
    except (TypeError, ZeroDivisionError):
        return -999.0


# ─────────────────────────────────────────────────────────────────────
# Grid generation
# ─────────────────────────────────────────────────────────────────────

def generate_param_grid(param_ranges: dict[str, list]) -> list[dict]:
    """
    Generate all combinations of parameter values.
    Caps at MAX_COMBINATIONS to prevent runaway grids.
    """
    keys = sorted(param_ranges.keys())
    values = [param_ranges[k] for k in keys]

    total = 1
    for v in values:
        total *= len(v)

    if total > MAX_COMBINATIONS:
        raise ValueError(
            f"Parameter grid has {total} combinations (max {MAX_COMBINATIONS}). "
            f"Reduce ranges or number of parameters."
        )

    combos = []
    for combo in itertools.product(*values):
        combos.append(dict(zip(keys, combo)))

    return combos


# ─────────────────────────────────────────────────────────────────────
# Core optimiser
# ─────────────────────────────────────────────────────────────────────

def run_optimisation(
    bars: list[Bar],
    template_name: str,
    param_grid: dict[str, list],
    score_metric: str = "profit_factor",
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    initial_balance: float = 10000.0,
    lots: float = 0.01,
    spread_pips: float = 1.5,
    walk_forward: bool = True,
    train_pct: float = 0.7,
    top_n: int = 10,
) -> OptimisationResult:
    """
    Run parameter optimisation with optional walk-forward validation.

    1. Generate parameter grid
    2. Run backtest for each combination on full data (or training window)
    3. Rank by score_metric
    4. If walk_forward: re-run top candidates on validation window
    5. Flag overfitting (validation score < 50% of training score)

    No live execution. No ExecutionJob. No MT5 orders.
    """
    if score_metric not in SCORE_METRICS:
        return OptimisationResult(
            template_name=template_name, symbol=symbol, timeframe=timeframe,
            score_metric=score_metric, total_combinations=0, completed_combinations=0,
            error=f"Unknown score metric: {score_metric}. Available: {list(SCORE_METRICS.keys())}",
        )

    try:
        combos = generate_param_grid(param_grid)
    except ValueError as e:
        return OptimisationResult(
            template_name=template_name, symbol=symbol, timeframe=timeframe,
            score_metric=score_metric, total_combinations=0, completed_combinations=0,
            error=str(e),
        )

    total = len(combos)
    warnings: list[str] = []

    if total > 200:
        warnings.append(f"Large grid ({total} combinations) — may take time.")

    # ── Split data for walk-forward ──
    if walk_forward and len(bars) > 100:
        split_idx = int(len(bars) * train_pct)
        train_bars = bars[:split_idx]
        val_bars = bars[split_idx:]
    else:
        train_bars = bars
        val_bars = []
        if walk_forward:
            warnings.append("Walk-forward disabled: not enough bars for split.")
            walk_forward = False

    # ── Run optimisation on training window ──
    results: list[ParamSetResult] = []
    completed = 0

    for params in combos:
        try:
            bt = run_template_backtest(
                train_bars, template_name=template_name, params=params,
                symbol=symbol, timeframe=timeframe,
                initial_balance=initial_balance, lots=lots,
                spread_pips=spread_pips,
            )
            s = score_result(bt.metrics, score_metric)
            results.append(ParamSetResult(
                params=params, metrics=bt.metrics,
                score=s, trade_count=bt.metrics.get("total_trades", 0),
            ))
            completed += 1
        except Exception as e:
            logger.warning(f"Optimiser: params={params} error={e}")
            completed += 1

    # Sort by score descending
    results.sort(key=lambda r: r.score, reverse=True)
    top_results = results[:top_n]

    # ── Walk-forward validation ──
    wf_results: list[WalkForwardResult] = []

    if walk_forward and val_bars:
        for pr in top_results[:5]:  # Validate top 5 only
            try:
                val_bt = run_template_backtest(
                    val_bars, template_name=template_name, params=pr.params,
                    symbol=symbol, timeframe=timeframe,
                    initial_balance=initial_balance, lots=lots,
                    spread_pips=spread_pips,
                )
                val_score = score_result(val_bt.metrics, score_metric)
                train_score = pr.score

                # Degradation: how much worse is validation vs training
                if train_score > 0:
                    degradation = (1 - val_score / train_score) * 100
                elif train_score == 0:
                    degradation = 0 if val_score >= 0 else 100
                else:
                    degradation = 0 if val_score <= train_score else -100

                robust = val_score >= train_score * 0.5 if train_score > 0 else val_score >= 0

                wf_results.append(WalkForwardResult(
                    params=pr.params,
                    train_score=round(train_score, 4),
                    validation_score=round(val_score, 4),
                    degradation_pct=round(degradation, 1),
                    train_trades=pr.trade_count,
                    validation_trades=val_bt.metrics.get("total_trades", 0),
                    train_metrics=pr.metrics,
                    validation_metrics=val_bt.metrics,
                    robust=robust,
                ))
            except Exception as e:
                logger.warning(f"Walk-forward validation error: params={pr.params} error={e}")

        # Sort walk-forward by validation score
        wf_results.sort(key=lambda r: r.validation_score, reverse=True)

        # Overfitting warning
        overfit_count = sum(1 for r in wf_results if not r.robust)
        if overfit_count == len(wf_results) and wf_results:
            warnings.append(
                "ALL top parameter sets show significant degradation in walk-forward. "
                "High risk of overfitting."
            )
        elif overfit_count > len(wf_results) / 2:
            warnings.append(
                f"{overfit_count}/{len(wf_results)} top parameter sets show degradation >50%. "
                "Moderate overfitting risk."
            )

    return OptimisationResult(
        template_name=template_name,
        symbol=symbol,
        timeframe=timeframe,
        score_metric=score_metric,
        total_combinations=total,
        completed_combinations=completed,
        top_results=top_results,
        walk_forward=wf_results,
        train_bars=len(train_bars),
        validation_bars=len(val_bars),
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────
# Serialisation helper
# ─────────────────────────────────────────────────────────────────────

def result_to_dict(result: OptimisationResult) -> dict:
    """Convert OptimisationResult to a JSON-serialisable dict."""
    return {
        "template_name": result.template_name,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "score_metric": result.score_metric,
        "total_combinations": result.total_combinations,
        "completed_combinations": result.completed_combinations,
        "train_bars": result.train_bars,
        "validation_bars": result.validation_bars,
        "warnings": result.warnings,
        "error": result.error,
        "top_results": [
            {
                "rank": i + 1,
                "params": r.params,
                "score": round(r.score, 4),
                "trade_count": r.trade_count,
                "net_profit": r.metrics.get("net_profit", 0),
                "win_rate": r.metrics.get("win_rate", 0),
                "profit_factor": r.metrics.get("profit_factor", 0),
                "max_drawdown": r.metrics.get("max_drawdown", 0),
            }
            for i, r in enumerate(result.top_results)
        ],
        "walk_forward": [
            {
                "rank": i + 1,
                "params": r.params,
                "train_score": r.train_score,
                "validation_score": r.validation_score,
                "degradation_pct": r.degradation_pct,
                "robust": r.robust,
                "train_trades": r.train_trades,
                "validation_trades": r.validation_trades,
                "validation_net_profit": r.validation_metrics.get("net_profit", 0),
                "validation_win_rate": r.validation_metrics.get("win_rate", 0),
                "validation_pf": r.validation_metrics.get("profit_factor", 0),
            }
            for i, r in enumerate(result.walk_forward)
        ],
        "mode": "research",
        "mode_label": "Research Mode Parameter Optimisation",
        "mode_disclaimer": (
            "Optimised parameters are fitted to historical data and may not "
            "perform similarly in live trading. Walk-forward validation provides "
            "some overfitting detection but is not a guarantee of future results."
        ),
    }
