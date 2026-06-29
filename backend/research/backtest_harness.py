"""
Offline Backtest Harness for GuvFX Strategy Engines.

Replays historical bars through ALTS or SCE evaluation logic and simulates
execution with realistic spread, slippage, and fill models.

NO Django models, NO fetch_rates(), NO database access.
Takes pre-loaded bars and returns deterministic results.

Usage:
    from research.backtest_harness import run_backtest
    from research.data_loader import load_bars_from_csv

    bars = {
        "M5": load_bars_from_csv("EURUSD_M5.csv"),
        "M15": load_bars_from_csv("EURUSD_M15.csv"),
    }

    result = run_backtest(
        engine_slug="adaptive-liquidity-trap-scalper",
        bars_dict=bars,
        config_overrides={"alts_adx_max": 30},
        initial_balance=10000.0,
        risk_pct=1.0,
    )

    print(result.summary())
"""

from __future__ import annotations

import logging
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spread / slippage models
# ---------------------------------------------------------------------------

# Typical spreads (pips) per symbol × scenario
SPREAD_TABLE: Dict[str, Dict[str, float]] = {
    "EURUSD": {"base": 0.8, "stress": 1.5},
    "GBPUSD": {"base": 1.2, "stress": 2.0},
}

DEFAULT_SPREAD = {"base": 1.0, "stress": 2.0}


def get_spread_pips(symbol: str, scenario: str = "base") -> float:
    """Get spread in pips for a symbol and scenario."""
    entry = SPREAD_TABLE.get(symbol.upper(), DEFAULT_SPREAD)
    return entry.get(scenario, entry.get("base", 1.0))


@dataclass
class SlippageModel:
    """
    Execution slippage model.

    Slippage is expressed in R-units added to stop distance.
    Three scenarios:
      - Base: +0.1R always
      - Stress: 10% of trades get +0.3R
      - Shock: 2% of trades get +0.6R
    """

    base_r: float = 0.1
    stress_prob: float = 0.10
    stress_r: float = 0.3
    shock_prob: float = 0.02
    shock_r: float = 0.6

    def draw_slippage_r(self, rng: random.Random) -> float:
        """Draw random slippage in R-units."""
        roll = rng.random()
        if roll < self.shock_prob:
            return self.shock_r
        elif roll < self.shock_prob + self.stress_prob:
            return self.stress_r
        return self.base_r


# ---------------------------------------------------------------------------
# Trade result
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """A single simulated trade."""

    bar_idx: int = 0
    bar_time: str = ""
    side: str = ""  # "BUY" or "SELL"
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    lots: float = 0.01

    # Simulated execution
    fill_price: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""  # "tp_hit", "sl_hit", "end_of_data"

    # P&L
    pnl_r: float = 0.0  # P&L in R-units
    pnl_usd: float = 0.0
    slippage_r: float = 0.0

    # Diagnostics
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Backtest result
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """Summary of a backtest run."""

    engine_slug: str = ""
    symbol: str = ""
    scenario: str = "base"
    initial_balance: float = 10000.0
    risk_pct: float = 1.0

    trades: List[BacktestTrade] = field(default_factory=list)
    total_pnl_r: float = 0.0
    total_pnl_usd: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_r: float = 0.0
    max_consecutive_losses: int = 0
    expectancy_r: float = 0.0

    def summary(self) -> str:
        """Human-readable summary."""
        return (
            f"=== Backtest: {self.engine_slug} ({self.scenario}) ===\n"
            f"Trades: {len(self.trades)} (W: {self.win_count}, L: {self.loss_count})\n"
            f"Win rate: {self.win_rate:.1%}\n"
            f"Total P&L: {self.total_pnl_r:+.2f}R (${self.total_pnl_usd:+.2f})\n"
            f"Avg win: {self.avg_win_r:+.2f}R, Avg loss: {self.avg_loss_r:.2f}R\n"
            f"Profit factor: {self.profit_factor:.2f}\n"
            f"Expectancy: {self.expectancy_r:+.4f}R\n"
            f"Max DD: {self.max_drawdown_r:.2f}R\n"
            f"Max consec losses: {self.max_consecutive_losses}\n"
        )


