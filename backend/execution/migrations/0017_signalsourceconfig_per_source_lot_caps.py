from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive: per-SOURCE sizing ceilings on SignalSourceConfig. Defaults equal the global
    constants (0.02 / 0.06), so every existing row is behaviour-preserving on apply — only a
    source explicitly raised (e.g. ti_signals) sizes larger."""

    dependencies = [
        ("execution", "0016_signalexecutionplan_status_closed"),
    ]

    operations = [
        migrations.AddField(
            model_name="signalsourceconfig",
            name="max_lot_per_leg",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.02"), max_digits=6),
        ),
        migrations.AddField(
            model_name="signalsourceconfig",
            name="max_total_lot",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.06"), max_digits=6),
        ),
    ]
