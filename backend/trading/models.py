from django.conf import settings
import uuid
from django.db import models
from django.db.models import Q


class BrokerServer(models.Model):
    """A directory of broker/MT5 server names used for autocomplete and validation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    broker_display_name = models.CharField(max_length=120, help_text="Broker name shown to users (e.g. TradersWay)")
    server_name = models.CharField(max_length=160, unique=True, help_text="Exact MT5 server name (e.g. TradersWay-Demo)")

    DEMO = "demo"
    LIVE = "live"
    ENV_CHOICES = [
        (DEMO, "Demo"),
        (LIVE, "Live"),
    ]
    environment = models.CharField(max_length=10, choices=ENV_CHOICES, default=DEMO)

    aliases = models.JSONField(default=list, blank=True, help_text="Alternative names users might type (e.g. traders way, tw)")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["broker_display_name", "server_name"]

    def __str__(self) -> str:
        return f"{self.broker_display_name} / {self.server_name} ({self.environment})"

class TradingAccount(models.Model):
    """
    A trading account linked to a GuvFX user (e.g., MT5 account at a broker).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trading_accounts",
    )

    name = models.CharField(max_length=100, help_text="Friendly name (e.g. Main MT5)")

    # NEW: normalized broker/server directory reference (preferred)
    broker_server = models.ForeignKey(
        BrokerServer,
        on_delete=models.PROTECT,
        related_name="trading_accounts",
        null=True,
        blank=True,
        help_text="Selected BrokerServer record (preferred).",
    )

    # Legacy / fallback: free-text broker or server name (deprecated; keep for backward compatibility)
    broker_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="DEPRECATED: free-text broker or server name. Prefer broker_server.server_name",
    )
    account_number = models.CharField(max_length=64, help_text="External account ID / login")

    # Legacy / fallback: plaintext password (deprecated). Prefer storing encrypted password_enc.
    broker_password = models.CharField(
        max_length=255,
        blank=True,
        help_text="DEPRECATED: plaintext password for the broker platform account. Prefer password_enc.",
    )

    # NEW: encrypted password storage (MVP: store encrypted string; later rotate to KMS/envelope)
    password_enc = models.TextField(
        blank=True,
        help_text="Encrypted password for MT5/broker login. Do not store plaintext.",
    )

    is_demo = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

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
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        server = (
            self.broker_server.server_name
            if self.broker_server_id
            else (self.broker_name or "<unknown-server>")
        )
        return f"{self.user} | {server} | {self.account_number}"

class Trade(models.Model):
    """
    A single trade executed on a TradingAccount.

    Initially this can be created manually or via admin/import;
    later it will be fed by broker/MT5 integrations.
    """

    BUY = "BUY"
    SELL = "SELL"
    SIDE_CHOICES = [
        (BUY, "Buy"),
        (SELL, "Sell"),
    ]

    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="trades",
    )
    ticket = models.CharField(max_length=64, help_text="Broker trade ticket / ID")
    symbol = models.CharField(max_length=32)
    side = models.CharField(max_length=4, choices=SIDE_CHOICES)
    volume = models.DecimalField(max_digits=12, decimal_places=2, help_text="Lots or units")
    open_time = models.DateTimeField()
    close_time = models.DateTimeField(null=True, blank=True)

    open_price = models.DecimalField(max_digits=20, decimal_places=5)
    close_price = models.DecimalField(max_digits=20, decimal_places=5, null=True, blank=True)

    profit = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    commission = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    swap = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    magic_number = models.IntegerField(null=True, blank=True)
    comment = models.CharField(max_length=255, blank=True)
    opened_by = models.CharField(
        max_length=64,
        blank=True,
        help_text="Strategy/bot name or identifier",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("account", "ticket")
        ordering = ["-open_time"]

    def __str__(self) -> str:
        return f"{self.account} | {self.ticket} | {self.symbol} {self.side}"