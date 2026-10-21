"""run_copier_replication — record replication INTENTS for pending master events (PR B).

For master events with no replication row yet, records one idempotent ``CopierReplication`` per
configured follower (sized volume, mapped symbol, translated SL/TP) — or a REJECTED/SKIPPED outcome.
Creates NO ExecutionJobs and performs NO execution.

DARK: no-op unless BOTH ``COPIER_ENABLED`` and ``COPIER_REPLICATION_ENABLED`` are on; ``--force``
runs one diagnostic sweep regardless.

Usage::

    python manage.py run_copier_replication
    python manage.py run_copier_replication --limit 200 --force
"""
from django.core.management.base import BaseCommand

from trade_copier.flags import copier_replication_enabled
from trade_copier.replication import replicate_pending


class Command(BaseCommand):
    help = "Record replication intents for pending master events (no execution)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--force", action="store_true",
                            help="Run one sweep even when the DARK flags are off (diagnostics).")

    def handle(self, *args, **opts):
        if not opts["force"] and not copier_replication_enabled():
            self.stdout.write(
                "copier-replication: DISABLED (COPIER_ENABLED/COPIER_REPLICATION_ENABLED off) — no-op. "
                "Use --force for a diagnostic sweep."
            )
            return
        summary = replicate_pending(limit=opts["limit"])
        self.stdout.write(
            "copier-replication: events={events} intents={intents} "
            "(INTENTS only; no ExecutionJobs, no execution)".format(**summary)
        )
