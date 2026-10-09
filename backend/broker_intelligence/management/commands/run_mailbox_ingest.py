"""WP3b — run the mailbox ingestion pass (poll CONNECTED mailboxes -> ingest new broker mail).

DARK by default: refuses unless ``BROKER_INTELLIGENCE_INGEST_ENABLED`` is on, or ``--force`` (operator/test escape).
Owner-scoped + read-only (no order/MT5/strategy); the cursor advances only after durable ingestion. Prints a
secret-free summary.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Poll CONNECTED mailboxes and ingest new broker mail (DARK; requires BROKER_INTELLIGENCE_INGEST_ENABLED or --force)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run even if the DARK flag is off (operator/test).")
        parser.add_argument("--limit", type=int, default=None, help="Max mailboxes to process this pass.")

    def handle(self, *args, **o):
        from broker_intelligence.flags import broker_intelligence_ingest_enabled
        from broker_intelligence.mailbox_ingest import run_mailbox_ingest

        if not broker_intelligence_ingest_enabled() and not o["force"]:
            self.stdout.write(self.style.WARNING(
                "mailbox ingestion is DARK (BROKER_INTELLIGENCE_INGEST_ENABLED off); pass --force to run. "
                "No mailboxes polled."))
            return
        summary = run_mailbox_ingest(limit_mailboxes=o["limit"])
        self.stdout.write(self.style.SUCCESS("mailbox ingestion pass complete:"))
        for key in sorted(summary):
            self.stdout.write(f"  {key} = {summary[key]}")
