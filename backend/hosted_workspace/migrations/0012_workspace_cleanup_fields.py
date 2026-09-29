# Remove Broker Account STAGE-2 physical decommission (2026-09-29): durable, retryable cleanup-lifecycle fields.
# Additive; all nullable/defaulted so existing rows are inert (cleanup_state=NOT_REQUIRED, no attempt, count 0).
# The cleanup runner (run_workspace_cleanup) is gated on the existing hosted observation cron + master flag.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hosted_workspace', '0011_hostedmt5workspace_proj_margin_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='cleanup_state',
            field=models.CharField(default='NOT_REQUIRED', max_length=20),
        ),
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='cleanup_attempts',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='cleanup_next_retry_at',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name='hostedmt5workspace',
            name='cleanup_last_reason',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
    ]
