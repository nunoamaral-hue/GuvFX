from django.conf import settings
from django.db import models
from django.db.models import Q
from trading.models import TradingAccount


class Strategy(models.Model):
    """
    A trading strategy definition in GuvFX.

    Owned by a user, can be linked to one or more TradingAccounts
    via StrategyAssignment.
    """

    STYLE_SCALPER = "SCALPER"
    STYLE_INTRADAY = "INTRADAY"
    STYLE_SWING = "SWING"
    STYLE_POSITION = "POSITION"

    STYLE_CHOICES = [
        (STYLE_SCALPER, "Scalper"),
        (STYLE_INTRADAY, "Intraday"),
        (STYLE_SWING, "Swing"),
        (STYLE_POSITION, "Position"),
    ]

    MARKET_FOREX = "FOREX"
    MARKET_INDICES = "INDICES"
    MARKET_GOLD = "GOLD"
    MARKET_CRYPTO = "CRYPTO"

    MARKET_TYPE_CHOICES = [
        (MARKET_FOREX, "Forex"),
        (MARKET_INDICES, "Indices"),
        (MARKET_GOLD, "Gold"),
        (MARKET_CRYPTO, "Crypto"),
    ]

    TIMEFRAMES = [
        ("M1", "1 minute"),
        ("M3", "3 minutes"),
        ("M5", "5 minutes"),
        ("M15", "15 minutes"),
        ("M30", "30 minutes"),
        ("H1", "1 hour"),
        ("H4", "4 hours"),
        ("D1", "Daily"),
        ("W1", "Weekly"),
    ]

    EDGE_TREND_FOLLOWING = "TREND_FOLLOWING"
    EDGE_MEAN_REVERSION = "MEAN_REVERSION"
    EDGE_BREAKOUT = "BREAKOUT"
    EDGE_NEWS = "NEWS_FUNDAMENTAL"

    EDGE_TYPE_CHOICES = [
        (EDGE_TREND_FOLLOWING, "Trend-following"),
        (EDGE_MEAN_REVERSION, "Mean reversion"),
        (EDGE_BREAKOUT, "Breakout"),
        (EDGE_NEWS, "News/fundamental"),
    ]

    SIZING_FIXED_RISK = "FIXED_RISK_PERCENT"
    SIZING_FIXED_LOT = "FIXED_LOT_SIZE"

    SIZING_MODE_CHOICES = [
        (SIZING_FIXED_RISK, "Fixed risk %"),
        (SIZING_FIXED_LOT, "Fixed lot size"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="strategies",
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    style = models.CharField(
        max_length=20,
        choices=STYLE_CHOICES,
        blank=True,
        help_text="General style of the strategy (optional).",
    )
    market_type = models.CharField(
        max_length=16,
        choices=MARKET_TYPE_CHOICES,
        blank=True,
        help_text="Primary market (Forex, indices, gold, crypto).",
    )
    symbol_universe = models.CharField(
        max_length=255,
        blank=True,
        help_text="Comma-separated symbols (e.g. 'EURUSD,GBPUSD').",
    )
    timeframe = models.CharField(
        max_length=20,
        choices=TIMEFRAMES,
        blank=True,
        help_text="Primary timeframe (e.g. H1, H4, D1).",
    )
    edge_type = models.CharField(
        max_length=32,
        choices=EDGE_TYPE_CHOICES,
        blank=True,
        help_text="Primary trade idea (edge) for the strategy.",
    )
    edge_rationale = models.CharField(
        max_length=512,
        blank=True,
        help_text="Short rationale or edge description.",
    )

    risk_per_trade_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Typical risk per trade in % of account (e.g. 1.0).",
    )
    max_drawdown_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Soft max drawdown in % for risk controls.",
    )

    magic_number = models.IntegerField(
        null=True,
        blank=True,
        help_text="Optional magic number for MT5/EA integration.",
    )

    is_active = models.BooleanField(default=True)

    sizing_mode = models.CharField(
        max_length=32,
        choices=SIZING_MODE_CHOICES,
        default=SIZING_FIXED_RISK,
        help_text="How the strategy sizes positions (risk % vs fixed lot).",
    )
    fixed_lot_size = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Fixed lot size when using fixed lot sizing mode.",
    )

    # NEW: moving averages configuration (simple example)
    ma_fast_period = models.IntegerField(
        null=True,
        blank=True,
        help_text="Fast moving average period (e.g. 20).",
    )
    ma_slow_period = models.IntegerField(
        null=True,
        blank=True,
        help_text="Slow moving average period (e.g. 50).",
    )

    MA_SMA = "SMA"
    MA_EMA = "EMA"
    MA_WMA = "WMA"

    MA_TYPE_CHOICES = [
        (MA_SMA, "Simple MA (SMA)"),
        (MA_EMA, "Exponential MA (EMA)"),
        (MA_WMA, "Weighted MA (WMA)"),
    ]

    ma_type = models.CharField(
        max_length=10,
        choices=MA_TYPE_CHOICES,
        blank=True,
        help_text="Type of moving average used for signals.",
    )

    # NEW: whether AI is allowed to auto-tune these parameters
    auto_optimize_by_ai = models.BooleanField(
        default=False,
        help_text="If true, allow AI to adjust parameters automatically.",
    )

    indicator_blocks = models.JSONField(
        default=list,
        blank=True,
        help_text="Indicator configuration blocks that power signals and filters.",
    )
    entry_rules = models.JSONField(
        default=dict,
        blank=True,
        help_text="Entry rule configuration (patterns, indicator crosses, etc.).",
    )
    sl_rules = models.JSONField(
        default=dict,
        blank=True,
        help_text="Stop-loss configuration (ATR multiples, fixed pips, etc.).",
    )
    tp_rules = models.JSONField(
        default=dict,
        blank=True,
        help_text="Take-profit configuration (RR targets, trailing rules, partials).",
    )
    filters = models.JSONField(
        default=dict,
        blank=True,
        help_text="Filters and condition settings (news, time filters, max trades).",
    )
    trade_management = models.JSONField(
        default=dict,
        blank=True,
        help_text="Trade management rules (breakeven, pyramiding, etc.).",
    )
    risk_limits = models.JSONField(
        default=dict,
        blank=True,
        help_text="Risk and money management limits.",
    )
    plan_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text="Plan, routine, and psychology metadata.",
    )

    # Human-readable logic/notes
    entry_logic = models.TextField(blank=True, help_text="Human-readable entry conditions.")
    exit_logic = models.TextField(blank=True, help_text="Human-readable exit conditions.")
    notes = models.TextField(blank=True, help_text="Additional notes about the strategy.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.owner} | {self.name}"


class StrategyAssignment(models.Model):
    """
    Links a Strategy to a TradingAccount (strategy running on that account).
    """

    strategy = models.ForeignKey(
        Strategy,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="strategy_assignments",
    )

    is_active = models.BooleanField(default=True)
    risk_per_trade_override_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Overrides the strategy's default risk per trade percentage for this account.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("strategy", "account")
        ordering = ["-created_at"]
        constraints = [
            # One active assignment per MT5 instance (per user implicitly)
            models.UniqueConstraint(
                fields=["account"],
                condition=Q(is_active=True),  # CI: removed join-based constraint (account__mt5_instance__isnull)
                name="uniq_active_strategy_assignment_per_instance",
            ),
        ]

    def clean(self):
        # Strategy should only be active on an active account
        if self.is_active and self.account and (not self.account.is_active):
            from django.core.exceptions import ValidationError
            raise ValidationError({"is_active": "Cannot activate a strategy on an inactive TradingAccount."})

    def __str__(self) -> str:
        return f"{self.strategy} -> {self.account}"

class StrategyChangeLog(models.Model):
    """
    Records changes to Strategy settings, either manual edits or AI auto-tunes.
    """

    SOURCE_MANUAL = "MANUAL"
    SOURCE_AI_AUTO_TUNE = "AI_AUTO_TUNE"

    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual edit"),
        (SOURCE_AI_AUTO_TUNE, "AI auto-tune"),
    ]

    strategy = models.ForeignKey(
        Strategy,
        on_delete=models.CASCADE,
        related_name="change_logs",
    )
    source = models.CharField(
        max_length=32,
        choices=SOURCE_CHOICES,
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="strategy_change_logs",
        help_text="User who made the change; null if AI only.",
    )
    before_settings = models.JSONField(null=True, blank=True)
    after_settings = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.strategy.name} | {self.source} @ {self.created_at}"

