"""
GFX-PKT-WAYOND-CORPUS-SEED-READY — promote a REVIEWED staging file into the corpus.

Appends ONLY confirmed entries (``"confirmed": true`` with a valid expected_type) to
the permanent wayond_corpus.json; skips unconfirmed, duplicate-text, and bad-type
entries with a reason. Never fabricates. After promoting, run `certify_wayond`.

    python manage.py promote_wayond --in wayond_staging.json
"""

import json

from django.core.management.base import BaseCommand, CommandError

from signal_intake.staging import promote


class Command(BaseCommand):
    help = "Append CONFIRMED reviewed staging entries into the permanent Wayond corpus."

    def add_arguments(self, parser):
        parser.add_argument("--in", dest="infile", required=True,
                            help="Reviewed staging file from stage_wayond.")
        parser.add_argument("--corpus", default=None,
                            help="Corpus to append to (default: wayond_corpus.json).")

    def handle(self, *args, **o):
        try:
            data = json.load(open(o["infile"], encoding="utf-8"))
        except OSError as exc:
            raise CommandError(f"Cannot read {o['infile']!r}: {type(exc).__name__}")
        except ValueError as exc:
            raise CommandError(f"Invalid staging JSON: {exc}")

        staged = data.get("staged", data) if isinstance(data, dict) else data
        try:
            result = promote(staged, o["corpus"])
        except (OSError, ValueError) as exc:
            raise CommandError(f"Cannot promote into corpus: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"Promoted {len(result['added'])} confirmed message(s): "
            + (", ".join(result["added"]) or "none")))
        if result["skipped"]:
            self.stdout.write(self.style.WARNING("Skipped:"))
            for sk in result["skipped"]:
                self.stdout.write(f"  - {sk['id']}: {sk['reason']}")
        if result.get("unsafe"):
            self.stdout.write(self.style.ERROR(
                "PARSER GAP — added, but the parser does NOT yet produce these certified "
                "types (certification will FAIL until the parser is fixed):"))
            for u in result["unsafe"]:
                self.stdout.write(
                    f"  - {u['id']}: certified {u['expected']} but parser sees {u['observed']}")
        self.stdout.write("\nNow run: python manage.py certify_wayond")
