"""
Container healthcheck for the Wayond listener: exit 0 if its liveness heartbeat file
(written by ``run_wayond_listener --health-file``) is fresh, else exit 1. Repo-only,
no Telegram, no DB writes.

    python manage.py check_wayond_listener --health-file /tmp/wayond_health
"""

import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Healthcheck: exit 0 if the listener heartbeat file is fresh, else 1."

    def add_arguments(self, parser):
        parser.add_argument("--health-file", required=True)
        parser.add_argument("--max-age", type=float, default=120.0,
                            help="Max heartbeat age in seconds before UNHEALTHY.")

    def handle(self, *args, **o):
        try:
            ts = float(open(o["health_file"], encoding="utf-8").read().strip())
        except (OSError, ValueError):
            self.stderr.write("UNHEALTHY: heartbeat file missing/unreadable")
            raise SystemExit(1)
        age = time.time() - ts
        if age > o["max_age"]:
            self.stderr.write(f"UNHEALTHY: heartbeat {age:.0f}s old (> {o['max_age']:.0f}s)")
            raise SystemExit(1)
        self.stdout.write(f"healthy: heartbeat {age:.0f}s old")
