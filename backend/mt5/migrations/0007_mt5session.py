"""Migration 4 of 5 (Packet A): Create MT5Session."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mt5", "0006_interactionsession"),
    ]

    operations = [
        migrations.CreateModel(
            name="MT5Session",
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
                    "adapter_type",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Adapter implementation type, e.g. 'guacamole_rdp', 'direct_wine'.",
                        max_length=64,
                    ),
                ),
                (
                    "adapter_session_id",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Adapter-assigned session identifier.",
                        max_length=255,
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        default="",
                        max_length=32,
                    ),
                ),
                ("launch_issued_at", models.DateTimeField(blank=True, null=True)),
                ("connected_at", models.DateTimeField(blank=True, null=True)),
                ("suspended_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("last_heartbeat_at", models.DateTimeField(blank=True, null=True)),
                (
                    "launch_descriptor_snapshot",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Snapshot of launch parameters at session creation time.",
                    ),
                ),
                (
                    "adapter_metadata",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Adapter-specific metadata (connection params, etc.).",
                    ),
                ),
                ("failure_reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "interaction_session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mt5_sessions",
                        to="mt5.interactionsession",
                    ),
                ),
                (
                    "terminal_binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mt5_sessions",
                        to="mt5.terminalbinding",
                    ),
                ),
            ],
            options={
                "verbose_name": "MT5 Session",
                "verbose_name_plural": "MT5 Sessions",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="mt5session",
            index=models.Index(
                fields=["interaction_session", "state"],
                name="idx_mt5session_isession_state",
            ),
        ),
    ]