def _compute_stats(trades: List[BacktestTrade]) -> dict:
    """Compute aggregate statistics from a list of trades."""
    if not trades:
        return {
            "total_pnl_r": 0.0,
            "total_pnl_usd": 0.0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_r": 0.0,
            "max_consecutive_losses": 0,
            "expectancy_r": 0.0,
        }

    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r <= 0]

    total_pnl_r = sum(t.pnl_r for t in trades)
    total_pnl_usd = sum(t.pnl_usd for t in trades)

    gross_profit = sum(t.pnl_r for t in wins)
    gross_loss = abs(sum(t.pnl_r for t in losses))

    win_rate = len(wins) / len(trades) if trades else 0
    avg_win_r = statistics.mean([t.pnl_r for t in wins]) if wins else 0
    avg_loss_r = statistics.mean([t.pnl_r for t in losses]) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    expectancy_r = total_pnl_r / len(trades) if trades else 0

    # Max drawdown in R
    peak = 0.0
    max_dd = 0.0
    running = 0.0
    for t in trades:
        running += t.pnl_r
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    # Max consecutive losses
    max_consec = 0
    current_consec = 0
    for t in trades:
        if t.pnl_r <= 0:
            current_consec += 1
            if current_consec > max_consec:
                max_consec = current_consec
        else:
            current_consec = 0

    return {
        "total_pnl_r": total_pnl_r,
        "total_pnl_usd": total_pnl_usd,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": win_rate,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "profit_factor": profit_factor,
        "max_drawdown_r": max_dd,
        "max_consecutive_losses": max_consec,
        "expectancy_r": expectancy_r,
    }


# ---------------------------------------------------------------------------
# Signal extraction (engine-agnostic)
# ---------------------------------------------------------------------------

