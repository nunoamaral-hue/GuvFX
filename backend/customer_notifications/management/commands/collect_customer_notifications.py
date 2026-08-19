from django.conf import settings
from django.core.management.base import BaseCommand

from customer_notifications.event_sources import collect_customer_notification_events


class Command(BaseCommand):
    help = "Reconcile durable customer event sources into the isolated notification outbox."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1000)

    def handle(self, *args, **options):
        enabled = bool(getattr(settings, "CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED", False))
        worker_enabled = bool(getattr(settings, "CUSTOMER_TELEGRAM_WORKER_ENABLED", False))
        if not enabled or not worker_enabled:
            self.stdout.write(str({"enabled": enabled, "worker_enabled": worker_enabled, "collected": False}))
            return
        self.stdout.write(str(collect_customer_notification_events(limit=max(1, options["limit"]))))
