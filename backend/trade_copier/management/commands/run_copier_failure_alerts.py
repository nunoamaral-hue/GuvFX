"""run_copier_failure_alerts — route copier terminal-failure signals to the governed alert channel (ADR-0074).

No-op unless COPIER_ENABLED AND COPIER_ALERTS_ENABLED are on. Scans terminal ``CopierPositionLifecycle`` rows
(and recent non-terminal modify-rejection signals), classifies the alert category, and raises de-duplicated
``reliability.AlertEvent`` rows through the EXISTING governed mechanism. Creates NO order, NO job, invents NO
notification channel, and recovers NOTHING — it only exposes operator action. NOT scheduled — run on demand.

Usage::

    python manage.py run_copier_failure_alerts
    python manage.py run_copier_failure_alerts --limit 200
"""
from django.core.management.base import BaseCommand

from trade_copier.failure_alerts import run_failure_alert_pass


class Command(BaseCommand):
    help = "Raise governed, de-duplicated copier terminal-failure alerts (gated; no recovery, no order)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **opts):
        summary = run_failure_alert_pass(limit=opts["limit"])
        self.stdout.write("copier-failure-alerts: {} (governed AlertEvent only; no recovery)".format(summary))
