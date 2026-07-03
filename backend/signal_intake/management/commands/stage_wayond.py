"""
GFX-PKT-WAYOND-CORPUS-SEED-READY — stage a raw paste of REAL Wayond messages.

Splits the paste, classifies each message, PROPOSES a type, and writes a reviewable
DRAFT staging file (never the permanent corpus). Never fabricates messages. No DB,
no Telegram, no order.

    # from a file:
    python manage.py stage_wayond --in paste.txt --out wayond_staging.json
    # or piped in:
    pbpaste | python manage.py stage_wayond --out wayond_staging.json
"""

import json
import sys

from django.core.management.base import BaseCommand, CommandError

from signal_intake.staging import stage_entries


class Command(BaseCommand):
    help = "Split + classify a raw paste of real Wayond messages into a draft staging file."

    def add_arguments(self, parser):
        parser.add_argument("--in", dest="infile", default=None,
                            help="Paste file (default: read stdin).")
        parser.add_argument("--out", default="wayond_staging.json",
                            help="Draft staging file to write (NOT the permanent corpus).")

    def handle(self, *args, **o):
        if o["infile"]:
            try:
                text = open(o["infile"], encoding="utf-8").read()
            except OSError as exc:
                raise CommandError(f"Cannot read {o['infile']!r}: {type(exc).__name__}")
        else:
            text = sys.stdin.read()
        if not text.strip():
            raise CommandError("No input. Pipe a paste in, or pass --in <file>. "
                               "Separate messages with a line that is exactly '---'.")

        staged = stage_entries(text)
        if not staged:
            raise CommandError("No messages found. Separate each message with a '---' line.")

        with open(o["out"], "w", encoding="utf-8") as fh:
            json.dump({"staged": staged}, fh, indent=2, ensure_ascii=False)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"STAGED {len(staged)} message(s) -> {o['out']} (DRAFT — review before promoting)"))
        self.stdout.write(f"  {'id':<30} {'observed':<12} {'proposed':<12} confirmed review")
        self.stdout.write("  " + "-" * 74)
        for e in staged:
            flag = "REVIEW" if e["needs_review"] else "ok"
            style = self.style.WARNING if e["needs_review"] else self.style.SUCCESS
            self.stdout.write(style(
                f"  {e['id']:<30} {e['observed']:<12} {e['expected_type']:<12} "
                f"{str(e['confirmed']):<9} {flag}"))
        needs = [e["id"] for e in staged if e["needs_review"]]
        self.stdout.write("")
        self.stdout.write(
            "Next: confirm/correct each entry's expected_type + set \"confirmed\": true, "
            f"then `promote_wayond --in {o['out']}`.")
        if needs:
            self.stdout.write(self.style.WARNING(
                f"{len(needs)} entrie(s) need review (unconfirmed, a proposed trade, or a "
                "signal-shaped message the parser did NOT read as tradeable)."))
