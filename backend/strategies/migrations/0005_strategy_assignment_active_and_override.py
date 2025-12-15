# Generated manually to add assignment-level overrides and updated timestamp.

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ("strategies", "0004_normalize_style_choices"),
    ]

    operations = [
        migrations.RenameField(
            model_name="strategyassignment",
            old_name="is_enabled",
            new_name="is_active",
        ),
        migrations.AddField(
            model_name="strategyassignment",
            name="risk_per_trade_override_pct",
            field=models.DecimalField(
                max_digits=5,
                decimal_places=2,
                null=True,
                blank=True,
                help_text="Optional risk per trade override percent for this assignment.",
            ),
        ),
        migrations.AddField(
            model_name="strategyassignment",
            name="updated_at",
            field=models.DateTimeField(
                auto_now=True,
                default=timezone.now,
            ),
            preserve_default=False,
        ),
    ]
