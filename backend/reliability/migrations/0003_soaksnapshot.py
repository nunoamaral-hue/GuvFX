from django.db import migrations, models


class Migration(migrations.Migration):
    """WS-G — additive: durable soak-test evidence snapshots."""

    dependencies = [
        ("reliability", "0002_circuitbreakerstate_recoveryattempt"),
    ]

    operations = [
        migrations.CreateModel(
            name="SoakSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("generated_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("window_hours", models.PositiveIntegerField(default=24)),
                ("data", models.JSONField(blank=True, default=dict)),
            ],
            options={"ordering": ["-generated_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="soaksnapshot",
            index=models.Index(fields=["-generated_at"], name="reliability_soak_gen_idx"),
        ),
    ]
