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
    """Write the session to a 0600 file (parent dir 0700). Returns the path.

    The file is created atomically at mode 0600 (no world-readable window) with
    O_NOFOLLOW (never write the credential *through* a symlink an attacker planted
    at the destination) and its mode is re-asserted with fchmod so a pre-existing
    looser-permission file is tightened before the secret is written. The session
    string is never returned to the caller for logging.
    """
    path = Path(out_path).expanduser()
    path.parent.mkdir(mode=stat.S_IRWXU, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)  # 0700 (best-effort; own dirs only)
    except OSError:
        pass
    # 0600 on O_CREAT => the file is never briefly world-readable. O_NOFOLLOW =>
    # fail closed rather than follow a symlink at the final path component.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise CommandError(
            f"Refusing to write the session file at {path} ({type(exc).__name__}). "
            "It may be a symlink, or the directory may not be writable — remove any "
            "existing link and retry."
        )
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # enforce 0600 on a reused file
        os.write(fd, session_str.encode("utf-8"))
    finally:
        os.close(fd)
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

        try:
            api_id_int = int(api_id)
        except (TypeError, ValueError):
            # Never echo the malformed value — API_ID is a credential.
            raise CommandError(
                "TELEGRAM_API_ID must be an integer (from https://my.telegram.org). "
                "Its value is not shown here for safety."
            )

        phone = os.environ.get("TELEGRAM_PHONE") or input(
            "Enter the GFX account phone number (PH number, e.g. +63...): "
        ).strip()

        self.stdout.write("Starting Telegram login for the DEDICATED GuvFX account…")
        self.stdout.write("Telegram will send a login code to that account — type it below.")
        # Stable, realistic device fingerprint. Telethon's library defaults advertise
        # the client as an automated Telethon session — a known flag for fresh
        # accounts — and a fingerprint that changes between logins is itself a flag.
        # Source these from env and FREEZE them: the listener MUST reuse identical
        # values so Telegram sees one stable device, not a new one each connect.
        device_kwargs = dict(
            device_model=os.environ.get("TELEGRAM_DEVICE_MODEL", "Desktop"),
            system_version=os.environ.get("TELEGRAM_SYSTEM_VERSION", "Windows 10"),
            app_version=os.environ.get("TELEGRAM_APP_VERSION", "4.16.8"),
            system_lang_code="en",
            lang_code="en",
        )
        client = TelegramClient(StringSession(), api_id_int, api_hash, **device_kwargs)
        try:
            # No 2FA yet (deferred): start() prompts for the code only. If a 2FA
            # password is ever set, Telethon will additionally prompt for it.
            try:
                client.start(
                    phone=lambda: phone,
                    code_callback=lambda: input(
                        "Enter the login code Telegram sent to GFX: "
                    ).strip(),
                )
            except Exception as exc:
                # The raw exception text can embed the phone number — surface the
                # exception *type* only, never its message.
                raise CommandError(
                    f"Telegram login failed: {type(exc).__name__}. Check the phone "
                    "number and login code and retry. (Details omitted for safety.)"
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
            emit_session(session_str, o["session_out"],
                         print_secret=o["print_secret"], stdout=self.stdout)
        finally:
            # Always release the Telegram connection, even on a mid-flow error.
            try:
                client.disconnect()
            except Exception:
                pass

        self.stdout.write(self.style.SUCCESS(
            "\nDone. Next: hand the session file to the deploy secret store (env, 600). "
            "Do NOT commit it. Cleanup/revoke: to invalidate this session, on the GFX "
            "account go Settings → Devices → terminate the session, then re-run this "
            "command to mint a fresh one. Enable 2FA after verification."
        ))
