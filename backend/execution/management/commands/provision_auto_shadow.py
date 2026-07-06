"""
GFX-PKT-AUTO-SHADOW-VALIDATE — provision the auto-execution system reviewer identity.

Creates (or updates) the service User the auto-router resolves by username
(``AUTO_EXECUTION_SYSTEM_USERNAME``, default ``guvfx-auto-system``) and grants it the
``signal_intake.review_signals`` permission so the router's ``approve()`` call succeeds
when — and only when — the full AND of auto-shadow config gates is later armed.

Safety properties:
- The identity gets an UNUSABLE password — it is a service identity and CANNOT log in.
- It grants ONLY ``review_signals``. It does NOT arm any provider, does NOT set
  ``ExecutionControl.auto_execution_enabled``, and does NOT touch any account. Provisioning
  the reviewer alone changes nothing observable: the auto-router still returns MANUAL until
  every other gate is deliberately armed. It never creates an order or an ExecutionJob.
- Idempotent. ``--revoke`` removes the permission (rollback).

Usage::

    python manage.py provision_auto_shadow            # grant
    python manage.py provision_auto_shadow --revoke   # rollback
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError

REVIEW_CODENAME = "review_signals"


class Command(BaseCommand):
    help = (
        "Provision the auto-execution system reviewer identity (grants review_signals; "
        "no login, no provider arming, no order)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--revoke", action="store_true",
            help="Remove review_signals from the identity (rollback).",
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        username = getattr(settings, "AUTO_EXECUTION_SYSTEM_USERNAME", "guvfx-auto-system")
        try:
            perm = Permission.objects.get(
                codename=REVIEW_CODENAME, content_type__app_label="signal_intake",
            )
        except Permission.DoesNotExist as exc:  # pragma: no cover - defensive
            raise CommandError(
                "signal_intake.review_signals permission not found (run migrations first)."
            ) from exc

        user, created = User.objects.get_or_create(
            username=username, defaults={"is_active": True, "is_staff": False},
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])

        if opts["revoke"]:
            user.user_permissions.remove(perm)
            self.stdout.write(f"revoked {REVIEW_CODENAME} from '{username}'")
            return

        user.user_permissions.add(perm)
        self.stdout.write(
            f"auto-system reviewer ready: '{username}' "
            f"({'created' if created else 'exists'}; review_signals granted; unusable password; "
            f"no arming, no order)."
        )
