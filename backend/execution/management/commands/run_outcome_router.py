"""
PROFIT-NOTIFICATION-FOUNDATION — run the outcome router once over unrouted outcomes.

WIN → a PENDING NotificationCandidate (internal); LOSS/BREAKEVEN → internal record only.
Creates NO Telegram notification, NO WIMS contract, NO order. Idempotent — already-routed
outcomes are skipped.

Usage::

    python manage.py run_outcome_router
    python manage.py run_outcome_router --limit 100
"""
from django.core.management.base import BaseCommand

from execution.outcome_router import DEFAULT_LIMIT, route_outcomes


class Command(BaseCommand):
    help = "Route classified trade outcomes: WIN → internal NotificationCandidate; LOSS/BE → none."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    def handle(self, *args, **opts):
        counts = route_outcomes(limit=opts["limit"])
        self.stdout.write(
            "outcome-router: routed={routed} candidates={candidates} "
            "internal_only={internal_only} (internal candidates only; no Telegram/WIMS/"
            "order)".format(**counts)
        )
