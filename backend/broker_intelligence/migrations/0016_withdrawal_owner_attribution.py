# Owner-attributed, account-optional Withdrawal (ADR-0050, WV1 §3/§4).
#
# DARK & additive. NOT applied to production (prod broker_intelligence stays at 0010; 0011-0016 are all unapplied,
# and the WV1 DB-recovery GO/NO-GO gate — an off-host verified recovery point — must clear before ANY prod migration;
# the Board demo runs on an ISOLATED demo database). All operations are auto-reversible by Django.
#
# What it does: makes Withdrawal.trading_account nullable (account-UNRESOLVED stays unresolved, never bound to an
# unrelated account); adds Withdrawal.owner (SET_NULL, mirroring BrokerEvent.owner) for attribution independent of the
# account; and SPLITS the single partial-unique idempotency index into an account-scoped one (trading_account NOT
# NULL) plus an owner-scoped one (owner, broker_reference_id WHERE trading_account IS NULL AND owner NOT NULL) to close
# the Postgres NULLS-DISTINCT hole for account-NULL rows. No Withdrawal trigger exists, so there is no trigger
# interference; existing rows all have a non-null account and a NULL owner, so both new partial indexes build cleanly.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('broker_intelligence', '0015_c3_association_safeguards'),
        ('trading', '0020_broker_server_environment_governance'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='withdrawal',
            name='uniq_withdrawal_account_reference',
        ),
        migrations.AddField(
            model_name='withdrawal',
            name='owner',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='owner_withdrawals', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='withdrawal',
            name='trading_account',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='withdrawals', to='trading.tradingaccount'),
        ),
        migrations.AddIndex(
            model_name='withdrawal',
            index=models.Index(fields=['owner', 'status'], name='broker_inte_owner_i_544bae_idx'),
        ),
        migrations.AddConstraint(
            model_name='withdrawal',
            constraint=models.UniqueConstraint(condition=models.Q(('broker_reference_id__gt', ''), ('trading_account__isnull', False)), fields=('trading_account', 'broker_reference_id'), name='uniq_withdrawal_account_reference'),
        ),
        migrations.AddConstraint(
            model_name='withdrawal',
            constraint=models.UniqueConstraint(condition=models.Q(('broker_reference_id__gt', ''), ('owner__isnull', False), ('trading_account__isnull', True)), fields=('owner', 'broker_reference_id'), name='uniq_withdrawal_owner_reference'),
        ),
    ]
