from django.db import migrations, models


class Migration(migrations.Migration):
    """WS-E — additive: per-source opt-in for the provider trade-management command engine.
    Default False (deploy-dark); a source acts on follow-up commands only when this AND the global
    PROVIDER_COMMANDS_ENABLED env gate are on."""

    dependencies = [
        ("execution", "0020_notificationdelivery_provider_message_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="signalsourceconfig",
            name="command_engine_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
