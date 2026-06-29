"""
SHADOW intake of Wayond Telegram signals into PendingSignalApproval.

Reads a JSON batch of Telegram messages (fixture or --file), classifies them
with the deployed Wayond parser, and creates PendingSignalApproval rows for
human review. Creates NO ExecutionJob and places NO order. There is NO live
Telegram listener here — input is file-based only.

Usage:
    python manage.py ingest_wayond_signals_for_approval
    python manage.py ingest_wayond_signals_for_approval --file path/to/export.json
"""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from signal_intake import services

User = get_user_model()
DEFAULT_FIXTURE = "signal_intake/fixtures/wayond_signals_sample.json"


class Command(BaseCommand):
    help = "SHADOW: ingest Wayond Telegram signals into PendingSignalApproval (no orders)."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Path to a Telegram messages JSON export.")
        parser.add_argument("--actor", help="Existing username/email to attribute to.")

    def _actor(self, identifier):
        if identifier:
            u = (User.objects.filter(username=identifier).first()
                 or User.objects.filter(email=identifier).first())
            if u:
                return u
        u, _ = User.objects.get_or_create(
            username="signal_intake_operator",
            defaults={"email": "signal_intake_operator@example.invalid"},
        )
        return u

    def handle(self, *args, **opts):
        actor = self._actor(opts.get("actor"))
        rel = opts.get("file") or DEFAULT_FIXTURE
        path = Path(rel)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / rel
        if not path.exists():
            raise CommandError(f"messages file not found: {path}")
        messages = json.loads(path.read_text())

        result = services.ingest_messages(messages, actor=actor)

        self.stdout.write(self.style.SUCCESS(
            "\nWayond signal intake (SHADOW — pending approval; NO orders)"
        ))
        for a in result["created"]:
            self.stdout.write(
                f"  + PENDING #{a.id} {a.direction} {a.symbol} @ {a.entry} "
                f"(SL {a.stop_loss}, TP {a.take_profit}) msg={a.message_id}"
            )
        for a in result["quarantined"]:
            self.stdout.write(self.style.WARNING(
                f"  ~ QUARANTINED #{a.id} msg={a.message_id}"
            ))
        self.stdout.write(
            f"  updates skipped: {result['updates_skipped']}  "
            f"duplicates skipped: {result['duplicates_skipped']}"
        )
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS(
            f"Done — {len(result['created'])} pending approval(s); "
            f"0 ExecutionJobs created; 0 orders placed."
        ))
