from django.conf import settings
from django.db import models
from django.utils import timezone

from trading.models import TradingAccount
from strategies.models import Strategy, StrategyAssignment


# =============================================================================
# Demo Trade Safety Constants
# =============================================================================
# These are hard-coded safety rails for the demo execution feature.
# They cannot be overridden by user input or configuration.

DEMO_ALLOWED_SYMBOLS = ["EURUSD"]  # Only these symbols can be traded in demo
DEMO_FIXED_LOT_SIZE = 0.01  # Fixed lot size for all demo trades
DEMO_MAX_TRADES_PER_DAY = 3  # Maximum demo trades per account per day

# =============================================================================
# Strategy Signal Execution Safety Constants
# =============================================================================
# These apply to PLACE_ORDER jobs from strategy signals.

SIGNAL_ALLOWED_SYMBOLS = ["EURUSD", "GBPUSD"]  # Symbols allowed for signal execution
SIGNAL_MAX_LOT_SIZE = 0.02  # Hard cap on lot size for strategy signals
SIGNAL_MAX_TRADES_PER_DAY = 3  # Maximum signal trades per account+strategy+symbol per day
SIGNAL_MAX_CONCURRENT_POSITIONS = 1  # Max concurrent positions per account+strategy+symbol


class ExecutionJob(models.Model):
    class JobType(models.TextChoices):
        TEST_CONNECTION = "TEST_CONNECTION", "Test connection"
        OPEN_TRADE = "OPEN_TRADE", "Open trade"
        CLOSE_TRADE = "CLOSE_TRADE", "Close trade"
        SYNC_POSITIONS = "SYNC_POSITIONS", "Sync positions"
        PLACE_TEST_ORDER = "PLACE_TEST_ORDER", "Place test order (demo)"
        PLACE_ORDER = "PLACE_ORDER", "Place order (strategy signal)"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    job_type = models.CharField(max_length=32, choices=JobType.choices)

    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="execution_jobs",
    )

    strategy = models.ForeignKey(
        Strategy,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="execution_jobs",
    )

    assignment = models.ForeignKey(
        StrategyAssignment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="execution_jobs",
    )

    # Arbitrary parameters for the job (symbol, volume, SL/TP, etc.)
    payload = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )

    worker_id = models.CharField(max_length=64, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_execution_jobs",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.job_type} for account {self.account_id} (status={self.status})"

    @classmethod
    def count_today_demo_trades(cls, account_id: int) -> int:
        """
        Count PLACE_TEST_ORDER jobs created today for the given account.
        Used to enforce DEMO_MAX_TRADES_PER_DAY limit.
        """
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return cls.objects.filter(
            account_id=account_id,
            job_type=cls.JobType.PLACE_TEST_ORDER,
            created_at__gte=today_start,
        ).count()

    @classmethod
    def count_today_signal_trades(cls, account_id: int, strategy_id: int, symbol: str) -> int:
        """
        Count PLACE_ORDER jobs completed with SUCCESS today for the given account+strategy+symbol.
        Used to enforce SIGNAL_MAX_TRADES_PER_DAY limit.

        NOTE: Only SUCCESS jobs count toward the limit.
        FAILED jobs (including market_closed) do NOT consume the daily limit,
        so users can retry when the market opens.
        """
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return cls.objects.filter(
            account_id=account_id,
            strategy_id=strategy_id,
            job_type=cls.JobType.PLACE_ORDER,
            payload__symbol=symbol,
            status=cls.Status.SUCCESS,  # Only count successful trades
            created_at__gte=today_start,
        ).count()

    @classmethod
    def count_pending_signal_jobs(cls, account_id: int, strategy_id: int, symbol: str) -> int:
        """
        Count pending/running PLACE_ORDER jobs for the given account+strategy+symbol.
        Used to prevent duplicate signals being queued.
        """
        return cls.objects.filter(
            account_id=account_id,
            strategy_id=strategy_id,
            job_type=cls.JobType.PLACE_ORDER,
            payload__symbol=symbol,
            status__in=[cls.Status.PENDING, cls.Status.RUNNING],
        ).count()
