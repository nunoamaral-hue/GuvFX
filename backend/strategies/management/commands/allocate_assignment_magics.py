"""Phase A — allocate deterministic MT5 magic numbers to StrategyAssignments.

DRY-RUN by default (prints the plan, writes nothing). ``--commit`` persists.
Idempotent: only assignments with ``magic_number IS NULL`` are touched, and the
magic is the deterministic ``ASSIGNMENT_MAGIC_BASE + id`` (never reused).
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from strategies.magic_allocation import MagicAllocationError, allocate_magic, magic_for
from strategies.models import StrategyAssignment


class Command(BaseCommand):
    help = "Allocate deterministic MT5 magic numbers (1e9 + id) to StrategyAssignments (DRY-RUN by default)."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true",
                            help="Persist the allocation (default: dry-run).")

    def handle(self, *args, **opts):
        commit = opts["commit"]
        pending = StrategyAssignment.objects.filter(magic_number__isnull=True).order_by("id")
        planned, errors = [], []
        for a in pending:
            try:
                planned.append((a.id, magic_for(a.id)))
            except MagicAllocationError as exc:
                errors.append((a.id, str(exc)))

        self.stdout.write(f"StrategyAssignments needing a magic: {len(planned)} (errors: {len(errors)})")
        for aid, m in planned:
            self.stdout.write(f"  assignment {aid} -> magic {m}")
        for aid, err in errors:
            self.stderr.write(f"  assignment {aid} ERROR: {err}")

        if not commit:
            self.stdout.write(self.style.WARNING("DRY-RUN — nothing written. Re-run with --commit to persist."))
            return
        if errors:
            self.stderr.write(self.style.ERROR("Refusing to commit while allocation errors exist."))
            return

        allocated = 0
        with transaction.atomic():
            for a in (StrategyAssignment.objects.select_for_update()
                      .filter(magic_number__isnull=True).order_by("id")):
                allocate_magic(a, save=True)
                allocated += 1
        self.stdout.write(self.style.SUCCESS(f"Allocated {allocated} magic numbers."))
