import hashlib

from django.conf import settings
from django.db import models
from django.utils import timezone

from trading.models import TradingAccount
from strategies.models import Strategy, StrategyAssignment


# =============================================================================
# Terminal Node — represents an execution host running one or more MT5 workers
# =============================================================================


class TerminalNode(models.Model):
    """
    Represents a physical or virtual execution host that runs MT5 worker
    processes.  Each node can service multiple TradingAccounts.

    ``status`` is an *operator-declared* value — it is never auto-mutated by
    the heartbeat endpoint (which only updates ``last_heartbeat``).

    Capacity is *computed* from the count of TradingAccount rows that point at
    this node, not from ``active_accounts`` alone.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DRAINING = "draining", "Draining"
        OFFLINE = "offline", "Offline"
        DISABLED = "disabled", "Disabled"

    hostname = models.CharField(
        max_length=128,
        unique=True,
        help_text="Unique identifier for this execution host.",
    )
    display_name = models.CharField(
        max_length=128,
        blank=True,
        help_text="Human-friendly label shown in admin surfaces.",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    # Capacity bookkeeping — max_accounts is operator-set; active_accounts is
    # an advisory cache that MUST NOT be trusted for enforcement (use
    # TradingAccount.objects.filter(terminal_node=self).count() instead).
    max_accounts = models.PositiveIntegerField(
        default=50,
        help_text="Operator-declared maximum accounts this node can handle.",
    )
    active_accounts = models.PositiveIntegerField(
        default=0,
        help_text="Advisory cache — real count derived from TradingAccount FK.",
    )

    # Heartbeat — updated only by the heartbeat endpoint; never auto-mutates
    # ``status``.
    last_heartbeat = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time a worker on this node reported in.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["hostname"]
        verbose_name = "Terminal Node"
        verbose_name_plural = "Terminal Nodes"

    def __str__(self) -> str:
        label = self.display_name or self.hostname
        return f"{label} ({self.status})"

    @property
    def computed_active_accounts(self) -> int:
        """Authoritative account count — derived from FK, not cache field."""
        return TradingAccount.objects.filter(
            terminal_node=self, is_active=True
        ).count()

    @property
    def has_capacity(self) -> bool:
        return self.computed_active_accounts < self.max_accounts


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

SIGNAL_ALLOWED_SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD"]  # Symbols allowed for signal execution
SIGNAL_MAX_LOT_SIZE = 0.02  # Hard cap on lot size for strategy signals
SIGNAL_MAX_TRADES_PER_DAY = 10  # Maximum signal trades per account+strategy+symbol per day (SUCCESS only)
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

    # Snapshot of the terminal node at job creation time.
    # NULL means "legacy job" (created before node-aware routing).
    terminal_node = models.ForeignKey(
        TerminalNode,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="execution_jobs",
        help_text="Snapshotted from account.terminal_node at job creation.",
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

    # RX-2E: lease set when the job becomes RUNNING (claim path). A RUNNING job
    # whose lease has expired (or has no lease) is an orphan — detected by the
    # reliability supervisor. Nullable at DB level (PENDING jobs have no lease).
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    recovered = models.BooleanField(default=False)
    recovery_reason = models.TextField(blank=True)

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


# =============================================================================
# Worker Identity (for authenticated worker ↔ backend communication)
# =============================================================================


class WorkerIdentity(models.Model):
    """
    Registered MT5 worker identity with hashed secret for authentication.

    Secrets are stored as SHA-256 hashes and validated via constant-time
    comparison (``hmac.compare_digest``).
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"

    worker_id = models.CharField(max_length=64, unique=True)
    worker_secret_hash = models.CharField(
        max_length=255,
        help_text="SHA-256 hex digest of the worker secret.",
    )
    worker_permissions = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary permission flags for future use.",
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Worker Identity"
        verbose_name_plural = "Worker Identities"

    def __str__(self) -> str:
        return f"Worker {self.worker_id} ({self.status})"

    @staticmethod
    def hash_secret(raw_secret: str) -> str:
        """Return the SHA-256 hex digest of *raw_secret*."""
        return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()
