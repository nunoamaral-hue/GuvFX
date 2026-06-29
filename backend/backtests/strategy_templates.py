"""
GuvFX Research Mode Strategy Templates

Pluggable strategy templates for the backtesting engine.
Each template defines indicators, entry/exit logic, and default parameters.

No live execution.  Pure simulation only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from backtests.engine import Bar


# ─────────────────────────────────────────────────────────────────────
# Signal output
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    action: str = "none"  # "buy", "sell", "close", "none"
    sl: float = 0.0
    tp: float = 0.0
    reason: str = ""


# ─────────────────────────────────────────────────────────────────────
# Base template
# ─────────────────────────────────────────────────────────────────────

class StrategyTemplate(ABC):
    name: str = "base"
    description: str = ""
    version: str = "1.0"
    supported_timeframes: list[str] = []

    @abstractmethod
    def default_params(self) -> dict:
        """Return default parameter dict."""
        ...

    @abstractmethod
    def prepare(self, bars: list[Bar], params: dict) -> dict:
        """Compute indicators. Returns dict of indicator arrays."""
        ...

    @abstractmethod
    def signal(self, i: int, bars: list[Bar], indicators: dict, params: dict,
               position: Optional[dict]) -> Signal:
        """Generate signal for bar index i."""
        ...

    def min_bars(self, params: dict) -> int:
        """Minimum bars before strategy can generate signals."""
        return 50


# ─────────────────────────────────────────────────────────────────────
# Indicator helpers
# ─────────────────────────────────────────────────────────────────────

def ema(closes: list[float], period: int) -> list[Optional[float]]:
    """Exponential Moving Average."""
    result: list[Optional[float]] = []
    mult = 2.0 / (period + 1)
    for i, price in enumerate(closes):
        if i < period - 1:
            result.append(None)
        elif i == period - 1:
            result.append(sum(closes[:period]) / period)
        else:
            prev = result[-1]
            result.append((price - prev) * mult + prev if prev is not None else None)
    return result


def sma(values: list[float], period: int) -> list[Optional[float]]:
    """Simple Moving Average."""
    result: list[Optional[float]] = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1:i + 1]) / period)
    return result


def rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Relative Strength Index."""
    result: list[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return result

    gains = []
    losses = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result


def atr(bars: list[Bar], period: int = 14) -> list[Optional[float]]:
    """Average True Range."""
    result: list[Optional[float]] = [None] * len(bars)
    if len(bars) < period + 1:
        return result

    trs = []
    for i in range(1, len(bars)):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        trs.append(tr)

    if len(trs) < period:
        return result

    # First ATR is SMA of first `period` true ranges
    first_atr = sum(trs[:period]) / period
    result[period] = first_atr

    prev_atr = first_atr
    for i in range(period, len(trs)):
        current_atr = (prev_atr * (period - 1) + trs[i]) / period
        result[i + 1] = current_atr
        prev_atr = current_atr

    return result


# ─────────────────────────────────────────────────────────────────────
# Template 1: EMA Trend Crossover
# ─────────────────────────────────────────────────────────────────────

class EmaTrendTemplate(StrategyTemplate):
    name = "ema_trend"
    description = "EMA crossover trend following. Buy when fast > slow, sell when fast < slow."
    version = "1.0"
    supported_timeframes = ["M15", "M30", "H1", "H4", "D1"]

    def default_params(self) -> dict:
        return {"fast_ema": 20, "slow_ema": 50, "sl_pips": 30, "tp_pips": 60}

    def min_bars(self, params: dict) -> int:
        return max(params.get("fast_ema", 20), params.get("slow_ema", 50)) + 2

    def prepare(self, bars: list[Bar], params: dict) -> dict:
        closes = [b.close for b in bars]
        return {
            "fast": ema(closes, params.get("fast_ema", 20)),
            "slow": ema(closes, params.get("slow_ema", 50)),
        }

    def signal(self, i: int, bars: list[Bar], ind: dict, params: dict,
               position: Optional[dict]) -> Signal:
        if i < 1:
            return Signal()
        f, s = ind["fast"], ind["slow"]
        if any(v is None for v in [f[i], f[i-1], s[i], s[i-1]]):
            return Signal()

        pip = 0.0001 if "JPY" not in "" else 0.01  # simplified
        sl_dist = params.get("sl_pips", 30) * pip
        tp_dist = params.get("tp_pips", 60) * pip

        # Close opposite position on signal
        if position:
            if position["side"] == "BUY" and f[i] < s[i] and f[i-1] >= s[i-1]:
                return Signal("close", reason="ema_cross_down")
            if position["side"] == "SELL" and f[i] > s[i] and f[i-1] <= s[i-1]:
                return Signal("close", reason="ema_cross_up")
            return Signal()

        # Entry
        if f[i] > s[i] and f[i-1] <= s[i-1]:
            price = bars[i].close
            return Signal("buy", sl=price - sl_dist, tp=price + tp_dist, reason="ema_cross_up")
        if f[i] < s[i] and f[i-1] >= s[i-1]:
            price = bars[i].close
            return Signal("sell", sl=price + sl_dist, tp=price - tp_dist, reason="ema_cross_down")

        return Signal()


# ─────────────────────────────────────────────────────────────────────
# Template 2: RSI Mean Reversion
# ─────────────────────────────────────────────────────────────────────

class RsiMeanReversionTemplate(StrategyTemplate):
    name = "rsi_mean_reversion"
    description = "RSI oversold/overbought mean reversion. Buy at RSI<30, sell at RSI>70."
    version = "1.0"
    supported_timeframes = ["M15", "M30", "H1", "H4"]

    def default_params(self) -> dict:
        return {"rsi_period": 14, "oversold": 30, "overbought": 70,
                "sl_pips": 25, "tp_pips": 40}

    def min_bars(self, params: dict) -> int:
        return params.get("rsi_period", 14) + 5

    def prepare(self, bars: list[Bar], params: dict) -> dict:
        closes = [b.close for b in bars]
        return {"rsi": rsi(closes, params.get("rsi_period", 14))}

    def signal(self, i: int, bars: list[Bar], ind: dict, params: dict,
               position: Optional[dict]) -> Signal:
        r = ind["rsi"][i]
        if r is None:
            return Signal()

        pip = 0.0001
        sl_dist = params.get("sl_pips", 25) * pip
        tp_dist = params.get("tp_pips", 40) * pip
        price = bars[i].close

        # Exit at opposite extreme
        if position:
            if position["side"] == "BUY" and r >= params.get("overbought", 70):
                return Signal("close", reason="rsi_overbought_exit")
            if position["side"] == "SELL" and r <= params.get("oversold", 30):
                return Signal("close", reason="rsi_oversold_exit")
            return Signal()

        # Entry
        if r <= params.get("oversold", 30):
            return Signal("buy", sl=price - sl_dist, tp=price + tp_dist, reason="rsi_oversold")
        if r >= params.get("overbought", 70):
            return Signal("sell", sl=price + sl_dist, tp=price - tp_dist, reason="rsi_overbought")

        return Signal()


# ─────────────────────────────────────────────────────────────────────
# Template 3: ATR Breakout
# ─────────────────────────────────────────────────────────────────────

class AtrBreakoutTemplate(StrategyTemplate):
    name = "atr_breakout"
    description = "Volatility breakout using ATR bands. Entry on close above/below ATR envelope."
    version = "1.0"
    supported_timeframes = ["H1", "H4", "D1"]

    def default_params(self) -> dict:
        return {"atr_period": 14, "atr_mult": 1.5, "sma_period": 20,
                "sl_atr_mult": 1.0, "tp_atr_mult": 2.0}

    def min_bars(self, params: dict) -> int:
        return max(params.get("atr_period", 14), params.get("sma_period", 20)) + 5

    def prepare(self, bars: list[Bar], params: dict) -> dict:
        closes = [b.close for b in bars]
        return {
            "atr": atr(bars, params.get("atr_period", 14)),
            "sma": sma(closes, params.get("sma_period", 20)),
        }

    def signal(self, i: int, bars: list[Bar], ind: dict, params: dict,
               position: Optional[dict]) -> Signal:
        a, m = ind["atr"][i], ind["sma"][i]
        if a is None or m is None:
            return Signal()

        mult = params.get("atr_mult", 1.5)
        upper = m + a * mult
        lower = m - a * mult
        sl_dist = a * params.get("sl_atr_mult", 1.0)
        tp_dist = a * params.get("tp_atr_mult", 2.0)
        price = bars[i].close

        # Exit on mean reversion
        if position:
            if position["side"] == "BUY" and price < m:
                return Signal("close", reason="mean_reversion")
            if position["side"] == "SELL" and price > m:
                return Signal("close", reason="mean_reversion")
            return Signal()

        # Entry on breakout
        if price > upper and (i == 0 or bars[i-1].close <= upper):
            return Signal("buy", sl=price - sl_dist, tp=price + tp_dist, reason="atr_breakout_up")
        if price < lower and (i == 0 or bars[i-1].close >= lower):
            return Signal("sell", sl=price + sl_dist, tp=price - tp_dist, reason="atr_breakout_down")

        return Signal()


# ─────────────────────────────────────────────────────────────────────
# Template 4: London Session Breakout
# ─────────────────────────────────────────────────────────────────────

class LondonBreakoutTemplate(StrategyTemplate):
    name = "london_breakout"
    description = "Trade the breakout of the Asian session range during London open (07:00-09:00 UTC)."
    version = "1.0"
    supported_timeframes = ["M15", "M30", "H1"]

    def default_params(self) -> dict:
        return {"asian_start_hour": 0, "asian_end_hour": 7,
                "london_start_hour": 7, "london_end_hour": 9,
                "sl_pips": 20, "tp_pips": 40}

    def min_bars(self, params: dict) -> int:
        return 50

    def prepare(self, bars: list[Bar], params: dict) -> dict:
        from datetime import datetime, timezone
        # Pre-compute session ranges
        asian_highs: list[Optional[float]] = [None] * len(bars)
        asian_lows: list[Optional[float]] = [None] * len(bars)

        as_start = params.get("asian_start_hour", 0)
        as_end = params.get("asian_end_hour", 7)

        current_high = 0.0
        current_low = float("inf")
        current_date = None

        for i, bar in enumerate(bars):
            dt = datetime.utcfromtimestamp(bar.time)
            bar_date = dt.date()
            bar_hour = dt.hour

            if bar_date != current_date:
                current_high = 0.0
                current_low = float("inf")
                current_date = bar_date

            if as_start <= bar_hour < as_end:
                current_high = max(current_high, bar.high)
                current_low = min(current_low, bar.low)

            if current_high > 0 and current_low < float("inf"):
                asian_highs[i] = current_high
                asian_lows[i] = current_low

        return {"asian_high": asian_highs, "asian_low": asian_lows}

    def signal(self, i: int, bars: list[Bar], ind: dict, params: dict,
               position: Optional[dict]) -> Signal:
        from datetime import datetime
        dt = datetime.utcfromtimestamp(bars[i].time)
        hour = dt.hour

        lon_start = params.get("london_start_hour", 7)
        lon_end = params.get("london_end_hour", 9)

        ah = ind["asian_high"][i]
        al = ind["asian_low"][i]
        if ah is None or al is None or ah == 0:
            return Signal()

        pip = 0.0001
        sl_dist = params.get("sl_pips", 20) * pip
        tp_dist = params.get("tp_pips", 40) * pip
        price = bars[i].close

        # Close after London session
        if position and hour >= lon_end:
            return Signal("close", reason="session_end")

        # Only trade during London open window
        if not (lon_start <= hour < lon_end):
            return Signal()

        if position:
            return Signal()

        # Breakout entry
        if price > ah:
            return Signal("buy", sl=price - sl_dist, tp=price + tp_dist, reason="london_break_high")
        if price < al:
            return Signal("sell", sl=price + sl_dist, tp=price - tp_dist, reason="london_break_low")

        return Signal()


# ─────────────────────────────────────────────────────────────────────
# Template registry
# ─────────────────────────────────────────────────────────────────────

TEMPLATES: dict[str, StrategyTemplate] = {
    "ema_trend": EmaTrendTemplate(),
    "rsi_mean_reversion": RsiMeanReversionTemplate(),
    "atr_breakout": AtrBreakoutTemplate(),
    "london_breakout": LondonBreakoutTemplate(),
}


def get_template(name: str) -> StrategyTemplate:
    """Get a strategy template by name. Raises KeyError if not found."""
    if name not in TEMPLATES:
        raise KeyError(f"Unknown template: {name}. Available: {list(TEMPLATES.keys())}")
    return TEMPLATES[name]


def list_templates() -> list[dict]:
    """List all available templates with metadata."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "version": t.version,
            "supported_timeframes": t.supported_timeframes,
            "default_params": t.default_params(),
        }
        for t in TEMPLATES.values()
    ]
