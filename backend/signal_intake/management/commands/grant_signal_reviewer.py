"""
E3-APPROVAL-RBAC — grant/revoke the ``review_signals`` permission for a user.

Idempotent operator entry point (avoids clicking through the auth admin):

    python manage.py grant_signal_reviewer <username_or_email>
    python manage.py grant_signal_reviewer <username_or_email> --revoke
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Grant (or --revoke) the signal_intake.review_signals permission for a user."

    def add_arguments(self, parser):
        parser.add_argument("user", help="Username or email of the reviewer.")
        parser.add_argument("--revoke", action="store_true", help="Revoke instead of grant.")

    def handle(self, *args, **options):
        User = get_user_model()
        ident = options["user"]
        user = (
            User.objects.filter(username=ident).first()
            or User.objects.filter(email=ident).first()
        )
        if user is None:
            raise CommandError(f"no user with username/email {ident!r}")

        perm = Permission.objects.get(
            codename="review_signals", content_type__app_label="signal_intake"
        )
        if options["revoke"]:
            user.user_permissions.remove(perm)
            verb = "revoked from"
        else:
            user.user_permissions.add(perm)
            verb = "granted to"
        self.stdout.write(f"review_signals {verb} {user.username} (id={user.pk})")
