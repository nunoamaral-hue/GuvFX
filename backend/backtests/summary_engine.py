"""
Packet B — B4: BacktestSummary calculation engine.

Consumes outputs produced by the backtest worker/artifact flow and
computes the approved summary metrics for BacktestSummary.

Approved BacktestSummary schema fields (B1):
    total_trades, win_rate, profit_factor, max_drawdown,
    sharpe_ratio, expectancy

Extended analytical data (PnL series, equity curve) is persisted as
artifact files — not stored in BacktestSummary or PostgreSQL blobs.

Current limitation:
    The backtest worker produces placeholder execution output only
    (no real engine yet).  This summary engine generates a small
    deterministic set of placeholder trades to exercise the full
    calculation pipeline.  When a real backtesting engine is wired
    in, `generate_placeholder_trades()` will be replaced by parsing
    real trade output from execution artifacts.
"""
import json
import logging
import math
from dataclasses import dataclass
from decimal import Decimal

from backtests.artifact_storage import store_artifact
from backtests.models import (
    BacktestArtifact,
    BacktestExecution,
    BacktestSummary,
)

logger = logging.getLogger(__name__)


# ── Trade representation ──


@dataclass(frozen=True)
class Trade:
    """A single trade result used for metric computation."""

    trade_number: int
    pnl: float  # profit/loss in account currency
    is_win: bool


# ── Public entry point ──


def compute_and_store_summary(execution: BacktestExecution) -> BacktestSummary:
    """
    Compute summary metrics for a completed BacktestExecution and
    persist them as a BacktestSummary row + analytical artifacts.

    This is the canonical B4 entry point, called from the worker
    service after artifacts have been written.

    Returns the created BacktestSummary instance.
    """
    job = execution.backtest_job

    # ── Obtain trade data ──
    # B4: placeholder trades.  A future packet will parse real trade
    # output from execution artifacts here.
    trades = _load_trades_from_execution(execution)

    # ── Compute metrics ──
    metrics = _compute_metrics(trades)

    # ── Persist analytical artifacts (PnL series, equity curve) ──
    _persist_analytical_artifacts(execution, job, trades, metrics)

    # ── Create/update BacktestSummary row ──
    summary = _persist_summary(execution, metrics)

    logger.info(
        "Summary computed for execution %s: trades=%d, win_rate=%s, pf=%s, dd=%s",
        execution.run_identifier,
        metrics.total_trades,
        metrics.win_rate,
        metrics.profit_factor,
        metrics.max_drawdown,
    )

    return summary


# ── Metrics data structure ──


@dataclass(frozen=True)
class SummaryMetrics:
    """Computed summary metrics matching the approved BacktestSummary schema."""

    total_trades: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal | None
    sharpe_ratio: Decimal | None
    expectancy: Decimal | None


# ── Trade loading ──


def _load_trades_from_execution(execution: BacktestExecution) -> list[Trade]:
    """
    Load trade results from the execution's produced outputs.

    B4 current state: the worker produces placeholder outputs only,
    so this generates a deterministic set of placeholder trades.
    When a real engine is integrated, this function will parse
    actual trade data from execution artifacts.
    """
    return _generate_placeholder_trades(execution)


def _generate_placeholder_trades(execution: BacktestExecution) -> list[Trade]:
    """
    Generate a small deterministic set of placeholder trades.

    Uses execution.pk as a seed-like value to produce slightly
    varied but reproducible results across different executions.

    This function exists only until a real backtesting engine is
    integrated.  It exercises the full calculation pipeline.
    """
    # Deterministic variation based on execution id
    seed = execution.pk if execution.pk else 1
    num_trades = 20 + (seed % 11)  # 20–30 trades

    trades = []
    for i in range(num_trades):
        # Deterministic win/loss pattern with ~58% win rate
        is_win = ((seed * 7 + i * 13) % 100) < 58

        # Deterministic PnL magnitude
        if is_win:
            pnl = 50.0 + ((seed + i * 3) % 80)  # +50 to +129
        else:
            pnl = -(30.0 + ((seed + i * 5) % 60))  # -30 to -89

        trades.append(Trade(trade_number=i + 1, pnl=round(pnl, 2), is_win=is_win))

    return trades


# ── Metric computation ──


def _compute_metrics(trades: list[Trade]) -> SummaryMetrics:
    """
    Compute the approved summary metrics from a list of trades.

    Calculation basis for each metric:
        total_trades:   len(trades)
        win_rate:       winning_trades / total_trades
        profit_factor:  sum(winning_pnl) / abs(sum(losing_pnl))
        max_drawdown:   largest peak-to-trough decline in cumulative PnL
        sharpe_ratio:   mean(trade_returns) / std(trade_returns) * sqrt(252)
                        (annualized, using 252 trading days)
        expectancy:     mean PnL per trade

    Returns safe nulls for metrics that cannot be computed (e.g.
    zero trades, zero losses for profit_factor).
    """
    if not trades:
        return SummaryMetrics(
            total_trades=0,
            win_rate=None,
            profit_factor=None,
            max_drawdown=None,
            sharpe_ratio=None,
            expectancy=None,
        )

    total = len(trades)
    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if not t.is_win]

    # ── win_rate ──
    win_rate = Decimal(str(round(len(wins) / total, 4)))

    # ── profit_factor ──
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    if gross_loss > 0:
        profit_factor = Decimal(str(round(gross_profit / gross_loss, 4)))
    else:
        profit_factor = None  # No losses — infinite PF, store as null

    # ── max_drawdown ──
    max_drawdown = _compute_max_drawdown(trades)

    # ── sharpe_ratio ──
    sharpe_ratio = _compute_sharpe_ratio(trades)

    # ── expectancy ──
    total_pnl = sum(t.pnl for t in trades)
    expectancy = Decimal(str(round(total_pnl / total, 4)))

    return SummaryMetrics(
        total_trades=total,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        sharpe_ratio=sharpe_ratio,
        expectancy=expectancy,
    )


