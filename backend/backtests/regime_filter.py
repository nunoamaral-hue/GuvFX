"""
GuvFX Regime-Aware Backtesting

Runs a strategy backtest twice — baseline (no filter) and filtered
(regime entry gate) — then computes comparative metrics to determine
whether regime awareness provides genuine edge.

Research Mode only.  No live execution.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from backtests.engine import (
    Bar, BacktestTrade, BacktestResult, DataQuality,
    run_template_backtest, compute_metrics, fetch_bars,
)
from backtests.regime_engine import (
    classify_regimes, RegimeParams, BULL, BEAR, SIDEWAYS, REGIMES,
)
from backtests.strategy_templates import get_template

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────

@dataclass
class RegimeFilterConfig:
    enabled: bool = False
    allowed_entry_regimes: list[str] = field(default_factory=lambda: [BULL, SIDEWAYS, BEAR])

    @property
    def blocked_regimes(self) -> set[str]:
        return set(REGIMES) - set(self.allowed_entry_regimes)


@dataclass
class ComparisonMetrics:
    """Side-by-side baseline vs filtered metrics."""
    baseline_trades: int = 0
    filtered_trades: int = 0
    skipped_trades: int = 0

    baseline_net_profit: float = 0
    filtered_net_profit: float = 0
    net_profit_diff: float = 0

    baseline_profit_factor: float = 0
    filtered_profit_factor: float = 0
    profit_factor_diff: float = 0

    baseline_win_rate: float = 0
    filtered_win_rate: float = 0
    win_rate_diff: float = 0

    baseline_max_drawdown: float = 0
    filtered_max_drawdown: float = 0
    drawdown_diff: float = 0

    improvement_pct: float = 0  # net profit improvement %

    skipped_by_regime: dict[str, int] = field(default_factory=dict)
    regime_contributing_most_losses: str = ""
    regime_contributing_most_profits: str = ""

    filter_verdict: str = ""  # "improved", "worsened", "neutral", "insufficient_data"


@dataclass
class RegimeFilterResult:
    """Complete regime filter comparison result."""
    template_name: str
    symbol: str
    timeframe: str
    filter_config: RegimeFilterConfig
    baseline: BacktestResult
    filtered: BacktestResult
    comparison: ComparisonMetrics
    regime_skip_analysis: dict = field(default_factory=dict)
    error: str = ""


# ─────────────────────────────────────────────────────────────────────
# Filtered backtest runner
# ─────────────────────────────────────────────────────────────────────

def run_filtered_backtest(
    bars: list[Bar],
    template_name: str,
    filter_config: RegimeFilterConfig,
    params: dict | None = None,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    initial_balance: float = 10000.0,
    lots: float = 0.01,
    spread_pips: float = 1.5,
    pip_value: float = 10.0,
    regime_params: RegimeParams | None = None,
) -> BacktestResult:
    """
    Run a backtest with regime entry filtering.

    Before each entry signal, checks the current regime.
    If regime is not in allowed_entry_regimes, the signal is skipped.
    Exit logic (SL/TP/signal exit) is unchanged.
    """
    from datetime import datetime

    template = get_template(template_name)
    params = {**template.default_params(), **(params or {})}

    pip_size = 0.0001 if "JPY" not in symbol.upper() else 0.01
    min_bars = template.min_bars(params)

    if len(bars) < min_bars + 5:
        return BacktestResult(
            symbol=symbol, timeframe=timeframe,
            start_date="", end_date="",
            bars_count=len(bars), initial_balance=initial_balance,
            final_balance=initial_balance,
            error=f"Not enough bars ({len(bars)})",
        )

    # Classify regimes
    regime_analysis = classify_regimes(bars, regime_params or RegimeParams())
    regime_labels = regime_analysis.labels

    # Prepare indicators
    indicators = template.prepare(bars, params)

    trades: list[BacktestTrade] = []
    equity_curve: list[dict] = []
    balance = initial_balance
    position: Optional[dict] = None
    trade_count = 0
    skipped_signals = 0
    skipped_by_regime: dict[str, int] = {r: 0 for r in REGIMES}

    for i in range(min_bars, len(bars)):
        bar = bars[i]
        bar_time = datetime.utcfromtimestamp(bar.time).strftime("%Y-%m-%dT%H:%M:%SZ")
        current_regime = regime_labels[i] if i < len(regime_labels) else SIDEWAYS

        # ── Check SL/TP on open position (unchanged) ──
        if position is not None:
            exit_price = None
            exit_reason = None

            if position["side"] == "BUY":
                if bar.low <= position["sl"]:
                    exit_price, exit_reason = position["sl"], "sl"
                elif bar.high >= position["tp"]:
                    exit_price, exit_reason = position["tp"], "tp"
            else:
                if bar.high >= position["sl"]:
                    exit_price, exit_reason = position["sl"], "sl"
                elif bar.low <= position["tp"]:
                    exit_price, exit_reason = position["tp"], "tp"

            if exit_price is None:
                sig = template.signal(i, bars, indicators, params, position)
                if sig.action == "close":
                    if position["side"] == "BUY":
                        exit_price = bar.close - spread_pips * pip_size
                    else:
                        exit_price = bar.close + spread_pips * pip_size
                    exit_reason = sig.reason or "signal_exit"

            if exit_price is not None:
                if position["side"] == "BUY":
                    pips = (exit_price - position["entry_price"]) / pip_size
                else:
                    pips = (position["entry_price"] - exit_price) / pip_size

                pnl = round(pips * pip_value * lots, 2)
                balance += pnl
                trade_count += 1
                trades.append(BacktestTrade(
                    trade_number=trade_count,
                    entry_bar=position["entry_bar"], exit_bar=i,
                    entry_time=position["entry_time"], exit_time=bar_time,
                    side=position["side"],
                    entry_price=round(position["entry_price"], 5),
                    exit_price=round(exit_price, 5),
                    sl=round(position["sl"], 5), tp=round(position["tp"], 5),
                    lots=lots, pnl=pnl,
                    exit_reason=f"{exit_reason}|regime:{position.get('entry_regime', '?')}",
                ))
                position = None

        # ── Check for new entry with regime filter ──
        if position is None:
            sig = template.signal(i, bars, indicators, params, None)
            if sig.action in ("buy", "sell"):
                # REGIME FILTER: check if current regime allows entry
                if filter_config.enabled and current_regime not in filter_config.allowed_entry_regimes:
                    skipped_signals += 1
                    skipped_by_regime[current_regime] = skipped_by_regime.get(current_regime, 0) + 1
                else:
                    if sig.action == "buy":
                        entry_price = bar.close + spread_pips * pip_size
                    else:
                        entry_price = bar.close - spread_pips * pip_size
                    position = {
                        "side": sig.action.upper(),
                        "entry_price": entry_price,
                        "sl": sig.sl, "tp": sig.tp,
                        "entry_bar": i, "entry_time": bar_time,
                        "entry_regime": current_regime,
                    }

        equity_curve.append({"timestamp": bar_time, "equity": round(balance, 2)})

    # Close open position at end
    if position is not None:
        last = bars[-1]
        if position["side"] == "BUY":
            exit_price = last.close - spread_pips * pip_size
            pips = (exit_price - position["entry_price"]) / pip_size
        else:
            exit_price = last.close + spread_pips * pip_size
            pips = (position["entry_price"] - exit_price) / pip_size
        pnl = round(pips * pip_value * lots, 2)
        balance += pnl
        trade_count += 1
        trades.append(BacktestTrade(
            trade_number=trade_count, entry_bar=position["entry_bar"],
            exit_bar=len(bars)-1, entry_time=position["entry_time"],
            exit_time=datetime.utcfromtimestamp(last.time).strftime("%Y-%m-%dT%H:%M:%SZ"),
            side=position["side"],
            entry_price=round(position["entry_price"], 5),
            exit_price=round(exit_price, 5),
            sl=round(position["sl"], 5), tp=round(position["tp"], 5),
            lots=lots, pnl=pnl, exit_reason="end_of_data",
        ))

    metrics = compute_metrics(trades, initial_balance, balance)
    metrics["skipped_signals"] = skipped_signals
    metrics["skipped_by_regime"] = skipped_by_regime

    start_date = datetime.utcfromtimestamp(bars[0].time).strftime("%Y-%m-%d") if bars else ""
    end_date = datetime.utcfromtimestamp(bars[-1].time).strftime("%Y-%m-%d") if bars else ""

    return BacktestResult(
        symbol=symbol, timeframe=timeframe,
        start_date=start_date, end_date=end_date,
        bars_count=len(bars), initial_balance=initial_balance,
        final_balance=round(balance, 2),
        trades=trades, equity_curve=equity_curve, metrics=metrics,
    )


# ─────────────────────────────────────────────────────────────────────
# Comparison runner
# ─────────────────────────────────────────────────────────────────────

def run_regime_comparison(
    bars: list[Bar],
    template_name: str,
    filter_config: RegimeFilterConfig,
    params: dict | None = None,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    initial_balance: float = 10000.0,
    lots: float = 0.01,
    spread_pips: float = 1.5,
) -> RegimeFilterResult:
    """
    Run baseline and filtered backtests, then compute comparison metrics.
    """
    # Baseline (no filter)
    baseline = run_template_backtest(
        bars, template_name, params=params,
        symbol=symbol, timeframe=timeframe,
        initial_balance=initial_balance, lots=lots, spread_pips=spread_pips,
    )

    # Filtered
    filtered = run_filtered_backtest(
        bars, template_name, filter_config, params=params,
        symbol=symbol, timeframe=timeframe,
        initial_balance=initial_balance, lots=lots, spread_pips=spread_pips,
    )

    # Comparison
    bm = baseline.metrics
    fm = filtered.metrics

    bt = bm.get("total_trades", 0)
    ft = fm.get("total_trades", 0)
    skipped = fm.get("skipped_signals", 0)

    b_net = bm.get("net_profit", 0)
    f_net = fm.get("net_profit", 0)
    net_diff = round(f_net - b_net, 2)

    b_pf = bm.get("profit_factor", 0)
    f_pf = fm.get("profit_factor", 0)

    b_wr = bm.get("win_rate", 0)
    f_wr = fm.get("win_rate", 0)

    b_dd = bm.get("max_drawdown", 0)
    f_dd = fm.get("max_drawdown", 0)

    improvement = round((f_net - b_net) / abs(b_net) * 100, 1) if b_net != 0 else 0

    # Determine which regime contributes most losses/profits from baseline regime data
    regime_perf = bm.get("regime", {}).get("performance_by_regime", {})
    most_losses = ""
    most_profits = ""
    worst_pnl = 0
    best_pnl = 0
    for r, perf in regime_perf.items():
        pnl = perf.get("net_profit", 0)
        if pnl < worst_pnl:
            worst_pnl = pnl
            most_losses = r
        if pnl > best_pnl:
            best_pnl = pnl
            most_profits = r

    # Verdict
    if ft < 3:
        verdict = "insufficient_data"
    elif f_net > b_net and f_pf > b_pf:
        verdict = "improved"
    elif f_net < b_net and f_pf < b_pf:
        verdict = "worsened"
    elif abs(f_net - b_net) < 1.0:
        verdict = "neutral"
    elif f_net > b_net:
        verdict = "improved"
    else:
        verdict = "neutral"

    comparison = ComparisonMetrics(
        baseline_trades=bt, filtered_trades=ft, skipped_trades=skipped,
        baseline_net_profit=b_net, filtered_net_profit=f_net, net_profit_diff=net_diff,
        baseline_profit_factor=b_pf, filtered_profit_factor=f_pf,
        profit_factor_diff=round(f_pf - b_pf, 2),
        baseline_win_rate=b_wr, filtered_win_rate=f_wr,
        win_rate_diff=round(f_wr - b_wr, 1),
        baseline_max_drawdown=b_dd, filtered_max_drawdown=f_dd,
        drawdown_diff=round(f_dd - b_dd, 2),
        improvement_pct=improvement,
        skipped_by_regime=fm.get("skipped_by_regime", {}),
        regime_contributing_most_losses=most_losses,
        regime_contributing_most_profits=most_profits,
        filter_verdict=verdict,
    )

    return RegimeFilterResult(
        template_name=template_name,
        symbol=symbol, timeframe=timeframe,
        filter_config=filter_config,
        baseline=baseline, filtered=filtered,
        comparison=comparison,
    )


# ─────────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────────

def comparison_to_dict(result: RegimeFilterResult) -> dict:
    c = result.comparison
    return {
        "template_name": result.template_name,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "filter_config": {
            "enabled": result.filter_config.enabled,
            "allowed_entry_regimes": result.filter_config.allowed_entry_regimes,
            "blocked_regimes": list(result.filter_config.blocked_regimes),
        },
        "comparison": {
            "baseline_trades": c.baseline_trades,
            "filtered_trades": c.filtered_trades,
            "skipped_trades": c.skipped_trades,
            "baseline_net_profit": c.baseline_net_profit,
            "filtered_net_profit": c.filtered_net_profit,
            "net_profit_diff": c.net_profit_diff,
            "baseline_profit_factor": c.baseline_profit_factor,
            "filtered_profit_factor": c.filtered_profit_factor,
            "profit_factor_diff": c.profit_factor_diff,
            "baseline_win_rate": c.baseline_win_rate,
            "filtered_win_rate": c.filtered_win_rate,
            "win_rate_diff": c.win_rate_diff,
            "baseline_max_drawdown": c.baseline_max_drawdown,
            "filtered_max_drawdown": c.filtered_max_drawdown,
            "drawdown_diff": c.drawdown_diff,
            "improvement_pct": c.improvement_pct,
            "filter_verdict": c.filter_verdict,
        },
        "regime_skip_analysis": {
            "total_skipped": c.skipped_trades,
            "skipped_by_regime": c.skipped_by_regime,
            "pct_trades_removed": round(
                c.skipped_trades / (c.baseline_trades + c.skipped_trades) * 100, 1
            ) if (c.baseline_trades + c.skipped_trades) > 0 else 0,
            "regime_most_losses": c.regime_contributing_most_losses,
            "regime_most_profits": c.regime_contributing_most_profits,
        },
        "mode": "research",
        "mode_label": "Research Mode Regime-Filtered Backtest",
        "mode_disclaimer": (
            "Regime filtering is applied to historical data in hindsight. "
            "Live regime classification may differ due to look-ahead bias in "
            "volatility calculation. Results are for research purposes only."
        ),
    }
