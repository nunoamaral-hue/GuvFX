from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive: stakeholder-facing presentation fields on TradingAccount. Defaults preserve
    existing behaviour (blank public name → fall back to the internal name; number hidden)."""

    dependencies = [
        ("trading", "0008_trade_correlation_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="tradingaccount",
            name="public_display_name",
            field=models.CharField(
                blank=True, max_length=100,
                help_text="Stakeholder-facing account name (e.g. 'IS6FX'). Blank → fall back to name.",
            ),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="public_show_account_number",
            field=models.BooleanField(
                default=False,
                help_text="Show the account number on public cards. Off → number is hidden publicly.",
            ),
        ),
    ]
