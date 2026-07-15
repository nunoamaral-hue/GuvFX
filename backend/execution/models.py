import hashlib
import os
from decimal import Decimal

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
SIGNAL_MAX_CONCURRENT_POSITIONS = int(os.getenv("SIGNAL_MAX_CONCURRENT_POSITIONS", "20"))  # Max concurrent positions per account+strategy+symbol (env-tunable)

# =============================================================================
# EXEC-E1b — Multi-leg demo execution PLAN safety constants (NO order is placed)
# =============================================================================
# These bound the NON-EXECUTABLE planning layer (SignalExecutionPlan +
# ProposedOrderLeg). They never reach a broker — a plan is not an ExecutionJob.

MAX_PLAN_LEGS = 3  # A signal is split into at most this many legs (one per TP).
LOT_STEP = "0.01"  # Broker minimum lot increment used by the volume split.
MAX_TOTAL_LOT_PER_SIGNAL = "0.06"  # Hard cap on the summed lot across a plan's legs.
DEMO_SOURCE_TOTAL_LOT_DEFAULT = "0.03"  # Default per-source total lot target.
SIGNAL_MAX_AGE_SECONDS = 120  # A signal older than this is voided (stale).
PLAN_MAX_GROUPS_PER_DAY = int(os.getenv("PLAN_MAX_GROUPS_PER_DAY", "24"))  # Max signal-GROUPS (plans) per account+symbol+SOURCE per calendar day (env-tunable). Per-SOURCE: each provider (e.g. wayond, ti_signals) has its own independent daily budget — one source can never consume another's allowance. Counts acted-on groups (PLANNED/PROMOTED/CLOSED), not just the momentary PLANNED backlog — see count_today.
PLAN_MAX_CONCURRENT_GROUPS = int(os.getenv("PLAN_MAX_CONCURRENT_GROUPS", "10"))  # Max concurrent ACTIVE groups (PLANNED/PROMOTED) per account+symbol (env-tunable). A plan leaves the active set when CLOSED (all its positions resolved) — see resolve_completed_plans.


class ExecutionJob(models.Model):
    class JobType(models.TextChoices):
        TEST_CONNECTION = "TEST_CONNECTION", "Test connection"
        OPEN_TRADE = "OPEN_TRADE", "Open trade"
        CLOSE_TRADE = "CLOSE_TRADE", "Close trade"
        SYNC_POSITIONS = "SYNC_POSITIONS", "Sync positions"
        PLACE_TEST_ORDER = "PLACE_TEST_ORDER", "Place test order (demo)"
        PLACE_ORDER = "PLACE_ORDER", "Place order (strategy signal)"
        # WS-B AUTO-BREAKEVEN — modify an OPEN position's stop-loss (and keep its
        # take-profit). Risk-REDUCING only: used to move a remaining leg's SL to the
        # entry price after TP1 closes. Not exposure-opening, so (like CLOSE_TRADE)
        # it is deliberately NOT in KILL_SWITCH_BLOCKED_JOB_TYPES — a breakeven must
        # still complete while the kill switch is engaged (closing risk is safe).
        MODIFY_POSITION = "MODIFY_POSITION", "Modify position SL/TP (breakeven)"
        # EXEC-E2a — a SUPPRESSED, un-claimable shadow job promoted from a demo
        # plan. Distinct from PLACE_ORDER so no deployed worker claims it, and
        # served by next_job only to a shadow_worker (see views.next_job guard).
        # No consumer executes it in E2a; it places no order.
        PLACE_ORDER_SHADOW = "PLACE_ORDER_SHADOW", "Place order (shadow / suppressed — no order)"

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

    def save(self, *args, **kwargs):
        # EXEC-HARDEN-JOBS: functional kill switch at the model layer. Block
        # creation of an exposure-OPENING job while the kill switch (or the
        # GUVFX_EXECUTION_DISABLED env flag) is engaged, so EVERY creation path
        # (generic API, services, strategy automation, demo) fails closed — not
        # just the ones that remember to check. Applies only on INSERT of an
        # order-opening job_type; updates and non-order jobs (SYNC_POSITIONS,
        # TEST_CONNECTION, CLOSE_TRADE) are unaffected.
        if self._state.adding and self.job_type in KILL_SWITCH_BLOCKED_JOB_TYPES:
            reason = order_creation_kill_reason()
            if reason:
                raise ExecutionKillSwitchEngaged(reason)
        super().save(*args, **kwargs)

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
# EXEC-HARDEN-JOBS — functional kill switch for order-bearing job creation
# =============================================================================
# Single source of truth used by the model-layer guard above, the user-facing
# order endpoints, and the E1a proposal bridge. Read-only (never creates the
# ExecutionControl row). ExecutionControl is defined later in this module; the
# name is resolved at call time.

