"""
Migration: Add StrategyRuntimeState and StrategyRuntimeEvent models.

These support the ALTS / SCE / future engines with:
  - Per-assignment+symbol runtime state (daily R, trade counts, regime, cooldown)
  - Append-only audit event log for signal evaluation diagnostics
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("strategies", "0008_strategyassignment_stage"),
    ]

    operations = [
        # StrategyRuntimeState
        migrations.CreateModel(
            name="StrategyRuntimeState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "strategy_key",
                    models.CharField(
                        help_text="Engine identifier / template slug, e.g. 'adaptive-liquidity-trap-scalper'.",
                        max_length=64,
                    ),
                ),
                (
                    "symbol",
                    models.CharField(
                        help_text="Trading pair, e.g. 'EURUSD'.",
                        max_length=20,
                    ),
                ),
                (
                    "last_eval_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Timestamp of the last signal evaluation for this key+symbol.",
                        null=True,
                    ),
                ),
                (
                    "paused_until",
                    models.DateTimeField(
                        blank=True,
                        help_text="If set, signals are blocked until this timestamp (loss-streak cooldown, etc.).",
                        null=True,
                    ),
                ),
                (
                    "pause_reason",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Reason code for the current pause (e.g. LOSS_STREAK_PAUSE).",
                        max_length=64,
                    ),
                ),
                (
                    "regime_blob",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Engine-specific state: bias, regime, last BOS, etc.",
                    ),
                ),
                (
                    "daily_r_pnl",
                    models.DecimalField(
                        decimal_places=4,
                        default=0,
                        help_text="Cumulative R P&L for the current day (negative = loss).",
                        max_digits=10,
                    ),
                ),
                (
                    "daily_trade_count",
                    models.IntegerField(
                        default=0,
                        help_text="Number of signals fired today.",
                    ),
                ),
                (
                    "daily_reset_date",
                    models.DateField(
                        blank=True,
                        help_text="Date (UTC) of last daily counter reset.",
                        null=True,
                    ),
                ),
                (
                    "weekly_r_pnl",
                    models.DecimalField(
                        decimal_places=4,
                        default=0,
                        help_text="Cumulative R P&L for the current week (resets Monday 00:00 UTC).",
                        max_digits=10,
                    ),
                ),
                (
                    "weekly_reset_date",
                    models.DateField(
                        blank=True,
                        help_text="Monday of the current tracking week.",
                        null=True,
                    ),
                ),
                (
                    "consecutive_losses",
                    models.IntegerField(
                        default=0,
                        help_text="Running count of consecutive losing trades (resets on a win).",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True),
                ),
                (
                    "assignment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runtime_states",
                        to="strategies.strategyassignment",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["assignment", "strategy_key"],
                        name="strategies_st_assignm_idx_key",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["assignment", "strategy_key", "symbol"],
                        name="uniq_runtime_state_per_key_symbol",
                    ),
                ],
            },
        ),
        # StrategyRuntimeEvent
        migrations.CreateModel(
            name="StrategyRuntimeEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "strategy_key",
                    models.CharField(
                        help_text="Engine identifier / template slug.",
                        max_length=64,
                    ),
                ),
                (
                    "symbol",
                    models.CharField(
                        help_text="Trading pair.",
                        max_length=20,
                    ),
                ),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("SIGNAL_FIRED", "Signal Fired"),
                            ("SIGNAL_SKIPPED", "Signal Skipped"),
                            ("RISK_THROTTLED", "Risk Throttled"),
                            ("REGIME_CHANGED", "Regime Changed"),
                            ("COOLDOWN_STARTED", "Cooldown Started"),
                            ("COOLDOWN_ENDED", "Cooldown Ended"),
                            ("DAILY_RESET", "Daily Reset"),
                            ("ERROR", "Error"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "reason_code",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Structured reason code from PART G enum (e.g. DAILY_LOSS_CAP).",
                        max_length=64,
                    ),
                ),
                (
                    "payload",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Full diagnostic payload (bar data, indicator values, etc.).",
                    ),
                ),
                (
                    "bar_close_time",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="ISO UTC timestamp of the bar close that triggered this event.",
                        max_length=32,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                (
                    "assignment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runtime_events",
                        to="strategies.strategyassignment",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["assignment", "strategy_key", "symbol"],
                        name="strategies_ev_assignm_key_sym",
                    ),
                    models.Index(
                        fields=["event_type", "created_at"],
                        name="strategies_ev_type_created",
                    ),
                    models.Index(
                        fields=["strategy_key", "created_at"],
                        name="strategies_ev_key_created",
                    ),
                ],
            },
        ),
    ]
