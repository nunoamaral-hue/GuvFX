"""run_copier_observer — one READ-ONLY master-observation pass (PR A).

For each enabled ``CopierMaster``, reads its live open positions from its OWN identity-firewalled
bridge, records a durable poll + snapshot rows, and appends the derived ``MasterEvent`` rows. Creates
NO order, NO modification, NO Telegram/WIMS side effect — observation and ledger only. Safe to run
repeatedly (cron-driven); a refused/failed read records nothing.

DARK: does nothing unless BOTH ``COPIER_ENABLED`` and ``COPIER_OBSERVER_ENABLED`` are on. Use
``--force`` to run a single pass regardless of the flags (operator diagnostics only).

Usage::

    python manage.py run_copier_observer
    python manage.py run_copier_observer --limit 5
    python manage.py run_copier_observer --force
"""
from django.core.management.base import BaseCommand

from trade_copier.flags import copier_observer_enabled
from trade_copier.observer import run_observer_pass


class Command(BaseCommand):
    help = "Read-only observation pass over enabled copier masters (poll + event ledger; no orders)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--force", action="store_true",
                            help="Run one pass even when the DARK flags are off (diagnostics).")

    def handle(self, *args, **opts):
        if not opts["force"] and not copier_observer_enabled():
            self.stdout.write(
                "copier-observer: DISABLED (COPIER_ENABLED/COPIER_OBSERVER_ENABLED off) — no-op. "
                "Use --force for a diagnostic pass."
            )
            return
        summary = run_observer_pass(limit=opts["limit"])
        self.stdout.write(
            "copier-observer: masters={masters} observed_ok={ok} refused={refused} "
            "events={events} (read-only; no orders)".format(**summary)
        )
