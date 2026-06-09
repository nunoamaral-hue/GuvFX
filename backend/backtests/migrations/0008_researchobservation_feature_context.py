"""
B16 — Feature Framework: add normalised market-context fields to
ResearchObservation. Additive only — existing rows get defaults and
remain valid (feature_context={}, empty states, position_size_warning=False).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backtests", "0007_researchobservation"),
    ]

    operations = [
        migrations.AddField(
            model_name="researchobservation",
            name="feature_context",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Full normalised market-context feature dict (trend/volatility/session/structure/normalisation).",
            ),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="trend_state",
            field=models.CharField(blank=True, db_index=True, default="", max_length=24),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="volatility_state",
            field=models.CharField(blank=True, db_index=True, default="", max_length=24),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="session_bucket",
            field=models.CharField(blank=True, default="", max_length=24),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="breakout_state",
            field=models.CharField(blank=True, default="", max_length=24),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="position_size_warning",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text="True if result may need position-size normalization before comparison.",
            ),
        ),
    ]
