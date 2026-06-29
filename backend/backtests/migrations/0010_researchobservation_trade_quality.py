"""
B18 — Trade Quality Framework: add decision-quality fields to
ResearchObservation. Additive only — existing rows get defaults
(quality_score=NULL, quality_label="", quality_buckets={}) and remain valid.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backtests", "0009_researchobservation_news_context"),
    ]

    operations = [
        migrations.AddField(
            model_name="researchobservation",
            name="quality_score",
            field=models.IntegerField(
                blank=True, db_index=True, null=True,
                help_text="Overall trade-quality score (0-100); null if not scored.",
            ),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="quality_label",
            field=models.CharField(
                blank=True, default="", max_length=16,
                help_text="Elite / Excellent / Good / Acceptable / Weak.",
            ),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="quality_buckets",
            field=models.JSONField(
                blank=True, default=dict,
                help_text="Per-bucket quality scores.",
            ),
        ),
    ]
