"""prune_copier_ingest_nonces — bounded, safe retention for the signed-ingest replay-burn store (ADR-0074).

DRY-RUN by default (reports what WOULD be pruned, deletes nothing). Deletes ONLY fully-expired
``CopierIngestNonce`` rows (``expires_at < now - margin``), so replay protection inside the active window is
never weakened. Not gated on the copier flags (it is pure housekeeping of disposable, secret-free rows) and NOT
scheduled — a pre-arming operator step, run on demand. The output is secret-free (counts only; a nonce value is
never read or printed).

Usage::

    python manage.py prune_copier_ingest_nonces                 # dry-run (default): report only
    python manage.py prune_copier_ingest_nonces --apply         # actually delete expired nonces
    python manage.py prune_copier_ingest_nonces --apply --margin-seconds 3600
"""
from django.core.management.base import BaseCommand

from trade_copier.event_ingestion import prune_ingest_nonces


class Command(BaseCommand):
    help = "Prune fully-expired copier ingest nonces (dry-run by default; preserves in-window replay protection)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run).")
        parser.add_argument("--margin-seconds", type=int, default=0,
                            help="Extra grace beyond expiry before a nonce is eligible (more conservative).")

    def handle(self, *args, **opts):
        summary = prune_ingest_nonces(dry_run=not opts["apply"], margin_seconds=opts["margin_seconds"])
        self.stdout.write("copier-ingest-nonce-prune: {}".format(summary))
