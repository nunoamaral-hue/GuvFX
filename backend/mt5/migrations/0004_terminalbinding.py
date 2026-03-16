"""Migration 1 of 5 (Packet A): Create TerminalBinding."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution", "0003_terminal_node"),
        ("mt5", "0003_remove_mt5instance_created_at_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TerminalBinding",
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
                    "terminal_identifier",
                    models.CharField(
                        help_text="Unique identifier for this terminal slot on the node.",
                        max_length=255,
                    ),
                ),
                (
                    "mt5_account_login",
                    models.CharField(
                        help_text="MT5 account login number bound to this terminal.",
                        max_length=64,
                    ),
                ),
                (
                    "environment_type",
                    models.CharField(
                        help_text="Environment type, e.g. 'demo', 'live'.",
                        max_length=32,
                    ),
                ),
                (
                    "terminal_label",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Human-friendly label for this terminal binding.",
                        max_length=128,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("available", "Available"),
                            ("launching", "Launching"),
                            ("active", "Active"),
                            ("suspended", "Suspended"),
                            ("maintenance", "Maintenance"),
                            ("locked", "Locked"),
                        ],
                        db_index=True,
                        default="available",
                        max_length=16,
                    ),
                ),
                ("occupied_since", models.DateTimeField(blank=True, null=True)),
                ("last_heartbeat", models.DateTimeField(blank=True, null=True)),
                ("supports_shared_view", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "terminal_node",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="terminal_bindings",
                        to="execution.terminalnode",
                    ),
                ),
            ],
            options={
                "verbose_name": "Terminal Binding",
                "verbose_name_plural": "Terminal Bindings",
                "ordering": ["terminal_node", "terminal_identifier"],
            },
        ),
        migrations.AddIndex(
            model_name="terminalbinding",
            index=models.Index(
                fields=["terminal_node", "status"],
                name="idx_binding_node_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="terminalbinding",
            constraint=models.UniqueConstraint(
                fields=("terminal_node", "terminal_identifier"),
                name="uniq_binding_per_node_identifier",
            ),
        ),
    ]
