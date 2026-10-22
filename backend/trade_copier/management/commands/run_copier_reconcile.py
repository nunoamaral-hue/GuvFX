"""run_copier_reconcile — establish the verified copier-owned follower-ticket association (ADR-0052).

DARK and READ-ONLY. No-op unless COPIER_ENABLED AND COPIER_RECONCILE_ENABLED are on. Reads each follower's
live positions through its own per-tenant bridge under the identity firewall and records which broker
ticket a copied armed-DEMO OPEN produced (``CopierReplication.follower_ticket``). Places/closes/modifies
NOTHING. This is the prerequisite for the follower-position-targeting CLOSE producer.

Usage::

    python manage.py run_copier_reconcile
    python manage.py run_copier_reconcile --limit 50
"""
from django.core.management.base import BaseCommand

from trade_copier.reconcile import run_reconcile_pass


class Command(BaseCommand):
    help = "Reconcile copier follower tickets from follower positions (gated, READ-ONLY)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        summary = run_reconcile_pass(limit=opts["limit"])
        self.stdout.write("copier-reconcile: {} (READ-ONLY; no order placed/closed/modified)".format(summary))
