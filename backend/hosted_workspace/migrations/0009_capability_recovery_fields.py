# AJ#6.3 Shape-3: durable loop-safety fields for post-login MT5 automation-capability recovery.
# Additive; both nullable/defaulted so existing rows are inert (no attempt recorded, count 0). The recovery
# runner is DARK by default (HOSTED_CAPABILITY_RECOVERY_ENABLED off).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hosted_workspace', '0008_hostedmt5workspace_execution_authorized_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='capability_recovery_at',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='capability_recovery_count',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
