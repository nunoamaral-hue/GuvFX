# P0 zero-terminal liveness recovery (2026-09-14): durable loop-safety fields for relaunching an ARMED
# workspace whose MT5 terminal has fully exited. Additive; both nullable/defaulted so existing rows are inert
# (no attempt recorded, count 0). The liveness runner is DARK by default (HOSTED_LIVENESS_RECOVERY_ENABLED off).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hosted_workspace', '0009_capability_recovery_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='liveness_recovery_at',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='liveness_recovery_count',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
