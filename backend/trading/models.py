from django.conf import settings
import uuid
from django.db import models
from django.db.models import Q


class BrokerServer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    broker_display_name = models.CharField(max_length=120)
    server_name = models.CharField(max_length=160, unique=True)

    DEMO = "demo"
    LIVE = "live"
    ENV_CHOICES = [
        (DEMO, "Demo"),
        (LIVE, "Live"),
    ]
    environment = models.CharField(max_length=10, choices=ENV_CHOICES, default=DEMO)

    aliases = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["broker_display_name", "server_name"]

    def __str__(self) -> str:
        return f"{self.broker_display_name} / {self.server_name} ({self.environment})"


class TradingAccount(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trading_accounts",
    )

    # NEW: bind this trading account to ONE MT5 instance
    mt5_instance = models.ForeignKey(
        "mt5.Mt5Instance",
        on_delete=models.PROTECT,
        related_name="trading_accounts",
        null=True,
        blank=True,
    )

    # Node-aware routing: which execution host services this account.
    # NULL means "not yet assigned to a node" (legacy accounts).
    terminal_node = models.ForeignKey(
        "execution.TerminalNode",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="trading_accounts",
        help_text="Execution host that services this account.",
    )

    name = models.CharField(max_length=100)

    # Presentation layer (stakeholder-facing). Internal identity (name / broker_name /
    # account_number) is UNCHANGED and still used by execution, routing and broker auth. Cards
    # show ``public_display_name`` when set; the account number appears publicly only when
    # ``public_show_account_number`` is true.
    public_display_name = models.CharField(
        max_length=100, blank=True,
        help_text="Stakeholder-facing account name (e.g. 'IS6FX'). Blank → fall back to name.",
    )
    public_show_account_number = models.BooleanField(
        default=False,
        help_text="Show the account number on public cards. Off → number is hidden publicly.",
    )

    broker_server = models.ForeignKey(
        BrokerServer,
        on_delete=models.PROTECT,
        related_name="trading_accounts",
        null=True,
        blank=True,
    )

    broker_name = models.CharField(max_length=100, blank=True)
    account_number = models.CharField(max_length=64)

    broker_password = models.CharField(max_length=255, blank=True)
    password_enc = models.TextField(blank=True)

    is_demo = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    # Multicurrency: account denomination (e.g. "USD", "EUR").
    # Nullable for backward compatibility; populated from MT5 account info.
    account_currency = models.CharField(
        max_length=8,
        null=True,
        blank=True,
        help_text="Account denomination currency (e.g. USD, EUR). Populated from MT5.",
    )

    # Cutover: deals with deal.time < cutover are skipped during ingest.
    # Set after wiping trades so old MT5 history doesn't re-import.
    ingest_cutover_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Skip deals older than this timestamp during trade ingest.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "broker_name", "account_number"],
                condition=Q(broker_server__isnull=True) & ~Q(broker_name=""),
                name="uniq_user_brokername_accountnumber",
            ),
            models.UniqueConstraint(
                fields=["user", "broker_server", "account_number"],
                condition=Q(broker_server__isnull=False),
                name="uniq_user_brokerserver_accountnumber",
            ),
            # ONE ACTIVE per (user, mt5_instance)
            models.UniqueConstraint(
                fields=["user", "mt5_instance"],
                condition=Q(is_active=True) & Q(mt5_instance__isnull=False),
                name="uniq_active_account_per_instance",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        server = (
            self.broker_server.server_name
            if self.broker_server_id
            else (self.broker_name or "<unknown-server>")
        )
        return f"{self.user} | {server} | {self.account_number}"

    def public_label(self) -> str:
        """The stakeholder-facing account label. Uses ``public_display_name`` when set (optionally
        with the account number when ``public_show_account_number``); otherwise falls back to the
        internal ``name`` unchanged. Presentation-only — never used for execution/routing/auth."""
        pub = (self.public_display_name or "").strip()
        if not pub:
            return self.name
        if self.public_show_account_number and self.account_number:
            return f"{pub} ({self.account_number})"
        return pub


class Trade(models.Model):
    BUY = "BUY"
    SELL = "SELL"
    SIDE_CHOICES = [
        (BUY, "Buy"),
        (SELL, "Sell"),
    ]

    STAGE_TEST = "TEST"
    STAGE_LIVE = "LIVE"
    STAGE_UNKNOWN = "UNKNOWN"
    SOURCE_STAGE_CHOICES = [
        (STAGE_TEST, "Test"),
        (STAGE_LIVE, "Live"),
        (STAGE_UNKNOWN, "Unknown"),
    ]

    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="trades",
    )

    ticket = models.CharField(max_length=64)
    symbol = models.CharField(max_length=32)
    side = models.CharField(max_length=4, choices=SIDE_CHOICES)
    volume = models.DecimalField(max_digits=12, decimal_places=2)

    open_time = models.DateTimeField()
    close_time = models.DateTimeField(null=True, blank=True)

    open_price = models.DecimalField(max_digits=20, decimal_places=5)
    close_price = models.DecimalField(max_digits=20, decimal_places=5, null=True, blank=True)

    profit = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    commission = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    swap = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    # Multicurrency: currency denomination of monetary amounts.
    # Nullable for backward compatibility; populated from MT5 account currency.
    profit_currency = models.CharField(
        max_length=8, null=True, blank=True,
        help_text="Currency of profit field (e.g. USD). Populated from MT5.",
    )
    commission_currency = models.CharField(
        max_length=8, null=True, blank=True,
        help_text="Currency of commission field. Populated from MT5.",
    )
    swap_currency = models.CharField(
        max_length=8, null=True, blank=True,
        help_text="Currency of swap field. Populated from MT5.",
    )

    magic_number = models.IntegerField(null=True, blank=True)
    comment = models.CharField(max_length=255, blank=True)
    opened_by = models.CharField(max_length=64, blank=True)

    source_stage = models.CharField(
        max_length=8,
        choices=SOURCE_STAGE_CHOICES,
        default=STAGE_UNKNOWN,
        help_text="TEST, LIVE, or UNKNOWN — inferred from job comment tag during ingest.",
    )

    # AUTO-SHADOW-CLOSE-MONITOR — linkage key back to the originating signal/execution.
    # Blank today (no real signal-originated trade exists yet); populated by a future
    # auto-demo order-ingest so the close-monitor can trace a closed trade to its signal.
    correlation_id = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("account", "ticket")
        ordering = ["-open_time"]
        indexes = [
            # TP-protection reads the newest Trade for a leg by (account, correlation comment) every
            # tick — at the watcher's 1s cadence over a growing Trade history a seq-scan would dominate
            # its DB load. This covering index makes it an index seek to the newest matching row.
            models.Index(fields=["account", "comment", "-open_time"],
                         name="trade_acct_comment_otime_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.account} | {self.ticket} | {self.symbol} {self.side}"
