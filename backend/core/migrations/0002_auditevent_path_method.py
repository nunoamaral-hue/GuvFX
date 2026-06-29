# Migration to add path and method fields to AuditEvent
# These fields capture HTTP request context for audit logging

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='auditevent',
            name='path',
            field=models.CharField(
                blank=True,
                default='',
                help_text="Request path (e.g., '/api/strategies/')",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='auditevent',
            name='method',
            field=models.CharField(
                blank=True,
                default='',
                help_text='HTTP method (GET, POST, PUT, DELETE, etc.)',
                max_length=12,
            ),
        ),
    ]
