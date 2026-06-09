"""
GuvFX Backtesting Engine V1

Deterministic bar-by-bar backtester.  Fetches OHLC data from the
MT5 signal bridge, evaluates a simple EMA-based strategy, simulates
trades with spread/SL/TP, and computes performance metrics.

No live execution.  No ExecutionJob created.  No MT5 orders sent.
All trades exist only in the backtest results.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Bar:
    time: int  # epoch seconds
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0


@dataclass
class BacktestTrade:
    trade_number: int
    entry_bar: int  # index
    exit_bar: int
    entry_time: str  # ISO
    exit_time: str
    side: str  # BUY or SELL
    entry_price: float
    exit_price: float
    sl: float
    tp: float
    lots: float
    pnl: float  # in account currency
    exit_reason: str  # "sl", "tp", "signal_exit", "end_of_data"


@dataclass
class DataQuality:
    bar_count: int = 0
    first_bar_time: str = ""
    last_bar_time: str = ""
    duplicate_bars: int = 0
    data_source: str = "MT5"
    status: str = "OK"  # OK, WARNING, FAIL
    notes: list[str] = field(default_factory=list)


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    bars_count: int
    initial_balance: float
    final_balance: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    data_quality: DataQuality = field(default_factory=DataQuality)
    reconciliation: dict = field(default_factory=dict)
    error: str = ""


# ─────────────────────────────────────────────────────────────────────
# Data fetching from MT5 bridge
# ─────────────────────────────────────────────────────────────────────

TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408, "W1": 32769,
}


def fetch_bars(symbol: str, timeframe: str, count: int = 500) -> list[Bar]:
    """
    Fetch OHLC bars from the MT5 signal bridge with batch support.

    Automatically batches requests for counts > 1000 using start_pos
    offset pagination.  Deduplicates by timestamp.
    """
    base = (
        os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL")
        or os.getenv("WINDOWS_AGENT_BASE")
        or os.getenv("GUVFX_AGENT_URL")
        or ""
    ).rstrip("/")
    token = (
        os.getenv("WINDOWS_AGENT_TOKEN")
        or os.getenv("GUVFX_WINDOWS_AGENT_TOKEN")
        or os.getenv("GUVFX_AGENT_TOKEN")
        or ""
    ).strip()

    if not base or not token:
        raise RuntimeError("Bridge URL/token not configured")

    BATCH_SIZE = 1000
    all_bars: dict[int, Bar] = {}  # keyed by timestamp for dedup
    remaining = count
    start_pos = 0

    while remaining > 0:
        batch = min(remaining, BATCH_SIZE)
        url = f"{base}/mt5/snapshots/rates?symbol={symbol}&timeframe={timeframe}&count={batch}&start_pos={start_pos}"

        req = urllib.request.Request(url, headers={"X-GuvFX-Agent-Token": token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        if not data.get("ok"):
            if all_bars:
                break  # Return what we have
            raise RuntimeError(f"Bridge error: {data}")

        batch_data = data.get("data", [])
        if not batch_data:
            break  # No more data available

        for d in batch_data:
            ts = d["time"]
            if ts not in all_bars:
                all_bars[ts] = Bar(
                    time=ts,
                    open=d["open"],
                    high=d["high"],
                    low=d["low"],
                    close=d["close"],
                    tick_volume=d.get("tick_volume", 0),
                )

        remaining -= batch
        start_pos += batch

        # If bridge returned fewer than requested, we've hit the end
        if len(batch_data) < batch:
            break

    return sorted(all_bars.values(), key=lambda b: b.time)


# ─────────────────────────────────────────────────────────────────────
# EMA calculation
# ─────────────────────────────────────────────────────────────────────

def compute_ema(closes: list[float], period: int) -> list[float | None]:
    """Compute EMA series. Returns None for bars before period is ready."""
    result: list[float | None] = []
    multiplier = 2.0 / (period + 1)

    for i, price in enumerate(closes):
        if i < period - 1:
            result.append(None)
        elif i == period - 1:
            sma = sum(closes[:period]) / period
            result.append(sma)
        else:
            prev = result[-1]
            if prev is None:
                result.append(None)
            else:
                ema = (price - prev) * multiplier + prev
                result.append(ema)

    return result


# ─────────────────────────────────────────────────────────────────────
# Strategy: EMA Trend Pullback V1
# ─────────────────────────────────────────────────────────────────────

@dataclass
class StrategyParams:
    fast_ema: int = 20
    slow_ema: int = 50
    sl_pips: float = 30.0
    tp_pips: float = 60.0
    lots: float = 0.01
    spread_pips: float = 1.5
    pip_value: float = 10.0  # per lot per pip for EURUSD


def run_backtest(
    bars: list[Bar],
    params: StrategyParams,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    initial_balance: float = 10000.0,
) -> BacktestResult:
    """
    Run a deterministic EMA crossover backtest.

    Entry: fast EMA crosses above slow EMA → BUY
           fast EMA crosses below slow EMA → SELL
    Exit:  SL/TP hit, or opposite signal.

    No live execution. Pure simulation.
    """
    if len(bars) < max(params.fast_ema, params.slow_ema) + 5:
        return BacktestResult(
            symbol=symbol, timeframe=timeframe,
            start_date="", end_date="",
            bars_count=len(bars), initial_balance=initial_balance,
            final_balance=initial_balance, error="Not enough bars",
        )

    closes = [b.close for b in bars]
    fast_ema = compute_ema(closes, params.fast_ema)
    slow_ema = compute_ema(closes, params.slow_ema)

    # Determine pip size
    pip_size = 0.0001 if "JPY" not in symbol.upper() else 0.01

    trades: list[BacktestTrade] = []
    equity_curve: list[dict] = []
    balance = initial_balance
    position: Optional[dict] = None  # {side, entry_price, sl, tp, entry_bar, entry_time}
    trade_count = 0

    start_bar = max(params.fast_ema, params.slow_ema) + 1

    for i in range(start_bar, len(bars)):
        bar = bars[i]
        prev_fast = fast_ema[i - 1]
        prev_slow = slow_ema[i - 1]
        curr_fast = fast_ema[i]
        curr_slow = slow_ema[i]

        if any(v is None for v in [prev_fast, prev_slow, curr_fast, curr_slow]):
            continue

        bar_time = datetime.utcfromtimestamp(bar.time).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── Check position exit ──
        if position is not None:
            exit_price = None
            exit_reason = None

            if position["side"] == "BUY":
                # Check SL (hit on low)
                if bar.low <= position["sl"]:
                    exit_price = position["sl"]
                    exit_reason = "sl"
                # Check TP (hit on high)
                elif bar.high >= position["tp"]:
                    exit_price = position["tp"]
                    exit_reason = "tp"
                # Signal exit: fast crosses below slow
                elif curr_fast < curr_slow and prev_fast >= prev_slow:
                    exit_price = bar.close - params.spread_pips * pip_size
                    exit_reason = "signal_exit"
            else:  # SELL
                if bar.high >= position["sl"]:
                    exit_price = position["sl"]
                    exit_reason = "sl"
                elif bar.low <= position["tp"]:
                    exit_price = position["tp"]
                    exit_reason = "tp"
                elif curr_fast > curr_slow and prev_fast <= prev_slow:
                    exit_price = bar.close + params.spread_pips * pip_size
                    exit_reason = "signal_exit"

            if exit_price is not None:
                # Calculate PnL
                if position["side"] == "BUY":
                    pips = (exit_price - position["entry_price"]) / pip_size
                else:
                    pips = (position["entry_price"] - exit_price) / pip_size

                pnl = round(pips * params.pip_value * params.lots, 2)
                balance += pnl
                trade_count += 1

                trades.append(BacktestTrade(
                    trade_number=trade_count,
                    entry_bar=position["entry_bar"],
                    exit_bar=i,
                    entry_time=position["entry_time"],
                    exit_time=bar_time,
                    side=position["side"],
                    entry_price=round(position["entry_price"], 5),
                    exit_price=round(exit_price, 5),
                    sl=round(position["sl"], 5),
                    tp=round(position["tp"], 5),
                    lots=params.lots,
                    pnl=pnl,
                    exit_reason=exit_reason,
                ))
                position = None

        # ── Check for new entry ──
        if position is None:
            # BUY signal: fast crosses above slow
            if curr_fast > curr_slow and prev_fast <= prev_slow:
                entry_price = bar.close + params.spread_pips * pip_size
                position = {
                    "side": "BUY",
                    "entry_price": entry_price,
                    "sl": entry_price - params.sl_pips * pip_size,
                    "tp": entry_price + params.tp_pips * pip_size,
                    "entry_bar": i,
                    "entry_time": bar_time,
                }
            # SELL signal: fast crosses below slow
            elif curr_fast < curr_slow and prev_fast >= prev_slow:
                entry_price = bar.close - params.spread_pips * pip_size
                position = {
                    "side": "SELL",
                    "entry_price": entry_price,
                    "sl": entry_price + params.sl_pips * pip_size,
                    "tp": entry_price - params.tp_pips * pip_size,
                    "entry_bar": i,
                    "entry_time": bar_time,
                }

        # ── Record equity ──
        equity_curve.append({
            "timestamp": bar_time,
            "equity": round(balance, 2),
        })

    # Close any open position at end of data
    if position is not None:
        last_bar = bars[-1]
        if position["side"] == "BUY":
            exit_price = last_bar.close - params.spread_pips * pip_size
            pips = (exit_price - position["entry_price"]) / pip_size
        else:
            exit_price = last_bar.close + params.spread_pips * pip_size
            pips = (position["entry_price"] - exit_price) / pip_size

        pnl = round(pips * params.pip_value * params.lots, 2)
        balance += pnl
        trade_count += 1
        trades.append(BacktestTrade(
            trade_number=trade_count,
            entry_bar=position["entry_bar"],
            exit_bar=len(bars) - 1,
            entry_time=position["entry_time"],
            exit_time=datetime.utcfromtimestamp(last_bar.time).strftime("%Y-%m-%dT%H:%M:%SZ"),
            side=position["side"],
            entry_price=round(position["entry_price"], 5),
            exit_price=round(exit_price, 5),
            sl=round(position["sl"], 5),
            tp=round(position["tp"], 5),
            lots=params.lots,
            pnl=pnl,
            exit_reason="end_of_data",
        ))

    # ── Compute metrics ──
    metrics = compute_metrics(trades, initial_balance, balance)

    start_date = datetime.utcfromtimestamp(bars[0].time).strftime("%Y-%m-%d") if bars else ""
    end_date = datetime.utcfromtimestamp(bars[-1].time).strftime("%Y-%m-%d") if bars else ""

    # ── Data quality assessment ──
    quality = DataQuality(
        bar_count=len(bars),
        first_bar_time=datetime.utcfromtimestamp(bars[0].time).isoformat() + "Z" if bars else "",
        last_bar_time=datetime.utcfromtimestamp(bars[-1].time).isoformat() + "Z" if bars else "",
        data_source="MT5",
        status="OK",
    )

    if len(bars) < 100:
        quality.status = "WARNING"
        quality.notes.append(f"Low bar count ({len(bars)}). Results may not be statistically significant.")
    if len(trades) == 0:
        quality.status = "WARNING"
        quality.notes.append("No trades generated. Strategy may not match this data.")

    # ── Reconciliation metadata ──
    reconciliation = {
        "execution_model": "bar_ohlc",
        "cost_model": "fixed_spread",
        "spread_pips": params.spread_pips,
        "strategy_params": {
            "fast_ema": params.fast_ema,
            "slow_ema": params.slow_ema,
            "sl_pips": params.sl_pips,
            "tp_pips": params.tp_pips,
            "lots": params.lots,
        },
        "pip_value_per_lot": params.pip_value,
        "notes": "Research mode. Bar OHLC simulation with fixed spread. "
                 "May differ from MT5 Strategy Tester or live execution.",
    }

    return BacktestResult(
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        bars_count=len(bars),
        initial_balance=initial_balance,
        final_balance=round(balance, 2),
        trades=trades,
        equity_curve=equity_curve,
        metrics=metrics,
        data_quality=quality,
        reconciliation=reconciliation,
    )


# ─────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────

def compute_metrics(
    trades: list[BacktestTrade],
    initial_balance: float,
    final_balance: float,
) -> dict:
    """Compute performance metrics from trade results."""
    if not trades:
        return {
            "total_trades": 0, "net_profit": 0, "win_rate": 0,
            "profit_factor": 0, "max_drawdown": 0, "expectancy": 0,
        }

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_profit = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0
    avg_win = gross_profit / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0
    expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss) if pnls else 0

    # Max drawdown from equity curve
    peak = initial_balance
    max_dd = 0
    equity = initial_balance
    for t in trades:
        equity += t.pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Max losing streak
    max_losing = 0
    current_losing = 0
    for p in pnls:
        if p <= 0:
            current_losing += 1
            max_losing = max(max_losing, current_losing)
        else:
            current_losing = 0

    return {
        "total_trades": len(pnls),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "net_profit": round(net_profit, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
        "max_drawdown": round(max_dd, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "max_losing_streak": max_losing,
        "initial_balance": initial_balance,
        "final_balance": round(final_balance, 2),
        "total_return_pct": round((final_balance - initial_balance) / initial_balance * 100, 2),
    }
