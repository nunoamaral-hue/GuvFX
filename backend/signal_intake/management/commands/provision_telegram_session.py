"""
GFX-PKT-TELEGRAM-ACCOUNT-PROVISIONING — interactive session generator (operator-run).

Generates + verifies the Telethon StringSession for the DEDICATED GuvFX Telegram
account (the PH-number "GFX" account — never Nuno's personal UK account). It logs
in interactively (Nuno types the code Telegram sends), verifies the account
identity and access to the Wayond source, and writes the session to a local
600-mode file. It does NOT print the session by default, does NOT ingest messages,
arm a provider, deploy, or touch execution.

Run it where Telethon is installed (see docs/TELEGRAM_PROVISIONING.md):

    TELEGRAM_API_ID=... TELEGRAM_API_HASH=... \
      python manage.py provision_telegram_session \
        --session-out ~/.guvfx/telegram_gfx.session \
        --wayond-chat <@username_or_id_or_link>

Add --print-secret ONLY if you must display the session string (loud warning).
"""

import os
import stat
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

PRINT_SECRET_WARNING = (
    "\n" + "!" * 70 + "\n"
    "!! SECURITY WARNING — the Telethon StringSession below is a FULL CREDENTIAL.\n"
    "!! Anyone with it can act as the GuvFX Telegram account. Do NOT paste it into\n"
    "!! chat/logs/tickets. Clear your terminal + shell history after copying it.\n"
    + "!" * 70 + "\n"
)


def write_session_file(session_str: str, out_path: str) -> str:
    """Write the session to a 600-mode file (parent dirs 700). Returns the path.
    The session string is never returned to the caller for logging."""
    path = Path(out_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)  # 700 (best-effort)
    except OSError:
        pass
    with open(path, "w") as fh:
        fh.write(session_str)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    return str(path)


def format_metadata(me, chat=None, latest_message_id=None) -> dict:
    """Only SAFE, non-secret identity metadata — never phone, never session."""
    name = " ".join(p for p in (getattr(me, "first_name", ""),
                                getattr(me, "last_name", "")) if p).strip()
    md = {
        "telegram_user_id": getattr(me, "id", None),
        "display_name": name,
        "username": getattr(me, "username", None),
    }
    if chat is not None:
        md["chat_title"] = getattr(chat, "title", None) or getattr(chat, "username", None)
        md["chat_id"] = getattr(chat, "id", None)
        md["latest_message_id"] = latest_message_id
    return md


def emit_session(session_str: str, out_path: str, *, print_secret: bool, stdout) -> str:
    """Write the session file; print the secret ONLY with an explicit flag + warning."""
    path = write_session_file(session_str, out_path)
    stdout.write(f"session written to {path} (mode 600) — NOT printed to stdout")
    if print_secret:
        stdout.write(PRINT_SECRET_WARNING)
        stdout.write(session_str)
        stdout.write(PRINT_SECRET_WARNING)
    return path


class Command(BaseCommand):
    help = (
        "Interactively generate + verify the dedicated GuvFX Telegram StringSession. "
        "Writes it to a 600-mode file; never prints it unless --print-secret."
    )

    def add_arguments(self, parser):
        parser.add_argument("--session-out", default="~/.guvfx/telegram_gfx.session",
                            help="Destination file for the StringSession (chmod 600).")
        parser.add_argument("--wayond-chat", default=None,
                            help="Wayond chat @username / id / link to verify access.")
        parser.add_argument("--print-secret", action="store_true",
                            help="DANGER: also print the session string (loud warning).")

    def handle(self, *args, **o):
        api_id = os.environ.get("TELEGRAM_API_ID")
        api_hash = os.environ.get("TELEGRAM_API_HASH")
        if not api_id or not api_hash:
            raise CommandError(
                "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in the environment "
                "(from https://my.telegram.org — App configuration). They are "
                "credentials: never pass them as CLI args, never commit them."
            )

        try:  # lazy import so the backend/tests do not depend on Telethon
            from telethon.sync import TelegramClient
            from telethon.sessions import StringSession
        except ImportError:
            raise CommandError(
                "Telethon is not installed. In the environment where you run this, "
                "install it: pip install -r backend/requirements-telegram.txt"
            )

        phone = os.environ.get("TELEGRAM_PHONE") or input(
            "Enter the GFX account phone number (PH number, e.g. +63...): "
        ).strip()

        self.stdout.write("Starting Telegram login for the DEDICATED GuvFX account…")
        self.stdout.write("Telegram will send a login code to that account — type it below.")
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        # No 2FA yet (deferred): start() prompts for the code only. If a 2FA
        # password is ever set, Telethon will additionally prompt for it.
        client.start(
            phone=lambda: phone,
            code_callback=lambda: input("Enter the login code Telegram sent to GFX: ").strip(),
        )

        me = client.get_me()
        chat = None
        latest_id = None
        if o["wayond_chat"]:
            try:
                chat = client.get_entity(o["wayond_chat"])
                msgs = client.get_messages(chat, limit=1)
                latest_id = msgs[0].id if msgs else None
            except Exception as exc:  # verification failure must be visible, not fatal
                self.stdout.write(self.style.WARNING(
                    f"Could not verify Wayond access for {o['wayond_chat']!r}: "
                    f"{type(exc).__name__}"
                ))

        self.stdout.write(self.style.SUCCESS("Login OK. Safe metadata:"))
        for k, v in format_metadata(me, chat, latest_id).items():
            self.stdout.write(f"  {k}: {v}")

        session_str = client.session.save()
        emit_session(session_str, o["session_out"], print_secret=o["print_secret"], stdout=self.stdout)
        client.disconnect()

        self.stdout.write(self.style.SUCCESS(
            "\nDone. Next: hand the session file to the deploy secret store (env, 600). "
            "Do NOT commit it. Cleanup/revoke: to invalidate this session, on the GFX "
            "account go Settings → Devices → terminate the session, then re-run this "
            "command to mint a fresh one. Enable 2FA after verification."
        ))
