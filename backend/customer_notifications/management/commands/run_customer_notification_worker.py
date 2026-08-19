import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from customer_notifications.delivery import dispatch_customer_notifications, queue_health
from customer_notifications.event_sources import collect_customer_notification_events


class Command(BaseCommand):
    help = "Run the isolated customer-notification collector and Telegram delivery worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=5.0)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        stopped = False

        def stop(*_):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        interval = max(1.0, min(float(options["interval"]), 60.0))
        limit = max(1, min(int(options["limit"]), 1000))
        while not stopped:
            enabled = bool(getattr(settings, "CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED", False))
            worker_enabled = bool(getattr(settings, "CUSTOMER_TELEGRAM_WORKER_ENABLED", False))
            if enabled and worker_enabled:
                collected = collect_customer_notification_events(limit=limit)
                delivered = dispatch_customer_notifications(limit=limit)
            else:
                collected = {"dark": not enabled, "worker_disabled": not worker_enabled}
                delivered = {"enabled": enabled, "worker_enabled": worker_enabled}
            self.stdout.write(
                f"customer-notifications collected={collected} delivered={delivered} health={queue_health()}"
            )
            if options["once"]:
                break
            time.sleep(interval)
