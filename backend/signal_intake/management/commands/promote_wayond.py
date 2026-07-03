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
        result = promote(staged, o["corpus"])

        self.stdout.write(self.style.SUCCESS(
            f"Promoted {len(result['added'])} confirmed message(s): "
            + (", ".join(result["added"]) or "none")))
        if result["skipped"]:
            self.stdout.write(self.style.WARNING("Skipped:"))
            for sk in result["skipped"]:
                self.stdout.write(f"  - {sk['id']}: {sk['reason']}")
        self.stdout.write("\nNow run: python manage.py certify_wayond")
