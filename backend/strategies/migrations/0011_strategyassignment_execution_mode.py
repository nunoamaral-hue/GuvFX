# AUTO-SHADOW FOUNDATION — additive: per-strategy execution_mode (default MANUAL).
# (Pre-existing StrategyRuntimeEvent/State index-rename drift deliberately NOT included
# here — that is a separate known issue, not part of this additive change.)
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('strategies', '0010_allow_multiple_active_assignments_per_account'),
    ]

    operations = [
        migrations.AddField(
            model_name='strategyassignment',
            name='execution_mode',
            field=models.CharField(
                choices=[
                    ('MANUAL', 'Manual (per-signal human approval)'),
                    ('AUTO_SHADOW', 'Auto — shadow (dry-run, no order)'),
                    ('AUTO_DEMO', 'Auto — demo (future, gated)'),
                    ('AUTO_LIVE', 'Auto — live (future, gated)'),
                ],
                default='MANUAL',
                help_text='MANUAL (default) = per-signal approval; AUTO_SHADOW = config-armed dry-run.',
                max_length=16,
            ),
        ),
    ]
