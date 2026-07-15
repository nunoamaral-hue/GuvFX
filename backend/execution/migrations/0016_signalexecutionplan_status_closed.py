from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive choice: SignalExecutionPlan.Status gains CLOSED.

    A plan is moved PROMOTED -> CLOSED by ``close_monitor.resolve_completed_plans`` once every
    leg is conclusively settled, freeing its concurrency/exposure slot (CLOSED is excluded from
    the risk gates' active-plan set). Choices are validation metadata only, so this AlterField is
    a no-op at the DB level — it exists solely to keep model and migration state consistent.
    """

    dependencies = [
        ("execution", "0015_brokerinstrument"),
    ]

    operations = [
        migrations.AlterField(
            model_name="signalexecutionplan",
            name="status",
            field=models.CharField(
                choices=[
                    ("PLANNED", "Planned (no order placed)"),
                    ("HELD", "Held (data/safety)"),
                    ("VOIDED", "Voided"),
                    ("SUPERSEDED", "Superseded"),
                    ("PROMOTED", "Promoted to shadow jobs (no order placed)"),
                    ("CLOSED", "Closed (all positions resolved)"),
                ],
                default="PLANNED",
                max_length=16,
            ),
        ),
    ]
