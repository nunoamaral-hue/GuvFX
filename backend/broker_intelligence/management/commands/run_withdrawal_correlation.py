"""WP5 — run the withdrawal correlation pass (project EXTERNAL_WITHDRAWAL events into Withdrawal records).

DARK by default: refuses unless ``BROKER_WITHDRAWAL_CORRELATION_ENABLED`` is on, or ``--force`` is passed (operator/
test escape hatch). The engine (``broker_intelligence.correlation.run_correlation``) is pure + idempotent: it only
reads already-ingested events and writes withdrawal projections — NO ingestion, NO trading effect, NO order, NO
credential, never a financial action. Prints a secret-free per-outcome summary.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the withdrawal correlation pass (DARK; requires BROKER_WITHDRAWAL_CORRELATION_ENABLED or --force)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Run even if the DARK flag is off (operator/test escape hatch).")
        parser.add_argument("--limit", type=int, default=None, help="Max events to process this pass.")

    def handle(self, *args, **o):
        from broker_intelligence.correlation import run_correlation
        from broker_intelligence.flags import (broker_cross_inbox_dedup_enabled,
                                               broker_withdrawal_correlation_enabled)

        if not broker_withdrawal_correlation_enabled() and not o["force"]:
            self.stdout.write(self.style.WARNING(
                "withdrawal correlation is DARK (BROKER_WITHDRAWAL_CORRELATION_ENABLED off); pass --force to run. "
                "No events processed."))
            return

        # Arming coupling (fail-closed): the ref-less cross-inbox collapse that stops a withdrawal delivered to TWO
        # inboxes from double-counting relies on the per-inbox delivery rows that exist ONLY when cross-inbox dedup is
        # ON. Refuse the LIVE pass without it, so arming correlation alone can never inflate public statistics.
        # --force (operator/test escape hatch) bypasses both gates.
        if not broker_cross_inbox_dedup_enabled() and not o["force"]:
            self.stdout.write(self.style.ERROR(
                "refusing: BROKER_WITHDRAWAL_CORRELATION_ENABLED is on but BROKER_CROSS_INBOX_DEDUP_ENABLED is off. "
                "The ref-less cross-inbox withdrawal collapse needs per-inbox delivery records; arm cross-inbox dedup "
                "first (or pass --force for an operator/test run). No events processed."))
            return

        summary = run_correlation(limit=o["limit"])
        self.stdout.write(self.style.SUCCESS("withdrawal correlation pass complete:"))
        for key in sorted(summary):
            self.stdout.write(f"  {key} = {summary[key]}")
