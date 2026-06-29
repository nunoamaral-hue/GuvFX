"""
Packet B — B4: BacktestSummary calculation engine.

Consumes outputs produced by the backtest worker/artifact flow and
computes the approved summary metrics for BacktestSummary.

Approved BacktestSummary schema fields (B1):
    total_trades, win_rate, profit_factor, max_drawdown,
    sharpe_ratio, expectancy

Extended analytical data (PnL series, equity curve) is persisted as
artifact files — not stored in BacktestSummary or PostgreSQL blobs.

Honest-support policy:
    The summary engine only computes metrics that are genuinely
    derivable from actual worker-produced outputs.  If the current
    worker outputs (e.g. result_stub with status=placeholder) do
    not contain trade data, all trade-dependent metrics are set to
    their safe null/default values.  No synthetic or fabricated
    trade data is generated.
"""
import gzip
import json
import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.conf import settings

from backtests.artifact_storage import store_artifact, _get_artifact_root
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
    persist them as a BacktestSummary row.

    Metrics are derived strictly from actual worker-produced artifact
    outputs.  If no trade data is available in the artifacts, all
    trade-dependent metrics are set to null/0.

    Extended analytical artifacts (equity_curve, pnl_series) are only
    produced when real trade data is available.

    Returns the created BacktestSummary instance.
    """
    # ── Obtain trade data from actual artifacts ──
    trades = _load_trades_from_artifacts(execution)

    # ── Compute metrics ──
    metrics = _compute_metrics(trades)

    # ── Persist analytical artifacts only if real trade data exists ──
    if trades:
        _persist_analytical_artifacts(execution, trades, metrics)

    # ── Create/update BacktestSummary row ──
    summary = _persist_summary(execution, metrics)

    logger.info(
        "Summary computed for execution %s: trades=%d, win_rate=%s, pf=%s, dd=%s "
        "(trade_data_available=%s)",
        execution.run_identifier,
        metrics.total_trades,
        metrics.win_rate,
        metrics.profit_factor,
        metrics.max_drawdown,
        bool(trades),
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


# ── Trade loading from actual artifacts ──


def _load_trades_from_artifacts(execution: BacktestExecution) -> list[Trade]:
    """
    Load trade results from the execution's produced artifact files.

    Reads the ``result_stub`` artifact and parses trade data from it
    if available.  Returns an empty list if no trade data exists in
    any artifact (e.g. placeholder execution with no real engine).

    When a real backtesting engine is integrated, it will produce
    a result artifact containing a ``trades`` array.  This function
    parses that structure.
    """
    # Try to read the result_stub artifact
    result_data = _read_artifact_json(execution, "result_stub")
    if result_data is None:
        logger.info(
            "No result_stub artifact found for execution %s — "
            "no trade data available",
            execution.run_identifier,
        )
        return []

    # Check if the result contains actual trade data
    raw_trades = result_data.get("trades")
    if not isinstance(raw_trades, list) or not raw_trades:
        logger.info(
            "result_stub for execution %s contains no trade data "
            "(status=%s) — metrics will use safe defaults",
            execution.run_identifier,
            result_data.get("status", "unknown"),
        )
        return []

    # Parse trade records from the artifact
    trades = []
    for i, raw in enumerate(raw_trades):
        try:
            pnl = float(raw.get("pnl", 0))
            is_win = bool(raw.get("is_win", pnl > 0))
            trade_number = int(raw.get("trade_number", i + 1))
            trades.append(Trade(trade_number=trade_number, pnl=pnl, is_win=is_win))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Skipping malformed trade record %d in execution %s: %s",
                i,
                execution.run_identifier,
                exc,
            )

    logger.info(
        "Loaded %d trades from result_stub artifact for execution %s",
        len(trades),
        execution.run_identifier,
    )
    return trades


def _read_artifact_json(
    execution: BacktestExecution, artifact_type: str
) -> dict | None:
    """
    Read and parse a JSON artifact file for the given execution.

    Handles both gzip-compressed (.gz) and plain JSON files.
    Returns None if the artifact does not exist or cannot be read.
    """
    try:
        artifact = BacktestArtifact.objects.filter(
            execution=execution,
            artifact_type=artifact_type,
        ).first()

        if artifact is None:
            return None

        root = _get_artifact_root()
        full_path = root / artifact.file_path

        if not full_path.exists():
            logger.warning(
                "Artifact file not found on disk: %s", full_path
            )
            return None

        raw_bytes = full_path.read_bytes()

        # Decompress if gzipped
        if artifact.file_path.endswith(".gz"):
            raw_bytes = gzip.decompress(raw_bytes)

        return json.loads(raw_bytes)

    except Exception as exc:
        logger.warning(
            "Failed to read artifact %s for execution %s: %s",
            artifact_type,
            execution.run_identifier,
            exc,
        )
        return None


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

    If ``trades`` is empty (no trade data available from artifacts),
    returns total_trades=0 and all other metrics as None.
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
    trades: list[Trade],
    metrics: SummaryMetrics,
) -> None:
    """
    Persist extended analytical data as artifact files.

    Only called when real trade data is available from artifacts.
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
