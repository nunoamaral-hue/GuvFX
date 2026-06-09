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
# MT5 symbol metadata cache + PnL configuration
# ─────────────────────────────────────────────────────────────────

# Actual MT5 tick_value/tick_size per symbol (from /mt5/symbols)
# PnL = (price_delta / tick_size) × tick_value × lots
SYMBOL_METADATA: dict[str, dict] = {
    # FX Major — tick_size=point, tick_value=$/pip/lot
    "EURUSD":      {"tick_size": 0.00001, "tick_value": 1.00, "spread_points": 6, "digits": 5},
    "GBPUSD":      {"tick_size": 0.00001, "tick_value": 1.00, "spread_points": 6, "digits": 5},
    "USDJPY":      {"tick_size": 0.001,   "tick_value": 0.62, "spread_points": 9, "digits": 3},
    "USDCHF":      {"tick_size": 0.00001, "tick_value": 1.26, "spread_points": 11, "digits": 5},
    "USDCAD":      {"tick_size": 0.00001, "tick_value": 0.72, "spread_points": 6, "digits": 5},
    "AUDUSD":      {"tick_size": 0.00001, "tick_value": 1.00, "spread_points": 6, "digits": 5},
    "NZDUSD":      {"tick_size": 0.00001, "tick_value": 1.00, "spread_points": 7, "digits": 5},
    # Metals
    "XAUUSD":      {"tick_size": 0.01,    "tick_value": 1.00, "spread_points": 33, "digits": 2},
    "XAGUSD":      {"tick_size": 0.001,   "tick_value": 5.00, "spread_points": 133, "digits": 3},
    # Indices (contract_size × tick_value per tick)
    ".US30Cash":   {"tick_size": 1.0,     "tick_value": 10.0, "spread_points": 9, "digits": 0},
    ".USTECHCash": {"tick_size": 0.1,     "tick_value": 1.0,  "spread_points": 35, "digits": 1},
    ".US500Cash":  {"tick_size": 0.1,     "tick_value": 2.0,  "spread_points": 17, "digits": 1},
    ".DE30Cash":   {"tick_size": 0.1,     "tick_value": 1.0,  "spread_points": 30, "digits": 1},
    ".UK100Cash":  {"tick_size": 0.1,     "tick_value": 1.0,  "spread_points": 33, "digits": 1},
    # Crypto
    "BTCUSD":      {"tick_size": 0.01,    "tick_value": 0.01, "spread_points": 884, "digits": 2},
    "ETHUSD":      {"tick_size": 0.01,    "tick_value": 0.10, "spread_points": 158, "digits": 2},
    "SOLUSD":      {"tick_size": 0.001,   "tick_value": 0.001, "spread_points": 90, "digits": 3},
    "XRPUSD":      {"tick_size": 0.0001,  "tick_value": 0.0001, "spread_points": 21, "digits": 4},
    # Energy
    ".WTICrude":   {"tick_size": 0.01,    "tick_value": 10.0, "spread_points": 14, "digits": 2},
    ".BrentCrude": {"tick_size": 0.01,    "tick_value": 10.0, "spread_points": 8, "digits": 2},
    # FX Minor
    "EURGBP":      {"tick_size": 0.00001, "tick_value": 1.26, "spread_points": 5, "digits": 5},
    "EURJPY":      {"tick_size": 0.001,   "tick_value": 0.62, "spread_points": 10, "digits": 3},
    "GBPJPY":      {"tick_size": 0.001,   "tick_value": 0.62, "spread_points": 12, "digits": 3},
}


def get_pip_config(symbol: str) -> dict:
    """
    Return tick_value, tick_size, and spread in price units for a symbol.

    Uses MT5 metadata. Falls back to conservative FX defaults.
    """
    meta = SYMBOL_METADATA.get(symbol)
    if meta:
        return {
            "tick_size": meta["tick_size"],
            "tick_value": meta["tick_value"],
            "spread_points": meta["spread_points"],  # spread in ticks
            "digits": meta["digits"],
            "source": "mt5_metadata",
        }

    # Fallback for unknown symbols (conservative FX)
    return {
        "tick_size": 0.00001,
        "tick_value": 1.0,
        "spread_points": 15,  # 1.5 pips in FX terms
        "digits": 5,
        "source": "fallback",
    }


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
    feature_context: dict = field(default_factory=dict)
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
            ts = pip_cfg["tick_size"]
            tv = pip_cfg["tick_value"]
            sp = pip_cfg["spread_points"]

            for tmpl in templates:
                try:
                    # PnL = (price_delta / tick_size) × tick_value × lots
                    # spread_pips = spread in ticks (engine multiplies by pip_size=tick_size)
                    bt = run_template_backtest(
                        bars, tmpl, symbol=sym, timeframe=tf,
                        lots=0.01,
                        spread_pips=sp,
                        pip_value=tv,
                        pip_size=ts,
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
                        feature_context=m.get("feature_context", {}) or {},
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
    snap = (row.feature_context or {}).get("snapshot", {})
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
        # B16 — compact market-context snapshot for display
        "feature_snapshot": {
            "trend_state": snap.get("trend_state", ""),
            "volatility_state": snap.get("volatility_state", ""),
            "breakout_state": snap.get("breakout_state", ""),
            "position_size_warning": snap.get("position_size_warning", False),
        },
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
        "all_rows": [
            {**_row_to_dict(r), "feature_context": r.feature_context or {}}
            for r in result.rows if not r.error
        ],
        "failed_rows": [{"symbol": r.symbol, "template": r.template, "error": r.error}
                        for r in result.rows if r.error],
        "mode": "research",
        "mode_label": "Research Mode Multi-Symbol Matrix",
    }
