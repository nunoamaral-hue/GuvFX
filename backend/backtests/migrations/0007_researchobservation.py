"""
B14 — Research Knowledge Base: ResearchObservation model.

Stores individual research observations to build long-term confidence metrics.
Research Mode only — no live execution side effects.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backtests", "0006_packet_c2_execution_candidate"),
    ]

    operations = [
        migrations.CreateModel(
            name="ResearchObservation",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("symbol", models.CharField(db_index=True, max_length=32)),
                ("template", models.CharField(db_index=True, max_length=64)),
                ("timeframe", models.CharField(db_index=True, max_length=20)),
                (
                    "parameters",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Strategy parameters used for this observation.",
                    ),
                ),
                (
                    "research_score",
                    models.IntegerField(
                        default=0,
                        help_text="Composite research score (0-100).",
                    ),
                ),
                (
                    "robustness_label",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="STRONG / PROMISING / WATCHLIST / WEAK",
                        max_length=20,
                    ),
                ),
                ("profit_factor", models.FloatField(default=0.0)),
                (
                    "max_drawdown",
                    models.FloatField(
                        default=0.0, help_text="Max drawdown as pct."
                    ),
                ),
                ("net_profit", models.FloatField(default=0.0)),
                ("total_return_pct", models.FloatField(default=0.0)),
                ("win_rate", models.FloatField(default=0.0)),
                ("total_trades", models.IntegerField(default=0)),
                ("expectancy", models.FloatField(default=0.0)),
                (
                    "regime_at_observation",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Market regime at observation time (BULL/BEAR/SIDEWAYS).",
                        max_length=20,
                    ),
                ),
                (
                    "walk_forward_degradation",
                    models.FloatField(
                        blank=True,
                        help_text="Walk-forward degradation pct (null if not run).",
                        null=True,
                    ),
                ),
                (
                    "walk_forward_robust",
                    models.BooleanField(
                        blank=True,
                        help_text="Walk-forward robustness flag.",
                        null=True,
                    ),
                ),
                ("bar_count", models.IntegerField(default=0)),
                (
                    "data_quality_status",
                    models.CharField(blank=True, default="OK", max_length=20),
                ),
                (
                    "source",
                    models.CharField(
                        default="strategy_lab",
                        help_text="Which endpoint created this: strategy_lab, research_matrix, optimise, etc.",
                        max_length=40,
                    ),
                ),
                (
                    "observed_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
            ],
            options={
                "ordering": ["-observed_at"],
            },
        ),
        migrations.AddIndex(
            model_name="researchobservation",
            index=models.Index(
                fields=["symbol", "template", "timeframe"],
                name="ro_sym_tmpl_tf_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="researchobservation",
            index=models.Index(
                fields=["research_score"],
                name="ro_score_idx",
            ),
        ),
    ]
