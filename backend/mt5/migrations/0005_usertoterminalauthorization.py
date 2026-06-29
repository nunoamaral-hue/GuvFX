"""Migration 2 of 5 (Packet A): Create UserToTerminalAuthorization."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("mt5", "0004_terminalbinding"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserToTerminalAuthorization",
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
                    "access_mode",
                    models.CharField(
                        help_text="Access mode, e.g. 'full', 'view_only', 'trade_only'.",
                        max_length=32,
                    ),
                ),
                ("can_launch", models.BooleanField(default=False)),
                ("can_resume", models.BooleanField(default=False)),
                ("can_manual_trade", models.BooleanField(default=False)),
                ("can_chart_interact", models.BooleanField(default=False)),
                ("granted_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("revocation_reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "granted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="granted_terminal_authorizations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "terminal_binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="authorizations",
                        to="mt5.terminalbinding",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="terminal_authorizations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User-to-Terminal Authorization",
                "verbose_name_plural": "User-to-Terminal Authorizations",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="usertoterminalauthorization",
            index=models.Index(
                fields=["user", "terminal_binding"],
                name="idx_auth_user_binding",
            ),
        ),
    ]
