from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("customer_notifications", "0001_initial"),
        ("hosted_workspace", "0009_capability_recovery_fields"),
        ("strategies", "0013_assignment_leg_sizing"),
    ]

    operations = [
        migrations.AddField(
            model_name="customernotificationpreference",
            name="winning_trades",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="customernotificationpreference",
            name="losing_trades",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customernotificationpreference",
            name="tp_progress",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="customernotificationpreference",
            name="system_messages",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="customernotification",
            name="strategy_assignment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="customer_notifications",
                to="strategies.strategyassignment",
            ),
        ),
        migrations.CreateModel(
            name="CustomerStrategyNotificationPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enabled", models.BooleanField(default=False)),
                ("pending_enable", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assignment", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="customer_notification_preference",
                    to="strategies.strategyassignment",
                )),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="strategy_notification_preferences",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
        migrations.AddConstraint(
            model_name="customerstrategynotificationpreference",
            constraint=models.UniqueConstraint(
                fields=("user", "assignment"), name="cust_notify_user_assignment",
            ),
        ),
        migrations.CreateModel(
            name="WorkspaceReadinessNotificationIntent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("milestone", models.CharField(default="workspace_ready", max_length=32)),
                ("fulfilled_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="workspace_readiness_notification_intents",
                    to=settings.AUTH_USER_MODEL,
                )),
                ("workspace", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="customer_readiness_notification_intents",
                    to="hosted_workspace.hostedmt5workspace",
                )),
            ],
        ),
        migrations.AddConstraint(
            model_name="workspacereadinessnotificationintent",
            constraint=models.UniqueConstraint(
                fields=("workspace", "milestone"), name="cust_notify_workspace_milestone",
            ),
        ),
    ]
