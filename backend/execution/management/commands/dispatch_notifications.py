"""
TELEGRAM-TRANSPORT-FOUNDATION — dispatch PENDING notification candidates (dry-run only).

Runs the transport dispatcher once. Behind the ``NOTIFICATION_DISPATCH_ENABLED`` feature flag
(default OFF → no-op). Even when enabled, the only transport is dry-run: it renders the message
but NEVER transmits — no Telegram API, no credentials, no HTTP. Idempotent; SENT/SUPPRESSED
candidates are ignored.

Usage::

    python manage.py dispatch_notifications
    NOTIFICATION_DISPATCH_ENABLED=true python manage.py dispatch_notifications --limit 100
"""
from django.core.management.base import BaseCommand

from execution.notifications.dispatcher import DEFAULT_LIMIT, dispatch_pending


class Command(BaseCommand):
    help = "Dispatch PENDING notification candidates via the dry-run transport (nothing sent)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    def handle(self, *args, **opts):
        counts = dispatch_pending(limit=opts["limit"])
        self.stdout.write(
            "notification-dispatch: enabled={enabled} claimed={claimed} sent={sent} "
            "failed={failed} skipped={skipped} (dry-run; nothing transmitted)".format(**counts)
        )
