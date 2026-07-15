import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """WS-E — additive: the ProviderCommand ledger (recorded follow-up trade-management commands).
    Record-only on ingest; acting is separately gated. No data change."""

    dependencies = [
        ("signal_intake", "0008_signalauditevent_auto_route_deferred"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProviderCommand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chat_id", models.CharField(blank=True, max_length=64)),
                ("message_id", models.CharField(max_length=64)),
                ("reply_to_message_id", models.CharField(blank=True, max_length=64)),
                ("command_type", models.CharField(
                    choices=[
                        ("MOVE_SL_BE", "Move SL to breakeven"),
                        ("MOVE_SL_PRICE", "Move SL to price"),
                        ("CLOSE_ALL", "Close all remaining"),
                        ("CLOSE_LEG", "Close one leg"),
                        ("CANCEL", "Cancel pending signal"),
                        ("NON_ACTIONABLE", "Non-actionable update"),
                        ("AMBIGUOUS", "Ambiguous"),
                        ("UNKNOWN", "Unknown / unclassified"),
                    ], default="UNKNOWN", max_length=20)),
                ("args", models.JSONField(blank=True, default=dict)),
                ("raw_text", models.TextField(blank=True)),
                ("status", models.CharField(
                    choices=[
                        ("PENDING", "Pending (awaiting the gated engine)"),
                        ("APPLIED", "Applied"),
                        ("REJECTED", "Rejected"),
                        ("AMBIGUOUS", "Ambiguous (no unique target plan)"),
                        ("HELD", "Held"),
                        ("SKIPPED", "Skipped (non-actionable / unknown)"),
                    ], default="PENDING", max_length=12)),
                ("processed", models.BooleanField(default=False)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("provider", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name="commands",
                    to="signal_intake.signalprovider")),
            ],
            options={"ordering": ("-created_at", "id")},
        ),
        migrations.AddConstraint(
            model_name="providercommand",
            constraint=models.UniqueConstraint(fields=("provider", "message_id"), name="uniq_provider_command"),
        ),
        migrations.AddIndex(
            model_name="providercommand",
            index=models.Index(fields=["status", "processed"], name="signal_inta_status_pc_idx"),
        ),
    ]