# Job types that OPEN / increase market exposure. CLOSE_TRADE is intentionally
# excluded so positions can still be flattened while the switch is engaged.
KILL_SWITCH_BLOCKED_JOB_TYPES = (
    ExecutionJob.JobType.OPEN_TRADE,
    ExecutionJob.JobType.PLACE_ORDER,
    ExecutionJob.JobType.PLACE_TEST_ORDER,
    # EXEC-E2a — belt-and-braces: the kill switch also blocks creating a shadow
    # job. (Not load-bearing — a shadow job no consumer executes cannot reach a
    # broker regardless — but keeps promotion fail-closed under an engaged kill.)
    ExecutionJob.JobType.PLACE_ORDER_SHADOW,
)


class ExecutionKillSwitchEngaged(Exception):
    """Raised when an order-opening ExecutionJob is created while the kill switch
    (ExecutionControl) or the GUVFX_EXECUTION_DISABLED env flag is engaged."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"execution kill switch engaged: {reason}")


def order_creation_kill_reason():
    """Stable reason code if order-opening job creation is currently blocked,
    else None. Honours GUVFX_EXECUTION_DISABLED (defence in depth) and the DB
    ExecutionControl.kill_switch_engaged flag; performs no writes."""
    if os.getenv("GUVFX_EXECUTION_DISABLED", "").lower() in ("true", "1", "yes"):
        return "execution_globally_disabled"
    if ExecutionControl.objects.filter(
        pk=ExecutionControl.SINGLETON_ID, kill_switch_engaged=True
    ).exists():
        return "kill_switch_engaged"
    return None


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


# =============================================================================
# EXEC-E1a — Signal → ProposedSignalOrder bridge (NEVER places an order)
# =============================================================================
#
# A ``ProposedSignalOrder`` is a NON-EXECUTABLE candidate derived from an
# APPROVED ``signal_intake.PendingSignalApproval``. It is deliberately NOT an
# ``ExecutionJob``: the MT5 worker claims work via
# ``ExecutionJob.objects.filter(status=PENDING)`` (see ``execution.views``
# ``next_job``) and never queries this table, so a proposal can never reach a
# broker. "No real order" is therefore a STRUCTURAL guarantee, not merely a
# policy one. Promotion of a proposal to an executable job is a separate,
# sponsor-gated packet (E2+).
#
# The bridge that creates these rows lives in ``execution.signal_proposals`` and
# is the only writer. It imports ``signal_intake`` (one-way: execution may read
# signal_intake; signal_intake/wims/intelligence never import execution).


class ExecutionControl(models.Model):
    """Singleton DB-backed execution control state — the functional kill switch.

    Replaces the MVP 501 stub. The signal-proposal bridge fails closed when the
    global kill switch is engaged or signal proposals are disabled. The legacy
    ``GUVFX_EXECUTION_DISABLED`` environment flag remains honoured as
    defence-in-depth (see ``order_creation_kill_reason`` in this module).
    """

    SINGLETON_ID = 1

    class SignalExecutionMode(models.TextChoices):
        # Default + safest: shadow promotion only (order_check dry-run, no order placed).
        SHADOW = "SHADOW", "Shadow (suppressed — no order placed)"
        # E3-DEMO-PROMOTION: real order_send on a DEMO account. NOT a default; the master
        # lever an operator flips (under Nuno's recorded sign-off) to arm auto-demo. Nothing
        # auto-fires without ALSO arming auto_execution_enabled + an AUTO_DEMO assignment +
        # source + provider. LIVE is intentionally NOT defined (a separate, far-future packet).
        DEMO = "DEMO", "Demo (real order_send on a DEMO account — E3, gated OFF by default)"

    kill_switch_engaged = models.BooleanField(
        default=False,
        help_text="When true, the signal-proposal bridge fails closed.",
    )
    signal_proposals_enabled = models.BooleanField(
        default=True,
        help_text="Signal-specific disable: blocks proposals without a full kill.",
    )
    signal_execution_mode = models.CharField(
        max_length=16,
        choices=SignalExecutionMode.choices,
        default=SignalExecutionMode.SHADOW,
        help_text="E2a global gate: promotion only proceeds in SHADOW mode.",
    )
    # AUTO-SHADOW FOUNDATION — auto-only soft enable. Independent of the kill switch:
    # kill_switch_engaged blocks ALL order creation (manual + auto); this pauses the
    # AUTO path only, leaving manual per-signal approval fully working. Default OFF so
    # the auto-router is a no-op until deliberately armed.
    auto_execution_enabled = models.BooleanField(
        default=False,
        help_text="Auto-only enable for the auto-router. OFF by default; manual path unaffected.",
    )
    reason = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Execution Control"
        verbose_name_plural = "Execution Control"

    def __str__(self) -> str:
        if self.kill_switch_engaged:
            state = "KILLED"
        else:
            state = "proposals-on" if self.signal_proposals_enabled else "proposals-off"
        return f"ExecutionControl(#{self.pk}: {state})"

    def save(self, *args, **kwargs):
        # Enforce singleton: there is exactly one control row.
        self.pk = self.SINGLETON_ID
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls) -> "ExecutionControl":
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_ID)
        return obj


class ProposedSignalOrder(models.Model):
    """Non-executable order candidate derived from an APPROVED signal.

    NOT an ``ExecutionJob`` — structurally invisible to the MT5 worker claim
    path. Creating one places no order, queues no job, and contacts no broker.
    """

    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed (no order placed)"
        REJECTED = "REJECTED", "Rejected"
        SUPERSEDED = "SUPERSEDED", "Superseded"

    class Direction(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    # One proposal per approval — DB-level duplicate protection.
    approval = models.OneToOneField(
        "signal_intake.PendingSignalApproval",
        on_delete=models.PROTECT,
        related_name="proposed_order",
    )
    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.PROTECT,
        related_name="proposed_signal_orders",
    )

    symbol = models.CharField(max_length=32)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    entry = models.CharField(max_length=32, blank=True)
    stop_loss = models.CharField(max_length=32, blank=True)
    take_profit = models.CharField(max_length=32, blank=True)

    lot_size = models.DecimalField(max_digits=6, decimal_places=2)
    risk_per_trade_pct = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("1.00")
    )

    # Captured account context at proposal time (decision immutability).
    is_demo = models.BooleanField()
    account_environment = models.CharField(max_length=16, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PROPOSED
    )
    notes = models.TextField(blank=True)

    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="proposed_signal_orders",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Proposed Signal Order"
        verbose_name_plural = "Proposed Signal Orders"

    def __str__(self) -> str:
        return (
            f"Proposal #{self.pk} {self.direction} {self.symbol} "
            f"(demo={self.is_demo}, {self.status}) — NO ORDER"
        )

    @classmethod
    def count_today(cls, account_id: int, symbol: str) -> int:
        """Non-rejected proposals created today for account+symbol.

        Mirrors ``ExecutionJob.count_today_signal_trades`` semantics for the
        proposal layer, enforcing ``SIGNAL_MAX_TRADES_PER_DAY``.
        """
        today_start = timezone.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return cls.objects.filter(
            account_id=account_id,
            symbol=symbol,
            created_at__gte=today_start,
        ).exclude(status=cls.Status.REJECTED).count()

    @classmethod
    def count_active(cls, account_id: int, symbol: str) -> int:
        """Currently-PROPOSED proposals for account+symbol.

        Mirrors ``ExecutionJob.count_pending_signal_jobs`` for the proposal
        layer, enforcing ``SIGNAL_MAX_CONCURRENT_POSITIONS``.
        """
        return cls.objects.filter(
            account_id=account_id,
            symbol=symbol,
            status=cls.Status.PROPOSED,
        ).count()


class ProposalAuditEvent(models.Model):
    """Append-only audit for the signal → proposal bridge and kill switch.

    Extends the signal audit chain begun in ``signal_intake.SignalAuditEvent``
    (SIGNAL_RECEIVED → APPROVED) with the proposal lifecycle. Linked back to the
    originating approval so the full chain is traceable.
    """

    class Event(models.TextChoices):
        PROPOSAL_CREATED = "PROPOSAL_CREATED", "Proposal created (no order)"
        PROPOSAL_REJECTED = "PROPOSAL_REJECTED", "Proposal rejected"
        KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED", "Kill switch engaged"
        KILL_SWITCH_RELEASED = "KILL_SWITCH_RELEASED", "Kill switch released"
        PROPOSALS_DISABLED = "PROPOSALS_DISABLED", "Signal proposals disabled"
        PROPOSALS_ENABLED = "PROPOSALS_ENABLED", "Signal proposals enabled"

    event = models.CharField(max_length=32, choices=Event.choices)
    proposal = models.ForeignKey(
        ProposedSignalOrder,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    approval = models.ForeignKey(
        "signal_intake.PendingSignalApproval",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="proposal_audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Proposal Audit Event"
        verbose_name_plural = "Proposal Audit Events"

    def __str__(self) -> str:
        return f"{self.event} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


# =============================================================================
# EXEC-E1b — Multi-leg demo execution PLAN (NEVER places an order)
# =============================================================================
#
# A SignalExecutionPlan + its ProposedOrderLeg children are a NON-EXECUTABLE
# representation of how an APPROVED Telegram signal would be split into up to
# three demo market orders (common SL, one TP per leg). They are NOT
# ExecutionJobs: the MT5 worker claims work via
# ``ExecutionJob.objects.filter(status=PENDING)`` and never queries these
# tables, so a plan/leg can never reach a broker. "No order" is structural.
# Promotion of a plan to executable (worker-suppressed) jobs is a separate,
# sponsor-gated packet (E2+).


class SignalSourceConfig(models.Model):
    """Per-source demo auto-execution configuration. Default OFF.

    A signal source (e.g. ``WAYOND_TELEGRAM``) is only eligible for demo
    auto-planning when a config row exists AND ``auto_demo_execution_enabled``
    is true. This is an independent fail-closed gate on top of the kill switch.
    """

    source = models.CharField(max_length=32, unique=True)
    auto_demo_execution_enabled = models.BooleanField(default=False)
    total_lot_target = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal(DEMO_SOURCE_TOTAL_LOT_DEFAULT)
    )
    # Per-SOURCE sizing ceilings. Defaults equal the global constants, so every existing
    # source (e.g. wayond) is behaviour-preserving; only a source explicitly raised (e.g.
    # ti_signals → 0.40/1.20) sizes larger. The planning split, promotion re-validation,
    # worker and bridge all read these — a source can never exceed its own row, fail-closed.
    max_lot_per_leg = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal(str(SIGNAL_MAX_LOT_SIZE))
    )
    max_total_lot = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal(MAX_TOTAL_LOT_PER_SIGNAL)
    )
    # Per-SOURCE daily signal-group cap. 0 = UNLIMITED (no daily cap — the source processes
    # signals indefinitely, still bounded by duplicate/expiry/concurrency/exposure/broker/margin
    # gates). Default = the global PLAN_MAX_GROUPS_PER_DAY so existing sources are unchanged.
    daily_group_cap = models.PositiveIntegerField(
        default=PLAN_MAX_GROUPS_PER_DAY,
        help_text="Max signal groups/day for this source; 0 = unlimited.",
    )
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Signal Source Config"
        verbose_name_plural = "Signal Source Configs"

    def __str__(self) -> str:
        state = "ENABLED" if self.auto_demo_execution_enabled else "disabled"
        return f"SignalSourceConfig({self.source}: {state})"

    @classmethod
    def sizing_caps(cls, source: str):
        """Return ``(max_lot_per_leg, max_total_lot)`` for ``source`` — its own config row,
        else the global constants (FAIL-CLOSED: an unknown/unconfigured source gets the
        conservative 0.02/0.06 defaults, never a larger size). Single source of truth for the
        planning split and the promotion-time re-validation."""
        cfg = cls.objects.filter(source=source).first()
        if cfg is None:
            return Decimal(str(SIGNAL_MAX_LOT_SIZE)), Decimal(MAX_TOTAL_LOT_PER_SIGNAL)
        return cfg.max_lot_per_leg, cfg.max_total_lot


class SignalExecutionPlan(models.Model):
    """Non-executable plan (signal-GROUP) derived from an APPROVED signal.

    NOT an ``ExecutionJob`` — structurally invisible to the worker claim path.
    Carries up to ``MAX_PLAN_LEGS`` ``ProposedOrderLeg`` children sharing one SL.
    """

    class Status(models.TextChoices):
        PLANNED = "PLANNED", "Planned (no order placed)"
        HELD = "HELD", "Held (data/safety)"
        VOIDED = "VOIDED", "Voided"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        PROMOTED = "PROMOTED", "Promoted to shadow jobs (no order placed)"
        CLOSED = "CLOSED", "Closed (all positions resolved)"

    class Direction(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    # One plan per approval — hard idempotency.
    approval = models.OneToOneField(
        "signal_intake.PendingSignalApproval",
        on_delete=models.PROTECT, related_name="execution_plan",
    )
    account = models.ForeignKey(
        TradingAccount, on_delete=models.PROTECT, related_name="signal_execution_plans",
    )

    # Dedup identity (source/chat/message) — REQUIRED idempotency at plan level.
    source = models.CharField(max_length=32)
    chat_id = models.CharField(max_length=64, blank=True)
    message_id = models.CharField(max_length=128)

    symbol = models.CharField(max_length=32)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    entry = models.CharField(max_length=32, blank=True)  # informational; market order
    stop_loss = models.CharField(max_length=32, blank=True)  # common SL for all legs
    order_type = models.CharField(max_length=8, default="MARKET")  # market-only

    total_lot = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.00"))
    is_demo = models.BooleanField()
    account_environment = models.CharField(max_length=16, blank=True)
    signal_timestamp = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLANNED)
    hold_reason = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)

    # OPS-OBSERVABILITY: correlation id copied from the source approval (fresh
    # fallback if absent) and propagated into each shadow job's payload. Nullable/
    # blank for backwards compatibility with pre-existing rows.
    correlation_id = models.CharField(max_length=64, blank=True, default="")

    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="signal_execution_plans",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "chat_id", "message_id"],
                name="uniq_plan_source_chat_message",
            ),
        ]
        verbose_name = "Signal Execution Plan"
        verbose_name_plural = "Signal Execution Plans"

    def __str__(self) -> str:
        return (
            f"Plan #{self.pk} {self.direction} {self.symbol} "
            f"(legs={self.legs.count()}, {self.status}) — NO ORDER"
        )

    @classmethod
    def count_today(cls, account_id: int, symbol: str, source: str) -> int:
        """Groups ACTED ON today for account+symbol+SOURCE (per-source daily cap).

        Counts every plan created today (this account+symbol) for ``source`` that
        consumed an execution slot — ``PLANNED`` (in flight), ``PROMOTED`` (executing)
        and ``CLOSED`` (resolved) — so the cap bounds true daily volume, not just the
        momentary PLANNED backlog (which quickly promotes/closes and would otherwise
        make the cap a no-op). ``VOIDED``/``HELD``/``SUPERSEDED`` plans (rejected, held,
        or replaced — no order acted on) do NOT count. Scoped by ``source`` so each
        provider has an independent budget: reaching one source's daily cap never blocks
        another source (per-source isolation).
        """
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return cls.objects.filter(
            account_id=account_id, symbol=symbol, source=source,
            status__in=(cls.Status.PLANNED, cls.Status.PROMOTED, cls.Status.CLOSED),
            created_at__gte=today_start,
        ).count()

    @classmethod
    def count_active(cls, account_id: int, symbol: str) -> int:
        """Currently-PLANNED groups for account+symbol (per-group concurrency)."""
        return cls.objects.filter(
            account_id=account_id, symbol=symbol, status=cls.Status.PLANNED,
        ).count()


class ProposedOrderLeg(models.Model):
    """One non-executable leg of a SignalExecutionPlan (one TP, shared SL).

    NOT an ``ExecutionJob`` and has no claimable status — placing none.
    """

    class Status(models.TextChoices):
        PLANNED = "PLANNED", "Planned (no order placed)"
        HELD = "HELD", "Held"
        VOIDED = "VOIDED", "Voided"
        PROMOTED = "PROMOTED", "Promoted to a shadow job (no order placed)"

    plan = models.ForeignKey(
        SignalExecutionPlan, on_delete=models.CASCADE, related_name="legs",
    )
    leg_index = models.PositiveSmallIntegerField()  # 1..MAX_PLAN_LEGS
    take_profit = models.CharField(max_length=32)  # distinct TP for this leg
    stop_loss = models.CharField(max_length=32, blank=True)  # shared SL (denormalised)
    lot_size = models.DecimalField(max_digits=6, decimal_places=2)
    order_type = models.CharField(max_length=8, default="MARKET")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLANNED)
    hold_reason = models.CharField(max_length=64, blank=True)
    # EXEC-E2a — link to the SUPPRESSED shadow ExecutionJob promoted from this leg
    # (one leg ↔ one shadow job). OneToOne ⇒ duplicate-promotion is impossible.
    execution_job = models.OneToOneField(
        "execution.ExecutionJob",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="proposed_order_leg",
    )
    # WS-B AUTO-BREAKEVEN — idempotency/audit markers for the automatic move-to-breakeven.
    # When TP1 (leg_index==1) closes, each remaining OPEN leg's SL is moved to its entry via a
    # MODIFY_POSITION job. ``breakeven_job`` links the (latest) modify job; ``breakeven_attempts``
    # bounds retries; ``breakeven_applied_at`` is the terminal, broker-VERIFIED marker (set only
    # after the modify job SUCCEEDs) so the per-minute sweep never re-issues a modify for a leg.
    breakeven_job = models.ForeignKey(
        "execution.ExecutionJob",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="breakeven_legs",
    )
    breakeven_attempts = models.PositiveSmallIntegerField(default=0)
    breakeven_applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["plan", "leg_index"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "leg_index"], name="uniq_plan_leg"),
        ]
        verbose_name = "Proposed Order Leg"
        verbose_name_plural = "Proposed Order Legs"

    def __str__(self) -> str:
        return (
            f"Leg {self.leg_index} of plan #{self.plan_id} "
            f"TP={self.take_profit} lot={self.lot_size} — NO ORDER"
        )


class PlanAuditEvent(models.Model):
    """Append-only audit for the signal → plan → leg lifecycle.

    Extends the signal audit chain (signal_intake.SignalAuditEvent) with the
    plan lifecycle, linked back to the originating approval.
    """

    class Event(models.TextChoices):
        PLAN_CREATED = "PLAN_CREATED", "Plan created (no order)"
        PLAN_HELD = "PLAN_HELD", "Plan held"
        PLAN_VOIDED = "PLAN_VOIDED", "Plan voided"
        LEG_CREATED = "LEG_CREATED", "Leg created (no order)"
        LEG_HELD = "LEG_HELD", "Leg held"
        LEG_VOIDED = "LEG_VOIDED", "Leg voided"

    event = models.CharField(max_length=32, choices=Event.choices)
    plan = models.ForeignKey(
        SignalExecutionPlan, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="audit_events",
    )
    leg = models.ForeignKey(
        ProposedOrderLeg, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="audit_events",
    )
    approval = models.ForeignKey(
        "signal_intake.PendingSignalApproval", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="plan_audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Plan Audit Event"
        verbose_name_plural = "Plan Audit Events"

    def __str__(self) -> str:
        return f"{self.event} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class PromotionAuditEvent(models.Model):
    """Append-only audit for EXEC-E2a plan → shadow-job promotion.

    Extends the signal audit chain (SignalAuditEvent → ProposalAuditEvent →
    PlanAuditEvent) with the promotion lifecycle, linked to plan/leg/job/approval
    so the full chain signal → plan → leg → suppressed job is traceable.
    """

    class Event(models.TextChoices):
        PROMOTION_CREATED = "PROMOTION_CREATED", "Promotion created (no order)"
        JOB_CREATED = "JOB_CREATED", "Shadow job created (no order)"
        PROMOTION_REJECTED = "PROMOTION_REJECTED", "Promotion rejected"

    event = models.CharField(max_length=32, choices=Event.choices)
    plan = models.ForeignKey(
        SignalExecutionPlan, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="promotion_audit_events",
    )
    leg = models.ForeignKey(
        ProposedOrderLeg, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="promotion_audit_events",
    )
    job = models.ForeignKey(
        ExecutionJob, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="promotion_audit_events",
    )
    approval = models.ForeignKey(
        "signal_intake.PendingSignalApproval", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="promotion_audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Promotion Audit Event"
        verbose_name_plural = "Promotion Audit Events"

    def __str__(self) -> str:
        return f"{self.event} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class TradeOutcomeRecord(models.Model):
    """AUTO-SHADOW-CLOSE-MONITOR — the derived, idempotent outcome of a CLOSED trade.

    Created by ``execution.close_monitor`` for each closed, not-yet-processed trade. It is
    an INTERNAL record only — it never places an order, sends Telegram, or publishes to
    WIMS. A ``WIN`` becomes an internal *delivery candidate* (``is_delivery_candidate``)
    that a FUTURE, separately-gated notification packet may consume; ``LOSS``/``BREAKEVEN``
    are recorded internally and are never delivery candidates. One record per trade (the
    OneToOne is the idempotency guarantee).
    """

    class Outcome(models.TextChoices):
        WIN = "WIN", "Win (net pnl > 0)"
        LOSS = "LOSS", "Loss (net pnl < 0)"
        BREAKEVEN = "BREAKEVEN", "Breakeven (net pnl == 0)"

    trade = models.OneToOneField(
        "trading.Trade", on_delete=models.CASCADE, related_name="outcome_record",
    )
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    net_pnl = models.DecimalField(max_digits=20, decimal_places=2)

    # WIN only → an internal candidate for the future notification/WIMS path.
    is_delivery_candidate = models.BooleanField(default=False)
    # Future notification idempotency — never set here (no notification in this packet).
    delivered = models.BooleanField(default=False)
    # AUTO-SHADOW outcome-router marker: set once the outcome_router has made its
    # WIN→candidate / LOSS→internal-only decision. Prevents re-routing (idempotency).
    routed = models.BooleanField(default=False)

    # Signal/job linkage, preserved where available (blank/null otherwise).
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    signal_source = models.CharField(max_length=64, blank=True, default="")
    execution_job = models.ForeignKey(
        "execution.ExecutionJob", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="trade_outcomes",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["outcome"]),
            models.Index(fields=["is_delivery_candidate", "delivered"]),
        ]

    def __str__(self) -> str:
        return f"{self.outcome} {self.net_pnl} (trade {self.trade_id})"


class NotificationCandidate(models.Model):
    """PROFIT-NOTIFICATION-FOUNDATION — an internal 'this WIN should be notified' candidate.

    Produced by ``execution.outcome_router`` for a WIN ``TradeOutcomeRecord`` ONLY. It is an
    INTERNAL abstraction — it holds NO transport and triggers NOTHING: no Telegram API call,
    no WIMS publish, no order. A future, separately-gated notification packet consumes PENDING
    candidates and sets ``status`` (SENT / SUPPRESSED). LOSS/BREAKEVEN never get a candidate.
    One candidate per outcome record (the OneToOne is the idempotency guarantee).
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending (awaiting transport)"
        PROCESSING = "PROCESSING", "Processing (claimed by a transport run)"
        SENT = "SENT", "Sent (dry-run rendered; nothing transmitted)"
        FAILED = "FAILED", "Failed (retryable)"
        SUPPRESSED = "SUPPRESSED", "Suppressed (never to be sent)"

    outcome_record = models.OneToOneField(
        "execution.TradeOutcomeRecord", on_delete=models.CASCADE,
        related_name="notification_candidate",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    # Correlation preserved from the outcome record (durable for the future message).
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    signal_source = models.CharField(max_length=64, blank=True, default="")
    net_pnl = models.DecimalField(max_digits=20, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set explicitly on every status transition so the dispatcher can reclaim a PROCESSING
    # row that was orphaned by a crash/DB error (timeout reaper). NOT auto_now — .update()
    # (used for the atomic claim) bypasses auto_now, so the dispatcher passes it by hand.
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"NotificationCandidate({self.status}, {self.correlation_id or '-'})"


class NotificationDelivery(models.Model):
    """TELEGRAM-TRANSPORT-FOUNDATION — append-only audit of one transport attempt.

    Written by the notification dispatcher for each delivery attempt (retries append new
    rows). It records the transport name, the attempt result, whether anything was actually
    transmitted (ALWAYS False — dry-run only), the rendered message, and the preserved
    correlation id. It never transmits anything and never mutates a trade/outcome record.
    """

    class Result(models.TextChoices):
        SENT = "SENT", "Sent (dry-run rendered)"
        FAILED = "FAILED", "Failed"

    candidate = models.ForeignKey(
        "execution.NotificationCandidate", on_delete=models.CASCADE, related_name="deliveries",
    )
    transport = models.CharField(max_length=40)
    result = models.CharField(max_length=8, choices=Result.choices)
    transmitted = models.BooleanField(
        default=False, help_text="Always False in this foundation — dry-run only, never sent.",
    )
    attempt = models.PositiveIntegerField(default=1)
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    rendered_message = models.TextField(blank=True, default="")
    detail = models.CharField(max_length=255, blank=True, default="")
    # B2: the provider (Telegram) message id of a REAL transmission — durable proof of delivery.
    # Blank for dry-run/failed rows and for historical rows (no backfill).
    provider_message_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [models.Index(fields=["result"])]

    def __str__(self) -> str:
        return f"{self.transport} {self.result} attempt={self.attempt} (cand {self.candidate_id})"


class BrokerInstrument(models.Model):
    """A tradeable instrument on a specific account's broker — a cache of the broker's MT5 symbols.

    Populated from the broker (``mt5.symbols_get`` via the bridge ``GET /mt5/symbols``) by
    ``manage.py sync_broker_instruments``. The symbol registry (``execution.broker_symbols``)
    resolves a provider (Wayond) symbol to the account's ``broker_symbol``; a provider symbol with
    no resolvable broker instrument is rejected FAIL-CLOSED. Storing the broker's real symbols
    (instead of a hardcoded allowlist) is what lets any broker-supported symbol trade, while an
    account with no synced rows falls back to the legacy baseline (see broker_symbols).
    """

    account = models.ForeignKey(
        TradingAccount, on_delete=models.CASCADE, related_name="broker_instruments",
    )
    broker_symbol = models.CharField(max_length=64)  # exact MT5 name (e.g. BTCUSD, XAUUSD+, BTCUSD.)
    base_symbol = models.CharField(max_length=64)    # normalised base for provider matching
    enabled = models.BooleanField(
        default=True, help_text="Trade-enabled / visible on the account (from MT5 symbol_info).",
    )
    metadata = models.JSONField(
        default=dict, blank=True, help_text="digits / trade_mode / contract_size / tick_size, etc.",
    )
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "broker_symbol"], name="uniq_account_broker_symbol",
            ),
        ]
        indexes = [models.Index(fields=["account", "base_symbol"])]

    def __str__(self) -> str:
        return f"{self.broker_symbol} (base={self.base_symbol}) @ acct#{self.account_id}"
