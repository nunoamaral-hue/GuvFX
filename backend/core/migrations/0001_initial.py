# Generated migration for AuditEvent model

import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('event_type', models.CharField(choices=[
                    ('AUTH_LOGIN', 'User Login'),
                    ('AUTH_LOGOUT', 'User Logout'),
                    ('AUTH_REFRESH', 'Token Refresh'),
                    ('AUTH_FAILED', 'Authentication Failed'),
                    ('STRATEGY_CREATED', 'Strategy Created'),
                    ('STRATEGY_UPDATED', 'Strategy Updated'),
                    ('STRATEGY_DELETED', 'Strategy Deleted'),
                    ('BACKTEST_CONFIG_CREATED', 'Backtest Config Created'),
                    ('BACKTEST_RUN_CREATED', 'Backtest Run Created'),
                    ('BACKTEST_PROCESSED', 'Backtests Processed'),
                    ('ACCOUNT_LINKED', 'Account Linked'),
                    ('ACCOUNT_UNLINKED', 'Account Unlinked'),
                    ('ASSIGNMENT_CREATED', 'Strategy Assigned'),
                    ('ASSIGNMENT_UPDATED', 'Assignment Updated'),
                    ('ASSIGNMENT_REMOVED', 'Assignment Removed'),
                    ('EXECUTION_ENABLE_ATTEMPT', 'Execution Enable Attempted'),
                    ('EXECUTION_DISABLE_ATTEMPT', 'Execution Disable Attempted'),
                    ('EXECUTION_KILL_ATTEMPT', 'Kill Switch Attempted'),
                    ('RATE_LIMIT_EXCEEDED', 'Rate Limit Exceeded'),
                ], db_index=True, max_length=64)),
                ('severity', models.CharField(choices=[
                    ('DEBUG', 'Debug'),
                    ('INFO', 'Info'),
                    ('WARN', 'Warning'),
                    ('ERROR', 'Error'),
                    ('CRITICAL', 'Critical'),
                ], db_index=True, default='INFO', max_length=16)),
                ('entity_type', models.CharField(blank=True, db_index=True, default='', help_text="Type of entity (e.g., 'strategy', 'account', 'backtest_config')", max_length=64)),
                ('entity_id', models.CharField(blank=True, db_index=True, help_text='ID of the entity (string to support UUIDs and integers)', max_length=128, null=True)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.TextField(blank=True, default='')),
                ('metadata', models.JSONField(blank=True, default=dict, help_text='Additional event context. Must NOT contain sensitive data.')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_events', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Audit Event',
                'verbose_name_plural': 'Audit Events',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['event_type', 'created_at'], name='core_audite_event_t_a3e5c7_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['user', 'created_at'], name='core_audite_user_id_c8b2f1_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['entity_type', 'entity_id'], name='core_audite_entity__d5f9e3_idx'),
        ),
    ]
