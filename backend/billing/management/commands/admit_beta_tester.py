"""CVM controlled-beta admission — add/remove ONE beta test email on the admission allowlist.

This is the admission control ("invite issuance"). It does NOT open public onboarding: it admits a single
allowlisted identity whose admission replaces email verification for that identity only. Cap-enforced.

    python manage.py admit_beta_tester tester@example.com
    python manage.py admit_beta_tester tester@example.com --deactivate
    python manage.py admit_beta_tester --list
"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from billing.models import BetaTester


class Command(BaseCommand):
    help = "CVM: admit/deactivate/list controlled beta test emails on the admission allowlist."

    def add_arguments(self, parser):
        parser.add_argument("email", nargs="?", default="")
        parser.add_argument("--deactivate", action="store_true",
                            help="deactivate the email instead of admitting it")
        parser.add_argument("--note", default="", help="optional note")
        parser.add_argument("--list", action="store_true", help="list allowlist entries and exit")

    def handle(self, *args, **o):
        if o["list"]:
            rows = list(BetaTester.objects.order_by("email"))
            if not rows:
                self.stdout.write("beta admission allowlist is EMPTY (public onboarding stays closed)")
            for r in rows:
                self.stdout.write(f"  {r.email}  active={r.is_active}  note={r.note!r}")
            return

        email = (o["email"] or "").strip().lower()
        if not email:
            raise CommandError("provide an email (or --list)")

        if o["deactivate"]:
            n = BetaTester.objects.filter(email__iexact=email).update(is_active=False)
            self.stdout.write(self.style.WARNING(f"deactivated {n} allowlist entry(ies) for {email}"))
            return

        # Estate-safety: refuse to admit an email that belongs to an existing staff/superuser account.
        from django.contrib.auth import get_user_model
        if get_user_model().objects.filter(email__iexact=email, is_staff=True).exists() or \
           get_user_model().objects.filter(email__iexact=email, is_superuser=True).exists():
            raise CommandError(f"refusing to admit a staff/superuser email ({email})")

        try:
            bt, created = BetaTester.objects.get_or_create(email=email, defaults={"note": o["note"]})
            if not created and not bt.is_active:
                bt.is_active = True
                if o["note"]:
                    bt.note = o["note"]
                bt.save()
        except ValidationError as e:
            raise CommandError("; ".join(e.messages))
        self.stdout.write(self.style.SUCCESS(
            f"admitted beta tester {email} (created={created}). Public onboarding is unchanged/closed."))
