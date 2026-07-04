"""
GFX-PKT-SIGNAL-ACQUISITION-LISTENER-BUILD — listener entry point (repo-only).

Defaults to FIXTURE / DRY-RUN. The live path (``--live``) lazy-imports Telethon,
loads an EXISTING authorised StringSession (never logs in), connects read-only, and
runs the listener — it is intentionally guarded so it cannot connect by accident and
is never exercised by tests.

    # replay a fixture (no Telegram), previewing outcomes without writing:
    python manage.py run_wayond_listener --fixture msgs.json --dry-run
    # replay a fixture into the real pipeline (still no Telegram):
    python manage.py run_wayond_listener --fixture msgs.json
    # live (requires an AGED, authorised GFX session in env — not for MVP):
    python manage.py run_wayond_listener --live
"""

import json
import os

from django.core.management.base import BaseCommand, CommandError

from signal_intake.listener import WayondListener


class Command(BaseCommand):
    help = "Run the read-only Wayond Telegram listener (fixture/dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", default=None,
                            help="JSON file of raw messages to replay (no Telegram).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Preview outcomes only — do not write to the DB.")
        parser.add_argument("--live", action="store_true",
                            help="DANGER: connect Telegram with an aged authorised session.")

    def handle(self, *args, **o):
        listener = WayondListener(dry_run=o["dry_run"])

        if o["fixture"]:
            try:
                with open(o["fixture"], encoding="utf-8") as fh:
                    data = json.load(fh)
            except OSError as exc:
                raise CommandError(f"Cannot read fixture {o['fixture']!r}: {type(exc).__name__}")
            except ValueError as exc:
                raise CommandError(f"Invalid fixture JSON: {exc}")
            messages = data.get("messages", data) if isinstance(data, dict) else data
            n = listener.replay(messages)
            mode = "DRY-RUN" if o["dry_run"] else "replayed"
            self.stdout.write(self.style.SUCCESS(
                f"{mode} {n} fixture message(s) through the listener."))
            return

        if not o["live"]:
            raise CommandError(
                "Nothing to do. Pass --fixture <file> (optionally --dry-run), or --live "
                "to connect an aged authorised session.")

        # --- live path (lazy Telethon; not exercised by tests) -------------
        api_id = os.environ.get("TELEGRAM_API_ID")
        api_hash = os.environ.get("TELEGRAM_API_HASH")
        session = os.environ.get("TELEGRAM_STRING_SESSION")
        if not (api_id and api_hash and session):
            raise CommandError(
                "Live mode needs TELEGRAM_API_ID / TELEGRAM_API_HASH / "
                "TELEGRAM_STRING_SESSION in the environment (never on the CLI).")
        try:
            from telethon.sync import TelegramClient
            from telethon.sessions import StringSession
            from telethon import events
        except ImportError:
            raise CommandError("Telethon not installed: pip install -r "
                               "backend/requirements-telegram.txt")

        device = {
            "device_model": os.environ.get("TELEGRAM_DEVICE_MODEL", "Desktop"),
            "system_version": os.environ.get("TELEGRAM_SYSTEM_VERSION", "Windows 10"),
            "app_version": os.environ.get("TELEGRAM_APP_VERSION", "4.16.8"),
            "system_lang_code": "en", "lang_code": "en",
        }
        client = TelegramClient(StringSession(session), int(api_id), api_hash, **device)
        client.connect()  # uses the existing session; never logs in
        self.stdout.write("Wayond listener: connected (read-only). Catching up + listening…")
        try:
            listener.run(client, events)
        finally:
            client.disconnect()
