"""
GuvFX Markov Regime Classification Engine

Deterministic volatility-adjusted regime classifier.  Labels each bar
as BULL, BEAR, or SIDEWAYS based on rolling return vs rolling volatility.

Computes Markov transition matrix from sequential regime labels.

Research Mode only — no live execution, no strategy modification.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from backtests.engine import Bar


# ─────────────────────────────────────────────────────────────────────
# Regime labels
# ─────────────────────────────────────────────────────────────────────

BULL = "BULL"
BEAR = "BEAR"
SIDEWAYS = "SIDEWAYS"
REGIMES = [BULL, SIDEWAYS, BEAR]


# ─────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────

@dataclass
class RegimeParams:
    lookback: int = 20
    k: float = 1.0  # volatility multiplier threshold


@dataclass
class TransitionMatrix:
    """Markov transition probabilities between regimes."""
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    probabilities: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "probabilities": {
                src: {dst: round(p, 3) for dst, p in dsts.items()}
                for src, dsts in self.probabilities.items()
            },
        }


@dataclass
class RegimeAnalysis:
    """Complete regime analysis result."""
    labels: list[str]  # regime label per bar
    current_regime: str
    regime_counts: dict[str, int]
    regime_pct: dict[str, float]
    transition_matrix: TransitionMatrix
    persistence: dict[str, float]  # probability of staying in same regime
    params: RegimeParams
    bars_analysed: int
    error: str = ""


@dataclass
class RegimeMetrics:
    """Performance metrics grouped by regime."""
    by_regime: dict[str, dict] = field(default_factory=dict)
    # Each regime maps to: trades, net_profit, win_rate, profit_factor


# ─────────────────────────────────────────────────────────────────────
# Regime classification
# ─────────────────────────────────────────────────────────────────────

def classify_regimes(
    bars: list[Bar],
    params: RegimeParams | None = None,
) -> RegimeAnalysis:
    """
    Classify each bar into BULL, BEAR, or SIDEWAYS.

    Method: Volatility-adjusted return (Option B)
    - Rolling return = (close[i] - close[i-lookback]) / close[i-lookback]
    - Rolling volatility = std(log returns) over lookback window
    - BULL if return >= k * volatility
    - BEAR if return <= -k * volatility
    - SIDEWAYS otherwise
    """
    if params is None:
        params = RegimeParams()

    n = len(bars)
    lookback = params.lookback
    k = params.k

    if n < lookback + 5:
        return RegimeAnalysis(
            labels=[], current_regime=SIDEWAYS,
            regime_counts={}, regime_pct={},
            transition_matrix=TransitionMatrix(),
            persistence={}, params=params, bars_analysed=n,
            error=f"Not enough bars ({n}) for lookback {lookback}",
        )

    closes = [b.close for b in bars]
    labels: list[str] = [SIDEWAYS] * n  # default

    for i in range(lookback, n):
        # Rolling return
        ret = (closes[i] - closes[i - lookback]) / closes[i - lookback]

        # Rolling log-return volatility
        log_returns = []
        for j in range(i - lookback + 1, i + 1):
            if closes[j - 1] > 0:
                log_returns.append(math.log(closes[j] / closes[j - 1]))

        if len(log_returns) < 2:
            continue

        mean_lr = sum(log_returns) / len(log_returns)
        variance = sum((lr - mean_lr) ** 2 for lr in log_returns) / (len(log_returns) - 1)
        vol = math.sqrt(variance) if variance > 0 else 0.0001

        # Annualised-ish threshold (scale vol to match return period)
        threshold = k * vol * math.sqrt(lookback)

        if ret >= threshold:
            labels[i] = BULL
        elif ret <= -threshold:
            labels[i] = BEAR
        else:
            labels[i] = SIDEWAYS

    # ── Counts ──
    regime_counts = {r: 0 for r in REGIMES}
    for lbl in labels[lookback:]:
        regime_counts[lbl] = regime_counts.get(lbl, 0) + 1

    total_labelled = sum(regime_counts.values())
    regime_pct = {
        r: round(c / total_labelled * 100, 1) if total_labelled > 0 else 0
        for r, c in regime_counts.items()
    }

    # ── Transition matrix ──
    tm = _compute_transition_matrix(labels[lookback:])

    # ── Persistence ──
    persistence = {}
    for r in REGIMES:
        persistence[r] = round(tm.probabilities.get(r, {}).get(r, 0), 3)

    return RegimeAnalysis(
        labels=labels,
        current_regime=labels[-1] if labels else SIDEWAYS,
        regime_counts=regime_counts,
        regime_pct=regime_pct,
        transition_matrix=tm,
        persistence=persistence,
        params=params,
        bars_analysed=n,
    )


def _compute_transition_matrix(labels: list[str]) -> TransitionMatrix:
    """Compute Markov transition counts and probabilities."""
    counts: dict[str, dict[str, int]] = {
        r: {r2: 0 for r2 in REGIMES} for r in REGIMES
    }

    for i in range(1, len(labels)):
        src = labels[i - 1]
        dst = labels[i]
        if src in counts and dst in counts[src]:
            counts[src][dst] += 1

    # Normalise to probabilities
    probs: dict[str, dict[str, float]] = {}
    for src, dsts in counts.items():
        total = sum(dsts.values())
        if total > 0:
            probs[src] = {dst: c / total for dst, c in dsts.items()}
        else:
            probs[src] = {dst: 0.0 for dst in REGIMES}

    return TransitionMatrix(counts=counts, probabilities=probs)


# ─────────────────────────────────────────────────────────────────────
# Per-regime trade metrics
# ─────────────────────────────────────────────────────────────────────

def compute_regime_metrics(
    trades: list,  # BacktestTrade objects
    regime_labels: list[str],
    bars: list[Bar],
) -> RegimeMetrics:
    """
    Compute performance metrics grouped by the regime at trade entry.

    trades: list of BacktestTrade (with entry_bar index)
    regime_labels: regime label per bar (from classify_regimes)
    """
    by_regime: dict[str, list[float]] = {r: [] for r in REGIMES}

    for trade in trades:
        bar_idx = getattr(trade, "entry_bar", 0)
        if 0 <= bar_idx < len(regime_labels):
            regime = regime_labels[bar_idx]
        else:
            regime = SIDEWAYS
        by_regime[regime].append(trade.pnl)

    result: dict[str, dict] = {}
    for regime, pnls in by_regime.items():
        if not pnls:
            result[regime] = {
                "trades": 0, "net_profit": 0, "win_rate": 0, "profit_factor": 0,
            }
            continue

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))

        result[regime] = {
            "trades": len(pnls),
            "net_profit": round(sum(pnls), 2),
            "win_rate": round(len(wins) / len(pnls) * 100, 1),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else (
                999.99 if gross_profit > 0 else 0
            ),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
        }

    return RegimeMetrics(by_regime=result)


# ─────────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────────

def analysis_to_dict(analysis: RegimeAnalysis) -> dict:
    """Convert RegimeAnalysis to JSON-serialisable dict."""
    return {
        "current_regime": analysis.current_regime,
        "regime_counts": analysis.regime_counts,
        "regime_pct": analysis.regime_pct,
        "transition_matrix": analysis.transition_matrix.to_dict(),
        "persistence": analysis.persistence,
        "params": {"lookback": analysis.params.lookback, "k": analysis.params.k},
        "bars_analysed": analysis.bars_analysed,
        "error": analysis.error,
    }
