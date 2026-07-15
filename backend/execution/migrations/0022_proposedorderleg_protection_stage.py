from django.db import migrations, models


def set_stage_for_applied(apps, schema_editor):
    """A leg that already had breakeven applied is at the BREAKEVEN stage (behaviour-preserving)."""
    Leg = apps.get_model("execution", "ProposedOrderLeg")
    Leg.objects.filter(breakeven_applied_at__isnull=False).update(protection_stage="BREAKEVEN")


class Migration(migrations.Migration):
    """WS-INCREMENTAL-TP-PROTECTION — additive per-leg protection stage (INITIAL/BREAKEVEN/TP2_LOCKED).
    Data step backfills legs that already reached breakeven so the state machine treats them correctly."""

    dependencies = [
        ("execution", "0021_signalsourceconfig_command_engine_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="proposedorderleg",
            name="protection_stage",
            field=models.CharField(default="INITIAL", max_length=16),
        ),
        migrations.RunPython(set_stage_for_applied, migrations.RunPython.noop),
    ]
