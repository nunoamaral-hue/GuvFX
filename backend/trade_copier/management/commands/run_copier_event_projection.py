"""run_copier_event_projection — promote accepted fast-path revisions into the authoritative ledger (ADR-0074).

No-op unless COPIER_ENABLED AND COPIER_EVENT_PROJECTION_ENABLED are on. Folds each master's NEW
``CopierIngestedRevision`` rows, OVERLAYS them onto the shared poll baseline, and appends canonical
``MasterEvent`` rows (``source='EVENT'``, carrying the true ``broker_event_time``) under the SAME per-master row
lock + monotone sequence the observer uses — one ledger, one sequence space, no competing event truth. Creates
NO ExecutionJob and contacts NO broker; a late OPEN is ruled inadmissible by the unchanged lifecycle machine.
NOT scheduled — run on demand.

Usage::

    python manage.py run_copier_event_projection
    python manage.py run_copier_event_projection --limit 200
"""
from django.core.management.base import BaseCommand

from trade_copier.event_projection import run_event_projection_pass


class Command(BaseCommand):
    help = "Promote fast-path revisions into the authoritative MasterEvent ledger (gated; no order, no broker)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **opts):
        summary = run_event_projection_pass(limit=opts["limit"])
        self.stdout.write("copier-event-projection: {} (authoritative MasterEvent only; no order)".format(summary))
