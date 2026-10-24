"""run_copier_execute — produce enqueue-only DEMO/SHADOW ExecutionJobs from OPEN replication intents.

DARK and DEMO-only (B-exec). No-op unless COPIER_ENABLED AND COPIER_EXECUTE_ENABLED are on; even then
jobs are PLACE_ORDER_SHADOW (suppressed) unless COPIER_EXECUTE_DEMO_ARMED is also on. Never LIVE, never
bypasses the demo-only bridge walls, kill switch, pin or readiness. Arming is operator-gated.

Usage::

    python manage.py run_copier_execute
    python manage.py run_copier_execute --limit 50
"""
from django.core.management.base import BaseCommand

from trade_copier.execution_producer import produce_pending


class Command(BaseCommand):
    help = "Produce enqueue-only DEMO/SHADOW ExecutionJobs from OPEN replication intents (gated, DEMO-only)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        summary = produce_pending(limit=opts["limit"])
        self.stdout.write("copier-execute: {} (DEMO-only, enqueue-only; no LIVE)".format(summary))
