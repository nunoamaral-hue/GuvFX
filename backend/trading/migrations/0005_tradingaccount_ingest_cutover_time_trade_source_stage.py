from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0004_tradingaccount_mt5_instance_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="tradingaccount",
            name="ingest_cutover_time",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Skip deals older than this timestamp during trade ingest.",
            ),
        ),
        migrations.AddField(
            model_name="trade",
            name="source_stage",
            field=models.CharField(
                choices=[("TEST", "Test"), ("LIVE", "Live"), ("UNKNOWN", "Unknown")],
                default="UNKNOWN",
                help_text="TEST, LIVE, or UNKNOWN — inferred from job comment tag during ingest.",
                max_length=8,
            ),
        ),
    ]
