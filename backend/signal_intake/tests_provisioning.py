"""
GFX-PKT-TELEGRAM-ACCOUNT-PROVISIONING tests (safe logic only — no Telethon, no login).

Proves the session file is written 600 and never printed by default, --print-secret
gates the loud-warning disclosure, only safe metadata is formatted (never phone),
and the command guards on missing API creds / missing Telethon.
"""

import os
import stat
import sys
import tempfile
import types
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from signal_intake.management.commands.provision_telegram_session import (
    emit_session,
    format_metadata,
    write_session_file,
)

SESSION = "1BVtsOI4-FAKE-STRINGSESSION-should-never-be-logged"
PHONE = "+639999999999"  # the GFX account phone — must NEVER appear in output


class _Me:
    id = 777
    first_name = "GFX"
    last_name = ""
    username = "gfx_guvfx"
    phone = PHONE  # must NEVER appear in output


class _Chat:
    id = 42
    title = "Wayond | FX Signals"
    username = "wayond"


def _fake_telethon(me, *, chat=None, msgs=None, session=SESSION, start_exc=None,
                   captured=None):
    """A stand-in telethon package injected into sys.modules so the command's lazy
    import resolves without the real library or any network/login. If ``captured`` is
    given, the TelegramClient constructor args/kwargs are recorded into it."""
    telethon = types.ModuleType("telethon")
    sync = types.ModuleType("telethon.sync")
    sessions = types.ModuleType("telethon.sessions")

    class FakeSession:
        def save(self):
            return session

    class FakeStringSession:
        def __init__(self, *a, **k):
            pass

    class FakeClient:
        def __init__(self, *a, **k):
            if captured is not None:
                captured["args"] = a
                captured["kwargs"] = k
            self.session = FakeSession()
            self.disconnected = False

        def start(self, phone=None, code_callback=None):
            if phone:
                phone()               # exercise the closures like Telethon would
            if code_callback:
                code_callback()
            if start_exc:
                raise start_exc

        def get_me(self):
            return me

        def get_entity(self, _):
            return chat

        def get_messages(self, _entity, limit=1):
            return msgs or []

        def disconnect(self):
            self.disconnected = True

    sync.TelegramClient = FakeClient
    sessions.StringSession = FakeStringSession
    telethon.sync = sync
    telethon.sessions = sessions
    return {"telethon": telethon, "telethon.sync": sync, "telethon.sessions": sessions}


