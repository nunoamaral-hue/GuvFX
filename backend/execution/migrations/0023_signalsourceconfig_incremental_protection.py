from django.db import migrations, models


class Migration(migrations.Migration):
    """WS-INCREMENTAL-TP-PROTECTION — additive per-source opt-in for the TP2-lock stage. Default OFF:
    Wayond keeps its existing state-1 breakeven behaviour; ti_signals is enabled at deploy time."""

    dependencies = [
        ("execution", "0022_proposedorderleg_protection_stage"),
    ]

    operations = [
        migrations.AddField(
            model_name="signalsourceconfig",
            name="incremental_protection_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