def _compute_max_drawdown(trades: list[Trade]) -> Decimal | None:
    """
    Compute maximum drawdown as a peak-to-trough decline in
    cumulative PnL, expressed as a positive decimal fraction
    of the peak equity.

    Uses a running-peak approach over the cumulative PnL series.
    Returns None if no drawdown occurred.
    """
    if not trades:
        return None

    # Assume initial equity of 10000 for drawdown fraction calculation
    initial_equity = 10000.0
    equity = initial_equity
    peak = equity
    max_dd = 0.0

    for t in trades:
        equity += t.pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    if max_dd <= 0:
        return None

    return Decimal(str(round(max_dd, 4)))


def _compute_sharpe_ratio(trades: list[Trade]) -> Decimal | None:
    """
    Compute annualized Sharpe ratio.

    Formula: (mean_return / std_return) * sqrt(252)

    Uses per-trade PnL as returns.  Returns None if fewer than 2
    trades or zero standard deviation.
    """
    if len(trades) < 2:
        return None

    returns = [t.pnl for t in trades]
    n = len(returns)
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(variance) if variance > 0 else 0.0

    if std_r == 0:
        return None

    sharpe = (mean_r / std_r) * math.sqrt(252)
    return Decimal(str(round(sharpe, 4)))


# ── Analytical artifact persistence ──


def _persist_analytical_artifacts(
    execution: BacktestExecution,
    job,
    trades: list[Trade],
    metrics: SummaryMetrics,
) -> None:
    """
    Persist extended analytical data as artifact files.

    PnL series and equity curve are stored as artifacts (not in
    BacktestSummary) to keep the summary row lean and the schema
    within approved B1 boundaries.
    """
    # ── Equity curve artifact ──
    initial_equity = 10000.0
    equity_points = [{"trade": 0, "equity": initial_equity, "pnl": 0.0}]
    cumulative = initial_equity
    for t in trades:
        cumulative += t.pnl
        equity_points.append({
            "trade": t.trade_number,
            "equity": round(cumulative, 2),
            "pnl": t.pnl,
        })

    equity_content = json.dumps(
        {
            "execution_id": execution.pk,
            "initial_equity": initial_equity,
            "final_equity": round(cumulative, 2),
            "total_pnl": round(cumulative - initial_equity, 2),
            "points": equity_points,
        },
        indent=2,
    )
    _write_summary_artifact(execution, "equity_curve", equity_content, "json")

    # ── PnL series artifact ──
    pnl_content = json.dumps(
        {
            "execution_id": execution.pk,
            "trades": [
                {
                    "trade_number": t.trade_number,
                    "pnl": t.pnl,
                    "is_win": t.is_win,
                }
                for t in trades
            ],
            "summary": {
                "total_trades": metrics.total_trades,
                "gross_profit": round(sum(t.pnl for t in trades if t.is_win), 2),
                "gross_loss": round(sum(t.pnl for t in trades if not t.is_win), 2),
                "net_pnl": round(sum(t.pnl for t in trades), 2),
            },
        },
        indent=2,
    )
    _write_summary_artifact(execution, "pnl_series", pnl_content, "json")


def _write_summary_artifact(
    execution: BacktestExecution,
    artifact_type: str,
    content: str,
    extension: str,
) -> None:
    """Write a summary-produced artifact file and create the metadata row."""
    stored = store_artifact(
        execution_id=execution.pk,
        artifact_type=artifact_type,
        content=content,
        extension=extension,
    )

    BacktestArtifact.objects.create(
        execution=execution,
        artifact_type=artifact_type,
        file_path=stored.file_path,
        file_size=stored.file_size,
        checksum=stored.checksum,
    )


# ── Summary persistence ──


def _persist_summary(
    execution: BacktestExecution,
    metrics: SummaryMetrics,
) -> BacktestSummary:
    """
    Create or replace the BacktestSummary for the given execution.

    Uses update_or_create to support safe re-invocation during
    development.  In production the OneToOne constraint ensures
    one summary per execution.
    """
    summary, created = BacktestSummary.objects.update_or_create(
        execution=execution,
        defaults={
            "total_trades": metrics.total_trades,
            "win_rate": metrics.win_rate,
            "profit_factor": metrics.profit_factor,
            "max_drawdown": metrics.max_drawdown,
            "sharpe_ratio": metrics.sharpe_ratio,
            "expectancy": metrics.expectancy,
        },
    )

    action = "Created" if created else "Updated"
    logger.info(
        "%s BacktestSummary for execution %s (pk=%d)",
        action,
        execution.run_identifier,
        summary.pk,
    )

    return summary
