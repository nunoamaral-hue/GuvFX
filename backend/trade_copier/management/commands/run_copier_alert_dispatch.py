"""run_copier_alert_dispatch — governed outbound delivery of queued copier alerts (ADR-0074).

No-op unless COPIER_ENABLED AND COPIER_ALERT_DISPATCH_ENABLED are on. Claims PENDING/FAILED
``CopierAlertDelivery`` rows and sends them through the selected transport, which DEFAULTS to an inert dry-run
with NO real destination — so an armed dispatcher with no configured sink produces visibly-unsuccessful
deliveries, never a silent 'sent'. Records a per-attempt audit + bounded retry (FAILED → DEAD). Creates NO
order, NO job, contacts NO broker, recovers NOTHING. NOT scheduled — run on demand.

Usage::

    python manage.py run_copier_alert_dispatch
    python manage.py run_copier_alert_dispatch --limit 100
"""
from django.core.management.base import BaseCommand

from trade_copier.alert_delivery import dispatch_pending


class Command(BaseCommand):
    help = "Dispatch queued copier alert deliveries (gated; inert dry-run transport by default; no recovery)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        summary = dispatch_pending(limit=opts["limit"])
        self.stdout.write("copier-alert-dispatch: {} (no real destination by default)".format(summary))
