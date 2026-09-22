# Phase A — durable strategy ownership: per-StrategyAssignment MT5 magic number.
# ADDITIVE / nullable only (no NOT-NULL, no DB default). Ships DARK; nothing reads
# or writes these fields until STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED is set.
# NOTE: deliberately excludes the unrelated pre-existing RenameIndex drift on
# strategyruntimeevent/strategyruntimestate that makemigrations would bundle here
# (no drive-by refactors; index-name reconciliation is out of this packet's scope).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('strategies', '0013_assignment_leg_sizing'),
    ]

    operations = [
        migrations.AddField(
            model_name='strategyassignment',
            name='magic_number',
            field=models.IntegerField(
                blank=True, null=True,
                help_text='GuvFX MT5 magic for this assignment (1e9 + id); immutable once set.'),
        ),
        migrations.AddField(
            model_name='strategyassignment',
            name='magic_number_allocated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='strategyassignment',
            constraint=models.UniqueConstraint(
                condition=models.Q(('magic_number__isnull', False)),
                fields=('magic_number',),
                name='uniq_assignment_magic_number'),
        ),
        migrations.AddConstraint(
            model_name='strategyassignment',
            constraint=models.CheckConstraint(
                condition=models.Q(('magic_number__isnull', True), ('magic_number__gte', 1000000000), _connector='OR'),
                name='assignment_magic_in_band'),
        ),
    ]
