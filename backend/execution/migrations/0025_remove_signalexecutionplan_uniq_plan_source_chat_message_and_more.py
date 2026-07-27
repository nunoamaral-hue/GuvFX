"""ADR-0020 — Trusted Beta multi-account routing: SignalExecutionPlan.approval OneToOne -> ForeignKey.

DATA-PRESERVING: no row is created, deleted, or rewritten. Only field metadata + constraints change:
  * approval: OneToOne (unique approval_id) -> ForeignKey (drops the single-column unique index).
  * uniq_plan_source_chat_message -> uniq_plan_source_chat_message_account (adds the account dimension).
  * NEW uniq_plan_approval_account — the "one plan per (approval, account)" invariant.

Existing rows each have exactly one plan per approval on one account, so both new constraints hold with
zero conflicts. REVERSIBLE: the reverse migration re-adds the OneToOne unique on approval_id, which
succeeds while no approval has >1 plan — guaranteed while MULTI_ACCOUNT_ROUTING_ENABLED has never been
enabled in that environment (the Class-B gate controls enablement).
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('execution', '0024_protection_stage_db_default'),
        ('signal_intake', '0009_providercommand'),
        ('trading', '0011_trade_close_ingested_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='signalexecutionplan',
            name='uniq_plan_source_chat_message',
        ),
        migrations.AlterField(
            model_name='signalexecutionplan',
            name='approval',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='execution_plan', to='signal_intake.pendingsignalapproval'),
        ),
        migrations.AddConstraint(
            model_name='signalexecutionplan',
            constraint=models.UniqueConstraint(fields=('source', 'chat_id', 'message_id', 'account'), name='uniq_plan_source_chat_message_account'),
        ),
        migrations.AddConstraint(
            model_name='signalexecutionplan',
            constraint=models.UniqueConstraint(fields=('approval', 'account'), name='uniq_plan_approval_account'),
        ),
    ]
