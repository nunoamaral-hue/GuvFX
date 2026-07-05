"""
Write a listener fixture JSON derived from the certified Wayond corpus (real message
text + demo transport metadata). Repo-only, no Telegram. Feed the output to
``run_wayond_listener --fixture <file> [--dry-run]``.

    python manage.py dump_wayond_fixture --chat-id 1001 --out wayond_fixture.json
"""

import json
import time

from django.core.management.base import BaseCommand

from signal_intake.certification import load_corpus
from signal_intake.listener.fixtures import corpus_to_fixtures


class Command(BaseCommand):
    help = "Dump a listener fixture JSON from the certified Wayond corpus (no Telegram)."

    def add_arguments(self, parser):
        parser.add_argument("--chat-id", required=True,
                            help="Chat id to stamp on the fixtures (match a provider).")
        parser.add_argument("--out", default="wayond_fixture.json")
        parser.add_argument("--timestamp", type=float, default=None,
                            help="Epoch date for the messages (default: now — keeps a "
                                 "real replay fresh; regenerate before non-dry-run use).")

    def handle(self, *args, **o):
        entries = load_corpus()
        ts = o["timestamp"] if o["timestamp"] is not None else time.time()  # noqa: DTZ (harness ts)
        fixtures = corpus_to_fixtures(entries, chat_id=o["chat_id"], timestamp=ts)
        with open(o["out"], "w", encoding="utf-8") as fh:
            json.dump({"messages": fixtures}, fh, indent=2, ensure_ascii=False)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(fixtures)} fixture message(s) → {o['out']} "
            f"(from {len(entries)} certified corpus entries)."))
