"""Migration 5 of 5 (Packet A): Create TerminalInteractionAudit."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("mt5", "0007_mt5session"),
    ]

    operations = [
        migrations.CreateModel(
            name="TerminalInteractionAudit",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "action_type",
                    models.CharField(
                        db_index=True,
                        help_text="Action that occurred, e.g. 'session_started', 'mt5_connected'.",
                        max_length=64,
                    ),
                ),
                ("before_state", models.CharField(blank=True, default="", max_length=32)),
                ("after_state", models.CharField(blank=True, default="", max_length=32)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                (
                    "timestamp",
                    models.DateTimeField(
                        db_index=True,
                        help_text="When the audited action occurred.",
                    ),
                ),
                (
                    "actor_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="terminal_audit_entries",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "interaction_session",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_entries",
                        to="mt5.interactionsession",
                    ),
                ),
                (
                    "mt5_session",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_entries",
                        to="mt5.mt5session",
                    ),
                ),
            ],
            options={
                "verbose_name": "Terminal Interaction Audit",
                "verbose_name_plural": "Terminal Interaction Audit Entries",
                "ordering": ["-timestamp"],
            },
        ),
        migrations.AddIndex(
            model_name="terminalinteractionaudit",
            index=models.Index(
                fields=["interaction_session", "timestamp"],
                name="idx_tia_isession_ts",
            ),
        ),
        migrations.AddIndex(
            model_name="terminalinteractionaudit",
            index=models.Index(
                fields=["action_type", "timestamp"],
                name="idx_tia_action_ts",
            ),
        ),
    ]
