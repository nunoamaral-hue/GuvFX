from django.db import migrations


def forwards(apps, schema_editor):
    Strategy = apps.get_model("strategies", "Strategy")
    Strategy.objects.filter(style="SCALPING").update(style="SCALPER")
    Strategy.objects.filter(style="DAY").update(style="INTRADAY")


def backwards(apps, schema_editor):
    Strategy = apps.get_model("strategies", "Strategy")
    Strategy.objects.filter(style="SCALPER").update(style="SCALPING")
    Strategy.objects.filter(style="INTRADAY").update(style="DAY")


class Migration(migrations.Migration):

    dependencies = [
        ("strategies", "0003_strategy_edge_rationale_strategy_edge_type_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
