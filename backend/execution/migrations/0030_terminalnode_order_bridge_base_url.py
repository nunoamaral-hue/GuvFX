from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("execution", "0029_terminalnode_rdp_host"),
    ]

    operations = [
        migrations.AddField(
            model_name="terminalnode",
            name="order_bridge_base_url",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Per-node HOSTED order-bridge base URL (pin-enforcing). Consumed only by "
                    "order_transport for a Provider-B job; blank fails a hosted order closed "
                    "(never falls back to the global bridge)."
                ),
                max_length=255,
            ),
        ),
    ]
