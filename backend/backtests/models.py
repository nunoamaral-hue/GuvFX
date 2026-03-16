from django.conf import settings
from django.db import models

from strategies.models import Strategy
from trading.models import TradingAccount


class BacktestConfig(models.Model):
    """
    A saved backtest configuration for a given strategy.

    This defines WHAT to backtest (strategy, symbol, timeframe, date range, etc.).
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="backtest_configs",
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    strategy = models.ForeignKey(
        Strategy,
        on_delete=models.CASCADE,
        related_name="backtest_configs",
    )

    # Optional: tie config to a specific account environment (spread, leverage, etc.)
    reference_account = models.ForeignKey(
        TradingAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backtest_configs",
        help_text="Optional account to use as reference for conditions (e.g. spread, leverage).",
    )

    symbol = models.CharField(max_length=32, help_text="Primary symbol for the backtest.")
    timeframe = models.CharField(max_length=20, help_text="Timeframe (e.g. H1, H4, D1).")

    date_from = models.DateField()
    date_to = models.DateField()

    initial_balance = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=10000,
        help_text="Starting balance for the backtest.",
    )
    risk_per_trade_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Risk % per trade override (if null, use strategy default).",
    )

    slippage_points = models.IntegerField(
        null=True,
        blank=True,
        help_text="Optional slippage assumption in points.",
    )
    commission_per_lot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Commission per lot (if any).",
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.owner} | {self.name}"
    

class BacktestRun(models.Model):
    """
    A single execution (or requested execution) of a BacktestConfig.

    Later, a worker/engine will update status, metrics, and possibly attach logs/results.
    """

    STATUS_PENDING = "PENDING"
    STATUS_RUNNING = "RUNNING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    config = models.ForeignKey(
        BacktestConfig,
        on_delete=models.CASCADE,
        related_name="runs",
    )

    # Snapshot some key params, in case config changes later
    symbol = models.CharField(max_length=32)
    timeframe = models.CharField(max_length=20)
    date_from = models.DateField()
    date_to = models.DateField()
    initial_balance = models.DecimalField(max_digits=20, decimal_places=2)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    error_message = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    # Generic JSON metrics to keep it flexible: equity curve, stats, etc.
    metrics = models.JSONField(
        null=True,
        blank=True,
        help_text="Summary stats (e.g. total return, max DD, win rate, etc.)",
    )
    equity_curve = models.JSONField(
        null=True,
        blank=True,
        help_text="Optional equity curve data points.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"BacktestRun #{self.id} | {self.config.name} | {self.status}"


class WindowsBacktestJob(models.Model):
    """
    Persists Windows agent backtest job metadata, status, and results.

    Created when backend calls POST /mt5/backtest/run on the Windows agent.
    Updated when polling status/result endpoints.
    """

    STATE_QUEUED = "queued"
    STATE_RUNNING = "running"
    STATE_COMPLETED = "completed"
    STATE_FAILED = "failed"

    STATE_CHOICES = [
        (STATE_QUEUED, "Queued"),
        (STATE_RUNNING, "Running"),
        (STATE_COMPLETED, "Completed"),
        (STATE_FAILED, "Failed"),
    ]

    job_id = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Unique job ID returned by the Windows agent.",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="windows_backtest_jobs",
    )

    strategy = models.ForeignKey(
        Strategy,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="windows_backtest_jobs",
    )

    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="windows_backtest_jobs",
    )

    # Request parameters sent to agent
    username = models.CharField(max_length=128, help_text="Windows username for MT5 instance.")
    datadir = models.CharField(max_length=512, blank=True, help_text="MT5 data directory path.")
    symbol = models.CharField(max_length=32, help_text="Symbol to backtest.")
    timeframe = models.CharField(max_length=20, help_text="Timeframe (e.g. H1, H4, D1).")
    date_from = models.DateField(help_text="Backtest start date.")
    date_to = models.DateField(help_text="Backtest end date.")
    deposit = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        help_text="Initial deposit for backtest.",
    )
    leverage = models.IntegerField(help_text="Leverage setting (e.g. 100).")
    mode = models.CharField(
        max_length=32,
        default="real_ticks",
        help_text="Backtest mode (e.g. real_ticks, 1_minute_ohlc).",
    )

    # Job state
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_QUEUED,
    )

    # Raw JSON responses from agent
    status_json = models.JSONField(
        null=True,
        blank=True,
        help_text="Latest status response from Windows agent.",
    )
    result_json = models.JSONField(
        null=True,
        blank=True,
        help_text="Final result response from Windows agent.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"WindowsBacktestJob {self.job_id} | {self.owner} | {self.state}"


# =========================================================================
# Packet B — B1: Canonical Backtesting Domain Models
# =========================================================================
#
# New models below coexist with the existing BacktestConfig/BacktestRun/
# WindowsBacktestJob above.  The B1 models represent the formal, pipeline-
# oriented backtesting subsystem.
#
# Naming note: The B1 spec's "BacktestRun" is named "BacktestJobRun" here
# to avoid collision with the existing BacktestRun class (which references
# BacktestConfig, not BacktestJob).  The relationship and fields match the
# approved spec exactly.
# =========================================================================


class BacktestStatus(models.TextChoices):
    """
    Canonical status values for BacktestJob and BacktestJobRun.

    Used by both models via ``choices=BacktestStatus.choices``.
    """

    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class BacktestJob(models.Model):
    """
    A formal backtest job request.

    Represents a user's request to execute a backtest of a strategy
    over a specific symbol/timeframe/date range with given parameters.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="backtest_jobs",
    )
    strategy = models.ForeignKey(
        Strategy,
        on_delete=models.CASCADE,
        related_name="backtest_jobs",
    )

    symbol = models.CharField(max_length=32, help_text="Symbol to backtest (e.g. EURUSD).")
    timeframe = models.CharField(max_length=20, help_text="Timeframe (e.g. H1, H4, D1).")
    start_date = models.DateField(help_text="Backtest start date.")
    end_date = models.DateField(help_text="Backtest end date.")

    parameter_set = models.JSONField(
        default=dict,
        blank=True,
        help_text="Strategy parameter overrides for this job (JSON).",
    )
    data_source = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Data source identifier (e.g. broker tick data, synthetic).",
    )

    status = models.CharField(
        max_length=20,
        choices=BacktestStatus.choices,
        default=BacktestStatus.QUEUED,
        db_index=True,
    )

    requested_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the job was first requested.",
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When a worker began processing the job.",
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the job finished (success or failure).",
    )

    worker_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Identifier of the worker that claimed the job.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"], name="bt_job_user_status_idx"),
            models.Index(fields=["-requested_at"], name="bt_job_requested_idx"),
        ]

    def __str__(self) -> str:
        return f"BacktestJob #{self.pk} | {self.user_id} | {self.strategy_id} | {self.status}"