def _extract_signals_alts(
    bars_dict: Dict[str, List[Dict[str, Any]]],
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Extract ALTS signals from bars without DB or Django.

    Uses indicators.py directly for the ALTS evaluation pipeline.
    Returns list of raw signal dicts.
    """
    from strategies.indicators import (
        compute_atr,
        compute_atr_series,
        compute_adx,
        compute_ema,
        find_pivot_highs,
        find_pivot_lows,
        atr_percentile,
        body_size,
        bar_direction,
        bar_midpoint,
        range_size,
    )
    from strategies.engines.alts_engine import (
        ALTSConfig,
        _detect_regime_m15,
        _find_liquidity_pools,
        _detect_sweep,
        _detect_displacement,
        _detect_confirmation,
        _compute_dynamic_rr,
    )
    from strategies.execution_guards import get_pip_size, normalize_prices

    m5_bars = bars_dict.get("M5", [])
    m15_bars = bars_dict.get("M15", [])

    if len(m5_bars) < 100 or len(m15_bars) < 60:
        return []

    alts_config = ALTSConfig(**{
        k.replace("alts_", ""): v for k, v in config.items()
        if k.startswith("alts_")
    }) if any(k.startswith("alts_") for k in config) else ALTSConfig()

    signals: List[Dict[str, Any]] = []

    # Slide through M5 bars in chunks (simulate bar-by-bar evaluation)
    window_size = 200
    step = 1

    for end_idx in range(window_size, len(m5_bars), step):
        m5_window = m5_bars[end_idx - window_size:end_idx]

        # Align M15 window to M5 time
        m5_end_time = m5_window[-1]["time"]
        m15_window = [b for b in m15_bars if b["time"] <= m5_end_time][-100:]

        if len(m15_window) < 60:
            continue

        # Regime check
        regime, _ = _detect_regime_m15(m15_window, alts_config)
        if regime != "RANGE":
            continue

        # ATR
        atr14 = compute_atr(m5_window, period=14)
        if atr14 <= 0:
            continue

        # Shock candle
        last_range = range_size(m5_window[-1])
        if last_range > alts_config.shock_candle_atr_mult * atr14:
            continue

        # Liquidity pools
        high_pools, low_pools = _find_liquidity_pools(
            m5_window, alts_config.fractal_left, atr14,
            alts_config.pool_cluster_atr_mult, alts_config.min_pool_touches,
        )

        if not high_pools and not low_pools:
            continue

        sweep_breach = alts_config.sweep_wick_breach_atr * atr14
        disp_body = alts_config.displacement_body_atr * atr14

        best = None
        for pool in sorted(high_pools, key=lambda p: max(p["indices"]), reverse=True):
            sweep = _detect_sweep(m5_window, pool["level"], "high", sweep_breach, max(pool["indices"]))
            if not sweep:
                continue
            disp = _detect_displacement(m5_window, sweep, disp_body)
            if not disp:
                continue
            conf = _detect_confirmation(m5_window, disp, alts_config.confirm_within_bars)
            if not conf:
                continue
            best = {"side": "BUY", "sweep": sweep, "pool": pool, "disp": disp, "conf": conf}
            break

        if not best:
            for pool in sorted(low_pools, key=lambda p: max(p["indices"]), reverse=True):
                sweep = _detect_sweep(m5_window, pool["level"], "low", sweep_breach, max(pool["indices"]))
                if not sweep:
                    continue
                disp = _detect_displacement(m5_window, sweep, disp_body)
                if not disp:
                    continue
                conf = _detect_confirmation(m5_window, disp, alts_config.confirm_within_bars)
                if not conf:
                    continue
                best = {"side": "SELL", "sweep": sweep, "pool": pool, "disp": disp, "conf": conf}
                break

        if not best:
            continue

        # Entry/SL/TP
        entry = float(m5_window[-1]["close"])
        sl_buffer = alts_config.sweep_buffer_atr * atr14
        if best["side"] == "BUY":
            sl = best["sweep"]["sweep_low"] - sl_buffer
        else:
            sl = best["sweep"]["sweep_high"] + sl_buffer

        atr_ser = compute_atr_series(m5_window, 14)
        atrp = atr_percentile(atr_ser, 50, len(m5_window) - 1)
        rr = _compute_dynamic_rr(atrp, alts_config)

        stop_dist = abs(entry - sl)
        if stop_dist <= 0:
            continue

        if best["side"] == "BUY":
            tp = entry + rr * stop_dist
        else:
            tp = entry - rr * stop_dist

        signals.append({
            "bar_idx": end_idx - 1,  # Index in full m5_bars
            "bar_time": m5_window[-1]["time"],
            "side": best["side"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
        })

        # Skip ahead after signal to avoid overlapping trades
        # (crude: skip 10 bars to simulate position hold)
        # This is handled in the main run_backtest loop instead

    return signals


def _extract_signals_sce(
    bars_dict: Dict[str, List[Dict[str, Any]]],
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Extract SCE signals from bars without DB or Django.

    Uses indicators.py directly for the SCE evaluation pipeline.
    Returns list of raw signal dicts.
    """
    from strategies.indicators import (
        compute_atr,
        compute_adx,
        compute_ema,
        find_pivot_highs,
        find_pivot_lows,
        body_size,
        bar_direction,
    )
    from strategies.engines.sce_engine import (
        SCEConfig,
        _detect_bias_h4,
        _detect_bos_h1,
        _detect_pullback,
        _detect_rejection,
    )

    h1_bars = bars_dict.get("H1", [])
    h4_bars = bars_dict.get("H4", [])

    if len(h1_bars) < 100 or len(h4_bars) < 60:
        return []

    sce_config = SCEConfig(**{
        k.replace("sce_", ""): v for k, v in config.items()
        if k.startswith("sce_")
    }) if any(k.startswith("sce_") for k in config) else SCEConfig()

    signals: List[Dict[str, Any]] = []
    window_size = 150

    for end_idx in range(window_size, len(h1_bars)):
        h1_window = h1_bars[end_idx - window_size:end_idx]
        h1_end_time = h1_window[-1]["time"]
        h4_window = [b for b in h4_bars if b["time"] <= h1_end_time][-100:]

        if len(h4_window) < 60:
            continue

        # Bias
        bias, bias_diag = _detect_bias_h4(h4_window, sce_config)
        if bias == "NONE":
            continue

        # ATR
        atr14 = compute_atr(h1_window, 14)
        if atr14 <= 0:
            continue

        # BOS
        bos, _ = _detect_bos_h1(h1_window, bias, atr14, sce_config)
        if not bos or bos["direction"] != bias:
            continue

        # Pullback
        pullback, _ = _detect_pullback(h1_window, bos, sce_config)
        if not pullback:
            continue

        # Rejection
        rejection, _ = _detect_rejection(h1_window, pullback, atr14, sce_config)
        if not rejection:
            continue

        # Entry/SL/TP
        entry = float(h1_window[-1]["close"])
        side = "BUY" if bias == "BULL" else "SELL"
        sl_buffer = sce_config.pullback_buffer_atr * atr14

        if side == "BUY":
            sl = pullback["low"] - sl_buffer
        else:
            sl = pullback["high"] + sl_buffer

        adx_val = bias_diag.get("adx", 0)
        rr = sce_config.rr_strong if adx_val >= sce_config.adx_strong_threshold else sce_config.rr_base
        stop_dist = abs(entry - sl)
        if stop_dist <= 0:
            continue

        if side == "BUY":
            tp = entry + rr * stop_dist
        else:
            tp = entry - rr * stop_dist

        signals.append({
            "bar_idx": end_idx - 1,
            "bar_time": h1_window[-1]["time"],
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
        })

    return signals


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def _simulate_trade(
    signal: Dict[str, Any],
    bars: List[Dict[str, Any]],
    spread_pips: float,
    slippage_model: SlippageModel,
    rng: random.Random,
    pip_size: float,
    risk_amount: float,
) -> BacktestTrade:
    """
    Simulate a single trade through subsequent bars.

    Applies spread to entry, slippage to SL, then walks forward to find
    exit (TP hit, SL hit, or end of data).
    """
    bar_idx = signal["bar_idx"]
    side = signal["side"]
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]

    # Apply spread to entry
    spread_price = spread_pips * pip_size
    if side == "BUY":
        fill_price = entry + spread_price / 2  # Buy at ask
    else:
        fill_price = entry - spread_price / 2  # Sell at bid

    # Apply slippage to stop (worsens SL)
    slip_r = slippage_model.draw_slippage_r(rng)
    stop_dist = abs(fill_price - sl)
    slip_price = slip_r * stop_dist

    if side == "BUY":
        actual_sl = sl - slip_price  # Worse SL for BUY
    else:
        actual_sl = sl + slip_price  # Worse SL for SELL

    # Walk forward
    exit_price = fill_price
    exit_reason = "end_of_data"

    for i in range(bar_idx + 1, len(bars)):
        bar = bars[i]
        h = float(bar["high"])
        l = float(bar["low"])

        if side == "BUY":
            # Check SL first (conservative: assume worst fill)
            if l <= actual_sl:
                exit_price = actual_sl
                exit_reason = "sl_hit"
                break
            if h >= tp:
                exit_price = tp
                exit_reason = "tp_hit"
                break
        else:
            if h >= actual_sl:
                exit_price = actual_sl
                exit_reason = "sl_hit"
                break
            if l <= tp:
                exit_price = tp
                exit_reason = "tp_hit"
                break

    # Compute P&L
    if side == "BUY":
        pnl_pips = (exit_price - fill_price) / pip_size
    else:
        pnl_pips = (fill_price - exit_price) / pip_size

    # P&L in R-units
    stop_pips = abs(fill_price - sl) / pip_size
    pnl_r = pnl_pips / stop_pips if stop_pips > 0 else 0

    # P&L in USD (approximate: pip value * pips * lots)
    pip_value = 10.0  # Per standard lot for USD-quoted pairs
    pnl_usd = pnl_pips * pip_value * signal.get("lots", 0.01)

    return BacktestTrade(
        bar_idx=bar_idx,
        bar_time=signal["bar_time"],
        side=side,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        fill_price=fill_price,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_r=round(pnl_r, 4),
        pnl_usd=round(pnl_usd, 2),
        slippage_r=round(slip_r, 4),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_backtest(
    engine_slug: str,
    bars_dict: Dict[str, List[Dict[str, Any]]],
    config_overrides: Optional[Dict[str, Any]] = None,
    initial_balance: float = 10000.0,
    risk_pct: float = 1.0,
    symbol: str = "EURUSD",
    scenarios: Optional[List[str]] = None,
    seed: int = 42,
) -> Dict[str, BacktestResult]:
    """
    Run backtest for an engine across one or more scenarios.

    Parameters
    ----------
    engine_slug : "adaptive-liquidity-trap-scalper" or "structural-continuation-engine"
    bars_dict : dict of timeframe -> bars (e.g. {"M5": [...], "M15": [...]})
    config_overrides : override strategy.filters keys
    initial_balance : starting account balance
    risk_pct : risk per trade as percentage
    symbol : trading pair
    scenarios : list of ["base", "stress", "shock"] (default: all three)
    seed : random seed for reproducibility

    Returns
    -------
    dict of scenario_name -> BacktestResult
    """
    config = config_overrides or {}
    scenarios = scenarios or ["base", "stress", "shock"]
    pip_size = 0.0001 if "JPY" not in symbol.upper() else 0.01

    # Extract signals
    logger.info("[BACKTEST] Extracting signals for %s...", engine_slug)

    if engine_slug == "adaptive-liquidity-trap-scalper":
        execution_tf = "M5"
        signals = _extract_signals_alts(bars_dict, config)
    elif engine_slug == "structural-continuation-engine":
        execution_tf = "H1"
        signals = _extract_signals_sce(bars_dict, config)
    else:
        raise ValueError(f"Unknown engine: {engine_slug}")

    logger.info("[BACKTEST] Found %d raw signals", len(signals))

    # Deduplicate overlapping signals (skip if previous trade not closed)
    # Simple: require minimum spacing between signals
    min_bar_spacing = 10 if execution_tf == "M5" else 5
    filtered_signals: List[Dict[str, Any]] = []
    last_bar_idx = -min_bar_spacing

    for sig in signals:
        if sig["bar_idx"] >= last_bar_idx + min_bar_spacing:
            filtered_signals.append(sig)
            last_bar_idx = sig["bar_idx"]

    logger.info("[BACKTEST] %d signals after dedup (spacing=%d)", len(filtered_signals), min_bar_spacing)

    # Add lot sizing
    risk_amount = initial_balance * (risk_pct / 100.0)
    for sig in filtered_signals:
        stop_dist_pips = abs(sig["entry"] - sig["sl"]) / pip_size
        if stop_dist_pips > 0:
            lots = min(0.02, max(0.01, round(risk_amount / (stop_dist_pips * 10.0), 2)))
        else:
            lots = 0.01
        sig["lots"] = lots

    # Run each scenario
    execution_bars = bars_dict.get(execution_tf, [])
    results: Dict[str, BacktestResult] = {}

    for scenario in scenarios:
        rng = random.Random(seed)

        spread_pips = get_spread_pips(symbol, "stress" if scenario != "base" else "base")

        if scenario == "base":
            slip_model = SlippageModel(base_r=0.1, stress_prob=0.0, shock_prob=0.0)
        elif scenario == "stress":
            slip_model = SlippageModel(base_r=0.1, stress_prob=0.10, shock_prob=0.0)
        else:  # shock
            slip_model = SlippageModel(base_r=0.1, stress_prob=0.10, shock_prob=0.02)

        trades: List[BacktestTrade] = []
        for sig in filtered_signals:
            trade = _simulate_trade(
                sig, execution_bars, spread_pips, slip_model, rng, pip_size, risk_amount,
            )
            trades.append(trade)

        stats = _compute_stats(trades)

        result = BacktestResult(
            engine_slug=engine_slug,
            symbol=symbol,
            scenario=scenario,
            initial_balance=initial_balance,
            risk_pct=risk_pct,
            trades=trades,
            **stats,
        )

        results[scenario] = result
        logger.info("[BACKTEST] %s scenario: %d trades, PF=%.2f, WR=%.1f%%, DD=%.2fR",
                     scenario, len(trades), stats["profit_factor"],
                     stats["win_rate"] * 100, stats["max_drawdown_r"])

    return results


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

def run_monte_carlo(
    trades: List[BacktestTrade],
    n_simulations: int = 1000,
    drop_pct: float = 0.05,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    Monte Carlo simulation over a set of backtest trades.

    1. Shuffle trade sequence (order matters for drawdown)
    2. Random slippage draws per trade
    3. Drop drop_pct% trades (data loss simulation)
    4. Report expectancy distribution, DD distribution, pass threshold

    Parameters
    ----------
    trades : list of BacktestTrade from a backtest run
    n_simulations : number of shuffle iterations
    drop_pct : fraction of trades to randomly drop (0.05 = 5%)
    seed : random seed

    Returns
    -------
    dict with:
        expectancy_dist: list of expectancy values (one per sim)
        dd_dist: list of max drawdown values
        pass_rate: fraction of sims with positive expectancy
        median_expectancy: median expectancy across sims
        p5_expectancy: 5th percentile expectancy
        median_dd: median max drawdown
        p95_dd: 95th percentile drawdown
    """
    rng = random.Random(seed)
    pnl_rs = [t.pnl_r for t in trades]

    if not pnl_rs:
        return {
            "expectancy_dist": [],
            "dd_dist": [],
            "pass_rate": 0.0,
            "median_expectancy": 0.0,
            "p5_expectancy": 0.0,
            "median_dd": 0.0,
            "p95_dd": 0.0,
        }

    expectancy_dist: List[float] = []
    dd_dist: List[float] = []

    n_drop = max(0, int(len(pnl_rs) * drop_pct))

    for _ in range(n_simulations):
        # Shuffle
        shuffled = list(pnl_rs)
        rng.shuffle(shuffled)

        # Drop trades
        if n_drop > 0:
            drop_indices = set(rng.sample(range(len(shuffled)), min(n_drop, len(shuffled))))
            shuffled = [v for i, v in enumerate(shuffled) if i not in drop_indices]

        if not shuffled:
            expectancy_dist.append(0.0)
            dd_dist.append(0.0)
            continue

        # Random slippage perturbation (±0.1R per trade)
        perturbed = [v + rng.uniform(-0.1, 0.05) for v in shuffled]

        # Compute expectancy
        exp = sum(perturbed) / len(perturbed)
        expectancy_dist.append(exp)

        # Compute max drawdown
        peak = 0.0
        max_dd = 0.0
        running = 0.0
        for v in perturbed:
            running += v
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd
        dd_dist.append(max_dd)

    # Compute statistics
    expectancy_dist.sort()
    dd_dist.sort()

    pass_count = sum(1 for e in expectancy_dist if e > 0)

    return {
        "n_simulations": n_simulations,
        "n_trades": len(pnl_rs),
        "drop_pct": drop_pct,
        "expectancy_dist": expectancy_dist,
        "dd_dist": dd_dist,
        "pass_rate": pass_count / n_simulations if n_simulations > 0 else 0.0,
        "median_expectancy": expectancy_dist[len(expectancy_dist) // 2] if expectancy_dist else 0.0,
        "p5_expectancy": expectancy_dist[int(len(expectancy_dist) * 0.05)] if expectancy_dist else 0.0,
        "median_dd": dd_dist[len(dd_dist) // 2] if dd_dist else 0.0,
        "p95_dd": dd_dist[int(len(dd_dist) * 0.95)] if dd_dist else 0.0,
    }


# ---------------------------------------------------------------------------
# Acceptance check
# ---------------------------------------------------------------------------

def check_acceptance(
    results: Dict[str, BacktestResult],
    mc_result: Dict[str, Any],
    min_profit_factor: float = 1.2,
    max_dd_stress: float = 10.0,
    min_mc_pass_rate: float = 0.70,
) -> Dict[str, Any]:
    """
    Run acceptance checks against backtest and Monte Carlo results.

    Returns dict with pass/fail for each check and overall verdict.
    """
    checks: Dict[str, Any] = {}

    # PF check (base scenario)
    base = results.get("base")
    if base:
        checks["profit_factor_base"] = {
            "value": base.profit_factor,
            "threshold": min_profit_factor,
            "pass": base.profit_factor >= min_profit_factor,
        }
    else:
        checks["profit_factor_base"] = {"pass": False, "reason": "no_base_scenario"}

    # DD check (stress scenario)
    stress = results.get("stress")
    if stress:
        checks["max_dd_stress"] = {
            "value": stress.max_drawdown_r,
            "threshold": max_dd_stress,
            "pass": stress.max_drawdown_r <= max_dd_stress,
        }
    else:
        checks["max_dd_stress"] = {"pass": False, "reason": "no_stress_scenario"}

    # MC pass rate
    checks["mc_pass_rate"] = {
        "value": mc_result.get("pass_rate", 0),
        "threshold": min_mc_pass_rate,
        "pass": mc_result.get("pass_rate", 0) >= min_mc_pass_rate,
    }

    # MC expectancy positive at p5
    checks["mc_p5_positive"] = {
        "value": mc_result.get("p5_expectancy", 0),
        "pass": mc_result.get("p5_expectancy", 0) > 0,
    }

    # Overall
    all_pass = all(c.get("pass", False) for c in checks.values())
    checks["overall"] = all_pass

    return checks
