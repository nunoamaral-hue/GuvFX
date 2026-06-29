"""
B16.5 — Economic Event Framework: add factual economic-event context fields
to ResearchObservation. Additive only — existing rows get defaults
(news_impact="NONE", event_relevance="NONE", minutes_to_event=NULL) and
remain valid.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backtests", "0008_researchobservation_feature_context"),
    ]

    operations = [
        migrations.AddField(
            model_name="researchobservation",
            name="news_impact",
            field=models.CharField(
                blank=True, db_index=True, default="NONE", max_length=12,
                help_text="Nearest relevant event impact: NONE/LOW/MEDIUM/HIGH.",
            ),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="news_type",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="news_currency",
            field=models.CharField(blank=True, default="", max_length=8),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="event_relevance",
            field=models.CharField(
                blank=True, db_index=True, default="NONE", max_length=12,
                help_text="Event relevance to this symbol: NONE/LOW/MEDIUM/HIGH.",
            ),
        ),
        migrations.AddField(
            model_name="researchobservation",
            name="minutes_to_event",
            field=models.IntegerField(
                blank=True, null=True,
                help_text="Minutes to the nearest relevant upcoming event (null if none).",
            ),
        ),
    ]
