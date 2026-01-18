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