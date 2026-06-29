"""Migration 3 of 5 (Packet A): Create InteractionSession."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("mt5", "0005_usertoterminalauthorization"),
    ]

    operations = [
        migrations.CreateModel(
            name="InteractionSession",
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
                    "state",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        default="",
                        max_length=32,
                    ),
                ),
                ("requested_at", models.DateTimeField(blank=True, null=True)),
                ("authorized_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("last_activity_at", models.DateTimeField(blank=True, null=True)),
                ("terminated_reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "authorization",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="interaction_sessions",
                        to="mt5.usertoterminalauthorization",
                    ),
                ),
                (
                    "terminal_binding",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="interaction_sessions",
                        to="mt5.terminalbinding",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="interaction_sessions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Interaction Session",
                "verbose_name_plural": "Interaction Sessions",
                "ordering": ["-created_at"],
            },
        ),
        # Add the occupied_by_session FK on TerminalBinding now that
        # InteractionSession exists.
        migrations.AddField(
            model_name="terminalbinding",
            name="occupied_by_session",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="occupied_bindings",
                to="mt5.interactionsession",
                help_text="The InteractionSession currently occupying this binding.",
            ),
        ),
        migrations.AddIndex(
            model_name="interactionsession",
            index=models.Index(
                fields=["user", "state"],
                name="idx_isession_user_state",
            ),
        ),
        migrations.AddIndex(
            model_name="interactionsession",
            index=models.Index(
                fields=["terminal_binding", "state"],
                name="idx_isession_binding_state",
            ),
        ),
    ]
