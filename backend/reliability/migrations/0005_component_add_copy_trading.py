"""ADR-0074: add the COPY_TRADING alert/health component (choices-only; no DB schema change).

State-only AlterField of the ``component`` choices on the reliability models that use ``Component.CHOICES``.
Adding a choice is not enforced at the database level, so this migration makes no schema change and is
compatible with the currently-deployed backend. Scoped deliberately to the component-choice change only — the
unrelated pre-existing index-name drift on ``soaksnapshot`` is NOT bundled here (no drive-by changes)."""
from django.db import migrations, models

_CHOICES = [
    ("MT5_TERMINAL", "MT5 terminal"),
    ("MT5_BROKER", "MT5 broker connection"),
    ("SNAPSHOT_FEED", "Market snapshot feed"),
    ("INGEST_WORKER", "Trade ingest worker"),
    ("VALIDATE_WORKER", "Validate worker"),
    ("SCHEDULER_H1", "H1 scheduler"),
    ("SCHEDULER_H4", "H4 scheduler"),
    ("SCHEDULER_M5", "M5 scheduler"),
    ("EXECUTION_PIPELINE", "Execution pipeline"),
    ("BACKEND_DB", "Backend database"),
    ("COPY_TRADING", "Copy trading"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("reliability", "0004_brokeraccounthealth_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="alertevent",
            name="component",
            field=models.CharField(choices=_CHOICES, max_length=32),
        ),
        migrations.AlterField(
            model_name="componenthealth",
            name="component",
            field=models.CharField(choices=_CHOICES, max_length=32),
        ),
        migrations.AlterField(
            model_name="recoveryattempt",
            name="component",
            field=models.CharField(blank=True, choices=_CHOICES, max_length=32),
        ),
        migrations.AlterField(
            model_name="recoveryrecommendation",
            name="component",
            field=models.CharField(choices=_CHOICES, max_length=32),
        ),
    ]
