from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """WS-B AUTO-BREAKEVEN — additive. Adds the MODIFY_POSITION job type (choices-only,
    behaviour-preserving) and the three idempotency/audit markers on ProposedOrderLeg used by
    the automatic move-to-breakeven sweep. No data change; every existing leg gets
    breakeven_job=NULL, breakeven_attempts=0, breakeven_applied_at=NULL (i.e. "no breakeven yet")."""

    dependencies = [
        ("execution", "0018_signalsourceconfig_daily_group_cap"),
    ]

    operations = [
        migrations.AlterField(
            model_name="executionjob",
            name="job_type",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("TEST_CONNECTION", "Test connection"),
                    ("OPEN_TRADE", "Open trade"),
                    ("CLOSE_TRADE", "Close trade"),
                    ("SYNC_POSITIONS", "Sync positions"),
                    ("PLACE_TEST_ORDER", "Place test order (demo)"),
                    ("PLACE_ORDER", "Place order (strategy signal)"),
                    ("MODIFY_POSITION", "Modify position SL/TP (breakeven)"),
                    ("PLACE_ORDER_SHADOW", "Place order (shadow / suppressed — no order)"),
                ],
            ),
        ),
        migrations.AddField(
            model_name="proposedorderleg",
            name="breakeven_job",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="breakeven_legs",
                to="execution.executionjob",
            ),
        ),
        migrations.AddField(
            model_name="proposedorderleg",
            name="breakeven_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="proposedorderleg",
            name="breakeven_applied_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
