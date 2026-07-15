from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive: per-SOURCE daily signal-group cap (0 = unlimited). Default = 24 (the global
    PLAN_MAX_GROUPS_PER_DAY) so every existing source is behaviour-preserving on apply."""

    dependencies = [
        ("execution", "0017_signalsourceconfig_per_source_lot_caps"),
    ]

    operations = [
        migrations.AddField(
            model_name="signalsourceconfig",
            name="daily_group_cap",
            field=models.PositiveIntegerField(
                default=24,
                help_text="Max signal groups/day for this source; 0 = unlimited.",
            ),
        ),
    ]