class BacktestJobRun(models.Model):
    """
    A single execution run within a BacktestJob.

    One job may produce multiple runs (e.g. parameter sweeps, retries).
    Each run tracks its own status, worker, timing, and log location.

    Naming: Called "BacktestJobRun" to avoid collision with the existing
    BacktestRun model.  Matches the B1 spec's "BacktestRun" in all fields.
    """

    backtest_job = models.ForeignKey(
        BacktestJob,
        on_delete=models.CASCADE,
        related_name="job_runs",
    )

    run_identifier = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Unique identifier for this run (e.g. UUID or slug).",
    )

    status = models.CharField(
        max_length=20,
        choices=BacktestStatus.choices,
        default=BacktestStatus.QUEUED,
        db_index=True,
    )

    worker_hostname = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Hostname of the worker executing this run.",
    )

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    duration_seconds = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Wall-clock duration of the run in seconds.",
    )

    log_path = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Path or URI to the run's log output.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["backtest_job", "status"], name="bt_jobrun_job_status_idx"),
        ]

    def __str__(self) -> str:
        return f"BacktestJobRun {self.run_identifier} | {self.status}"


# ── BacktestArtifact immutability infrastructure ──


class BacktestArtifactQuerySet(models.QuerySet):
    """QuerySet that blocks bulk update and delete to enforce immutability."""

    def update(self, **kwargs):
        raise ValueError("BacktestArtifact records are immutable and cannot be updated.")

    def delete(self):
        raise ValueError("BacktestArtifact records are immutable and cannot be deleted.")


class BacktestArtifactManager(models.Manager):
    def get_queryset(self):
        return BacktestArtifactQuerySet(self.model, using=self._db)


class BacktestArtifact(models.Model):
    """
    An immutable output artifact produced by a BacktestJobRun.

    Artifacts are append-only: once created they cannot be updated or
    deleted.  This follows the same immutability pattern as core.AuditEvent.

    Examples: equity curve CSV, trade log, HTML report, screenshot.
    """

    objects = BacktestArtifactManager()

    backtest_run = models.ForeignKey(
        BacktestJobRun,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )

    artifact_type = models.CharField(
        max_length=100,
        help_text="Type of artifact (e.g. equity_curve, trade_log, html_report).",
    )
    file_path = models.CharField(
        max_length=1024,
        help_text="Path or URI to the stored artifact file.",
    )
    file_size = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="File size in bytes.",
    )
    checksum = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Integrity checksum (e.g. SHA-256 hex digest).",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["backtest_run", "artifact_type"], name="bt_artifact_run_type_idx"),
        ]

    def __str__(self) -> str:
        return f"BacktestArtifact #{self.pk} | {self.artifact_type} | run={self.backtest_run_id}"

    def save(self, *args, **kwargs):
        # Append-only: block updates to existing records
        if self.pk and BacktestArtifact.objects.filter(pk=self.pk).exists():
            raise ValueError("BacktestArtifact records are immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("BacktestArtifact records are immutable and cannot be deleted.")


class BacktestSummary(models.Model):
    """
    Aggregated performance summary for a BacktestJobRun.

    Stores the canonical performance metrics produced by the
    backtesting engine after a run completes.
    """

    backtest_run = models.OneToOneField(
        BacktestJobRun,
        on_delete=models.CASCADE,
        related_name="summary",
    )

    total_trades = models.IntegerField(
        default=0,
        help_text="Total number of trades executed.",
    )
    win_rate = models.DecimalField(
        max_digits=7,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Win rate as a decimal (e.g. 0.5500 = 55%).",
    )
    profit_factor = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Gross profit / gross loss.",
    )
    max_drawdown = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Maximum drawdown as a decimal (e.g. 0.1200 = 12%).",
    )
    sharpe_ratio = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Annualized Sharpe ratio.",
    )
    expectancy = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Expected value per trade in account currency.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return (
            f"BacktestSummary | run={self.backtest_run_id} | "
            f"trades={self.total_trades} | wr={self.win_rate}"
        )