class ProvisioningHelperTests(SimpleTestCase):
    def _tmp(self):
        import tempfile
        return os.path.join(tempfile.mkdtemp(), "sub", "telegram_gfx.session")

    def test_write_session_file_is_600(self):
        path = write_session_file(SESSION, self._tmp())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            self.assertEqual(fh.read(), SESSION)

    def test_write_refuses_symlink_at_destination(self):
        d = tempfile.mkdtemp()
        loot = os.path.join(d, "attacker_loot.txt")
        link = os.path.join(d, "telegram_gfx.session")
        os.symlink(loot, link)  # attacker plants a symlink at the destination
        with self.assertRaises(CommandError):
            write_session_file(SESSION, link)
        # The credential was NOT written through the link.
        self.assertFalse(os.path.exists(loot))

    def test_write_tightens_pre_existing_loose_file(self):
        path = self._tmp()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("stale")
        os.chmod(path, 0o644)  # pre-existing world-readable file
        write_session_file(SESSION, path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            self.assertEqual(fh.read(), SESSION)

    def test_emit_does_not_print_secret_by_default(self):
        out = StringIO()
        path = emit_session(SESSION, self._tmp(), print_secret=False, stdout=out)
        self.assertNotIn(SESSION, out.getvalue())        # secret NOT printed
        self.assertIn("NOT printed", out.getvalue())
        self.assertTrue(os.path.exists(path))

    def test_emit_prints_secret_with_flag_and_warning(self):
        out = StringIO()
        emit_session(SESSION, self._tmp(), print_secret=True, stdout=out)
        text = out.getvalue()
        self.assertIn(SESSION, text)                     # explicitly requested
        self.assertIn("SECURITY WARNING", text)          # loud warning present

    def test_metadata_is_safe_no_phone(self):
        md = format_metadata(_Me(), _Chat(), latest_message_id=1234)
        self.assertEqual(md["telegram_user_id"], 777)
        self.assertEqual(md["display_name"], "GFX")
        self.assertEqual(md["chat_title"], "Wayond | FX Signals")
        self.assertEqual(md["chat_id"], 42)
        self.assertEqual(md["latest_message_id"], 1234)
        self.assertNotIn("phone", md)
        self.assertNotIn("+639999999999", str(md))       # phone never leaks

    def test_metadata_without_chat(self):
        md = format_metadata(_Me())
        self.assertNotIn("chat_id", md)


class ProvisioningCommandGuardTests(SimpleTestCase):
    def test_missing_api_creds_errors(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(CommandError) as cm:
                call_command("provision_telegram_session", stdout=StringIO())
        self.assertIn("TELEGRAM_API_ID", str(cm.exception))

    def test_missing_telethon_errors_with_install_hint(self):
        # Force the lazy Telethon import to fail regardless of whether the library
        # happens to be installed here (None in sys.modules makes import raise).
        with mock.patch.dict(os.environ, {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h"}), \
                mock.patch.dict(sys.modules, {"telethon": None, "telethon.sync": None,
                                              "telethon.sessions": None}):
            with self.assertRaises(CommandError) as cm:
                call_command("provision_telegram_session", stdout=StringIO())
        self.assertIn("Telethon", str(cm.exception))


class ProvisioningHandleIntegrationTests(SimpleTestCase):
    """Exercise the full command.handle() flow against a fake Telethon (no login),
    proving the phone and session never reach stdout and errors stay sanitised."""

    def _out_path(self):
        return os.path.join(tempfile.mkdtemp(), "telegram_gfx.session")

    def _run(self, fake, extra=None, api_id="12345"):
        out = StringIO()
        env = {"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": "hash",
               "TELEGRAM_PHONE": PHONE}
        path = self._out_path()
        args = ["provision_telegram_session", "--session-out", path] + (extra or [])
        with mock.patch.dict(sys.modules, fake), \
                mock.patch.dict(os.environ, env), \
                mock.patch("builtins.input", return_value="00000"):
            call_command(*args, stdout=out)
        return out.getvalue(), path

    def test_happy_path_prints_metadata_not_phone_or_session(self):
        fake = _fake_telethon(_Me(), chat=_Chat(),
                              msgs=[types.SimpleNamespace(id=99)])
        text, path = self._run(fake, extra=["--wayond-chat", "wayond"])
        self.assertIn("777", text)                       # metadata IS shown
        self.assertIn("Wayond | FX Signals", text)
        self.assertIn("99", text)                        # latest message id
        self.assertNotIn(PHONE, text)                    # phone NEVER printed
        self.assertNotIn(SESSION, text)                  # session NEVER printed
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_print_secret_flag_shows_session_in_full_flow(self):
        fake = _fake_telethon(_Me())
        text, _ = self._run(fake, extra=["--print-secret"])
        self.assertIn(SESSION, text)                     # explicitly requested
        self.assertIn("SECURITY WARNING", text)
        self.assertNotIn(PHONE, text)                    # phone still never printed

    def test_login_failure_is_sanitised_no_phone_leak(self):
        # Telethon raises with the phone embedded in the message — must not leak.
        fake = _fake_telethon(_Me(), start_exc=RuntimeError(f"{PHONE} is banned"))
        with self.assertRaises(CommandError) as cm:
            self._run(fake)
        self.assertNotIn(PHONE, str(cm.exception))
        self.assertIn("Telegram login failed", str(cm.exception))

    def test_bad_api_id_is_sanitised_no_value_leak(self):
        fake = _fake_telethon(_Me())
        with self.assertRaises(CommandError) as cm:
            self._run(fake, api_id="not-an-int")
        self.assertNotIn("not-an-int", str(cm.exception))
        self.assertIn("TELEGRAM_API_ID", str(cm.exception))

    def _run_capturing(self, extra_env=None):
        """Run the full flow against a fake telethon and return the captured
        TelegramClient constructor kwargs."""
        cap = {}
        fake = _fake_telethon(_Me(), captured=cap)
        env = {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "hash",
               "TELEGRAM_PHONE": PHONE}
        if extra_env:
            env.update(extra_env)
        path = os.path.join(tempfile.mkdtemp(), "s.session")
        with mock.patch.dict(sys.modules, fake), \
                mock.patch.dict(os.environ, env), \
                mock.patch("builtins.input", return_value="00000"):
            call_command("provision_telegram_session", "--session-out", path,
                         stdout=StringIO())
        return cap["kwargs"]

    def test_client_gets_stable_non_default_device_fingerprint(self):
        kw = self._run_capturing()
        for key in ("device_model", "system_version", "app_version",
                    "system_lang_code", "lang_code"):
            self.assertIn(key, kw)          # explicitly set, not Telethon's default
            self.assertTrue(kw[key])        # non-empty

    def test_device_fingerprint_is_env_overridable(self):
        kw = self._run_capturing(extra_env={
            "TELEGRAM_DEVICE_MODEL": "MyBox",
            "TELEGRAM_SYSTEM_VERSION": "Ubuntu 24.04",
            "TELEGRAM_APP_VERSION": "9.9.9",
        })
        self.assertEqual(kw["device_model"], "MyBox")
        self.assertEqual(kw["system_version"], "Ubuntu 24.04")
        self.assertEqual(kw["app_version"], "9.9.9")
