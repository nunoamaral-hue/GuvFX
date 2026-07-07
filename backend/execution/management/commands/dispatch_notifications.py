"""
TELEGRAM-TRANSPORT — dispatch PENDING notification candidates.

Runs the transport dispatcher once. Behind the ``NOTIFICATION_DISPATCH_ENABLED`` feature flag
(default OFF → no-op). Even when enabled, the transport is dry-run (renders but NEVER transmits)
UNLESS an operator also selects the real transport via ``NOTIFICATION_DISPATCH_TRANSPORT=real``
(with ``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID``). Idempotent; SENT/SUPPRESSED are ignored.

Usage::

    python manage.py dispatch_notifications
    NOTIFICATION_DISPATCH_ENABLED=true python manage.py dispatch_notifications --limit 100
"""
from django.core.management.base import BaseCommand

from execution.notifications.dispatcher import DEFAULT_LIMIT, dispatch_pending


class Command(BaseCommand):
    help = ("Dispatch PENDING notification candidates. Dry-run by default; transmits only when "
            "dispatch is enabled AND NOTIFICATION_DISPATCH_TRANSPORT selects the real transport.")

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    def handle(self, *args, **opts):
        counts = dispatch_pending(limit=opts["limit"])
        self.stdout.write(
            "notification-dispatch: enabled={enabled} claimed={claimed} sent={sent} "
            "failed={failed} skipped={skipped} (dry-run unless the real transport is "
            "selected)".format(**counts)
        )
