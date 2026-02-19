from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("strategies", "0007_strategy_uniq_owner_magic_number_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategyassignment",
            name="stage",
            field=models.CharField(
                choices=[("TEST", "Test"), ("LIVE", "Live")],
                default="TEST",
                help_text="TEST = dry-run/testing (excluded from auto scheduler), LIVE = real auto-evaluation.",
                max_length=8,
            ),
        ),
    ]
