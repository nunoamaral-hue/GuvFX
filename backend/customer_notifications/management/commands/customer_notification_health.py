import json

from django.core.management.base import BaseCommand, CommandError

from customer_notifications.delivery import queue_health


class Command(BaseCommand):
    help = "Report customer-notification queue health without exposing recipient metadata."

    def add_arguments(self, parser):
        parser.add_argument("--max-oldest", type=int, default=None)
        parser.add_argument("--max-heartbeat", type=int, default=None)

    def handle(self, *args, **options):
        health = queue_health()
        self.stdout.write(json.dumps(health, sort_keys=True))
        ceiling = options["max_oldest"]
        if (
            ceiling is not None and health["feature_enabled"] and health["worker_enabled"]
            and health["oldest_pending_age_seconds"] > ceiling
        ):
            raise CommandError("customer notification queue is stale")
        heartbeat_ceiling = options["max_heartbeat"]
        heartbeat_age = health["worker_heartbeat_age_seconds"]
        if (
            heartbeat_ceiling is not None
            and health["feature_enabled"]
            and health["worker_enabled"]
            and (heartbeat_age is None or heartbeat_age > heartbeat_ceiling)
        ):
            raise CommandError("customer notification worker heartbeat is stale")
