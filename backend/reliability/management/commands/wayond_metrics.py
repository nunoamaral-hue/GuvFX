"""Read-only Wayond truthful-metrics dump (sanitized JSON) for operator verification.

Prints the SAME value-blind aggregate the staff endpoint serves. Mutates NOTHING, places no
order, contacts no broker/host, enables no flag. Safe to run read-only in any environment.
"""

import json

from django.core.management.base import BaseCommand

from reliability.services.wayond_metrics import build_wayond_metrics, DEFAULT_WINDOW_DAYS


class Command(BaseCommand):
    help = "Print the value-blind Wayond truthful-metrics aggregate as JSON (read-only, no mutation)."

    def add_arguments(self, parser):
        parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                            help="Lookback window in days (1..365).")
        parser.add_argument("--indent", type=int, default=2)

    def handle(self, *args, **opts):
        window = max(1, min(int(opts["window_days"]), 365))
        data = build_wayond_metrics(window_days=window)
        self.stdout.write(json.dumps(data, indent=opts["indent"], sort_keys=True, default=str))
