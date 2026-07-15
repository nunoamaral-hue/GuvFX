from django.db import migrations, models


class Migration(migrations.Migration):
    """WS-B2 — additive: store the Telegram message id of a real transmission on the delivery row
    (durable proof-of-delivery). No backfill; historical/dry-run/failed rows stay blank."""

    dependencies = [
        ("execution", "0019_proposedorderleg_breakeven"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationdelivery",
            name="provider_message_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
