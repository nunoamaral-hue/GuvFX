"""
GuvFX Multi-Symbol Research Matrix

Evaluates strategy templates across multiple symbols and timeframes.
Produces ranked results with research scores.

Research Mode only — no live execution.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from backtests.engine import Bar, fetch_bars, run_template_backtest
from backtests.regime_engine import classify_regimes, RegimeParams

logger = logging.getLogger(__name__)

MAX_COMBINATIONS = 200

# ─────────────────────────────────────────────────────────────────
# Asset classification
# ─────────────────────────────────────────────────────────────────

ASSET_CLASS = {
    "EURUSD": "FX Major", "GBPUSD": "FX Major", "USDJPY": "FX Major",
    "USDCHF": "FX Major", "USDCAD": "FX Major", "AUDUSD": "FX Major",
    "NZDUSD": "FX Major",
    "XAUUSD": "Metal", "XAGUSD": "Metal",
    ".US30Cash": "Index", ".USTECHCash": "Index", ".US500Cash": "Index",
    ".DE30Cash": "Index", ".UK100Cash": "Index",
    "BTCUSD": "Crypto", "ETHUSD": "Crypto", "SOLUSD": "Crypto",
    "XRPUSD": "Crypto",
    ".WTICrude": "Energy", ".BrentCrude": "Energy",
    "EURGBP": "FX Minor", "EURJPY": "FX Minor", "GBPJPY": "FX Minor",
}

TIER1_SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "XAUUSD", "XAGUSD",
    ".US30Cash", ".USTECHCash", ".US500Cash",
    "BTCUSD", "ETHUSD",
    ".WTICrude", ".DE30Cash",
]

ALL_TEMPLATES = ["ema_trend", "rsi_mean_reversion", "atr_breakout", "london_breakout"]


# ─────────────────────────────────────────────────────────────────
# Pip configuration per asset class
# ─────────────────────────────────────────────────────────────────

def get_pip_config(symbol: str) -> dict:
    """Return pip_value and spread_pips for a symbol."""
    s = symbol.upper()
    if s.startswith(".US") or s.startswith(".DE") or s.startswith(".UK") or s.startswith(".AUS") or s.startswith(".JP"):
        return {"pip_value": 1.0, "spread_pips": 3.0}  # index: $1/point/lot
    if "XAU" in s:
        return {"pip_value": 1.0, "spread_pips": 3.5}  # gold: $1/point
    if "XAG" in s:
        return {"pip_value": 0.5, "spread_pips": 15.0}
    if "BTC" in s:
        return {"pip_value": 0.01, "spread_pips": 90.0}  # BTC: $0.01/point
    if "ETH" in s:
        return {"pip_value": 0.01, "spread_pips": 16.0}
    if "SOL" in s or "XRP" in s:
        return {"pip_value": 0.01, "spread_pips": 10.0}
    if "CRUDE" in s.upper() or "WTI" in s.upper() or "BRENT" in s.upper():
        return {"pip_value": 1.0, "spread_pips": 1.5}
    if "JPY" in s:
        return {"pip_value": 7.0, "spread_pips": 1.0}  # JPY pairs
    return {"pip_value": 10.0, "spread_pips": 1.5}  # default FX


# ─────────────────────────────────────────────────────────────────
# Research Score (0-100)
# ─────────────────────────────────────────────────────────────────

def compute_research_score(metrics: dict) -> tuple[int, str]:
    """
    Compute a research score 0-100 from backtest metrics.

    Components:
    - Profit factor (0-35 pts): PF 1.0=15, 1.5=25, 2.0+=35
    - Net profit direction (0-15 pts): positive=15, negative=0
    - Drawdown penalty (0-20 pts): <1%=20, <5%=15, <10%=10, >10%=5
    - Trade count confidence (0-15 pts): >20=15, >10=10, >5=5
    - Data quality (0-15 pts): bars>500=15, >200=10, >50=5
    """
    pf = metrics.get("profit_factor", 0)
    net = metrics.get("net_profit", 0)
    dd = metrics.get("max_drawdown", 100)
    trades = metrics.get("total_trades", 0)
    bars = metrics.get("bars_count", 0) if "bars_count" in metrics else 500

    # PF score (0-35)
    if pf >= 2.0:
        pf_score = 35
    elif pf >= 1.5:
        pf_score = 25
    elif pf >= 1.2:
        pf_score = 20
    elif pf >= 1.0:
        pf_score = 15
    elif pf >= 0.8:
        pf_score = 8
    else:
        pf_score = 0

    # Net profit (0-15)
    net_score = 15 if net > 0 else 5 if net > -5 else 0

    # Drawdown (0-20)
    if dd < 1:
        dd_score = 20
    elif dd < 5:
        dd_score = 15
    elif dd < 10:
        dd_score = 10
    else:
        dd_score = 5

    # Trade confidence (0-15)
    if trades >= 20:
        trade_score = 15
    elif trades >= 10:
        trade_score = 10
    elif trades >= 5:
        trade_score = 5
    else:
        trade_score = 0

    # Data quality (0-15)
    data_score = 15  # default since we verified Grade A

    total = pf_score + net_score + dd_score + trade_score + data_score
    total = max(0, min(100, total))

    if total >= 80:
        label = "STRONG"
    elif total >= 65:
        label = "PROMISING"
    elif total >= 50:
        label = "WATCHLIST"
    else:
        label = "WEAK"

    return total, label


# ─────────────────────────────────────────────────────────────────
# Matrix row
# ─────────────────────────────────────────────────────────────────

@dataclass
class MatrixRow:
    symbol: str
    asset_class: str
    timeframe: str
    template: str
    bar_count: int = 0
    current_regime: str = ""
    trades: int = 0
    net_profit: float = 0
    profit_factor: float = 0
    win_rate: float = 0
    max_drawdown: float = 0
    expectancy: float = 0
    research_score: int = 0
    robustness_label: str = ""
    error: str = ""


@dataclass
class MatrixResult:
    rows: list[MatrixRow] = field(default_factory=list)
    combinations_requested: int = 0
    combinations_completed: int = 0
    combinations_failed: int = 0
    runtime_seconds: float = 0
    warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────
# Matrix runner
# ─────────────────────────────────────────────────────────────────

def run_research_matrix(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    templates: list[str] | None = None,
    bar_count: int = 1000,
    max_combinations: int = MAX_COMBINATIONS,
) -> MatrixResult:
    """Run Research Matrix across symbols × templates × timeframes."""
    symbols = symbols or TIER1_SYMBOLS
    timeframes = timeframes or ["H1"]
    templates = templates or ALL_TEMPLATES

    total = len(symbols) * len(templates) * len(timeframes)
    if total > max_combinations:
        return MatrixResult(
            combinations_requested=total,
            warnings=[f"Too many combinations ({total}). Max {max_combinations}."],
        )

    result = MatrixResult(combinations_requested=total)
    start = time.time()

    # Cache bars per symbol+timeframe
    bars_cache: dict[str, list[Bar]] = {}

    for tf in timeframes:
        for sym in symbols:
            cache_key = f"{sym}_{tf}"
            if cache_key not in bars_cache:
                try:
                    bars_cache[cache_key] = fetch_bars(sym, tf, count=bar_count)
                except Exception as e:
                    logger.warning(f"Matrix: failed to fetch {sym} {tf}: {e}")
                    bars_cache[cache_key] = []

            bars = bars_cache[cache_key]
            if not bars:
                for tmpl in templates:
                    result.rows.append(MatrixRow(
                        symbol=sym, asset_class=ASSET_CLASS.get(sym, "Other"),
                        timeframe=tf, template=tmpl, error=f"No data for {sym} {tf}",
                    ))
                    result.combinations_failed += 1
                continue

            # Regime for this symbol
            regime = ""
            try:
                ra = classify_regimes(bars, RegimeParams(lookback=20, k=1.0))
                regime = ra.current_regime
            except Exception:
                pass

            pip_cfg = get_pip_config(sym)

            for tmpl in templates:
                try:
                    bt = run_template_backtest(
                        bars, tmpl, symbol=sym, timeframe=tf,
                        lots=0.01,
                        spread_pips=pip_cfg["spread_pips"],
                        pip_value=pip_cfg["pip_value"],
                    )

                    m = bt.metrics
                    score, label = compute_research_score(m)

                    result.rows.append(MatrixRow(
                        symbol=sym, asset_class=ASSET_CLASS.get(sym, "Other"),
                        timeframe=tf, template=tmpl,
                        bar_count=bt.bars_count,
                        current_regime=regime,
                        trades=m.get("total_trades", 0),
                        net_profit=m.get("net_profit", 0),
                        profit_factor=m.get("profit_factor", 0),
                        win_rate=m.get("win_rate", 0),
                        max_drawdown=m.get("max_drawdown", 0),
                        expectancy=m.get("expectancy", 0),
                        research_score=score,
                        robustness_label=label,
                    ))
                    result.combinations_completed += 1

                except Exception as e:
                    result.rows.append(MatrixRow(
                        symbol=sym, asset_class=ASSET_CLASS.get(sym, "Other"),
                        timeframe=tf, template=tmpl, error=str(e)[:100],
                    ))
                    result.combinations_failed += 1

    result.runtime_seconds = round(time.time() - start, 1)
    return result


# ─────────────────────────────────────────────────────────────────
# Rankings
# ─────────────────────────────────────────────────────────────────

def compute_rankings(result: MatrixResult) -> dict:
    """Compute summary rankings from matrix result."""
    valid = [r for r in result.rows if not r.error and r.trades > 0]

    # Top 10 by score
    top10 = sorted(valid, key=lambda r: r.research_score, reverse=True)[:10]

    # Bottom 5
    bottom5 = sorted(valid, key=lambda r: r.research_score)[:5]

    # Average score by symbol
    by_symbol: dict[str, list[int]] = {}
    for r in valid:
        by_symbol.setdefault(r.symbol, []).append(r.research_score)
    avg_symbol = sorted(
        [{"symbol": s, "avg_score": round(sum(scores) / len(scores), 1), "count": len(scores)}
         for s, scores in by_symbol.items()],
        key=lambda x: x["avg_score"], reverse=True,
    )

    # Average score by template
    by_template: dict[str, list[int]] = {}
    for r in valid:
        by_template.setdefault(r.template, []).append(r.research_score)
    avg_template = sorted(
        [{"template": t, "avg_score": round(sum(scores) / len(scores), 1), "count": len(scores)}
         for t, scores in by_template.items()],
        key=lambda x: x["avg_score"], reverse=True,
    )

    return {
        "top_10": [_row_to_dict(r, i + 1) for i, r in enumerate(top10)],
        "bottom_5": [_row_to_dict(r, i + 1) for i, r in enumerate(bottom5)],
        "by_symbol": avg_symbol,
        "by_template": avg_template,
        "total_valid": len(valid),
        "total_failed": result.combinations_failed,
    }


def _row_to_dict(row: MatrixRow, rank: int = 0) -> dict:
    return {
        "rank": rank,
        "symbol": row.symbol,
        "asset_class": row.asset_class,
        "timeframe": row.timeframe,
        "template": row.template,
        "trades": row.trades,
        "net_profit": row.net_profit,
        "profit_factor": row.profit_factor,
        "win_rate": row.win_rate,
        "max_drawdown": row.max_drawdown,
        "research_score": row.research_score,
        "robustness_label": row.robustness_label,
        "current_regime": row.current_regime,
    }


def matrix_to_dict(result: MatrixResult) -> dict:
    rankings = compute_rankings(result)
    return {
        "combinations_requested": result.combinations_requested,
        "combinations_completed": result.combinations_completed,
        "combinations_failed": result.combinations_failed,
        "runtime_seconds": result.runtime_seconds,
        "warnings": result.warnings,
        "rankings": rankings,
        "all_rows": [_row_to_dict(r) for r in result.rows if not r.error],
        "failed_rows": [{"symbol": r.symbol, "template": r.template, "error": r.error}
                        for r in result.rows if r.error],
        "mode": "research",
        "mode_label": "Research Mode Multi-Symbol Matrix",
    }
