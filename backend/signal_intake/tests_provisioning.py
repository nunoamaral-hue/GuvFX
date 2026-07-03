"""
GFX-PKT-TELEGRAM-ACCOUNT-PROVISIONING tests (safe logic only — no Telethon, no login).

Proves the session file is written 600 and never printed by default, --print-secret
gates the loud-warning disclosure, only safe metadata is formatted (never phone),
and the command guards on missing API creds / missing Telethon.
"""

import os
import stat
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


class _Me:
    id = 777
    first_name = "GFX"
    last_name = ""
    username = "gfx_guvfx"
    phone = "+639999999999"  # must NEVER appear in output


class _Chat:
    id = 42
    title = "Wayond | FX Signals"
    username = "wayond"


class ProvisioningHelperTests(SimpleTestCase):
    def _tmp(self):
        import tempfile
        return os.path.join(tempfile.mkdtemp(), "sub", "telegram_gfx.session")

    def test_write_session_file_is_600(self):
        path = write_session_file(SESSION, self._tmp())
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
        # Creds present but Telethon not installed (as in CI) → clear install hint.
        with mock.patch.dict(os.environ, {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h"}):
            with self.assertRaises(CommandError) as cm:
                call_command("provision_telegram_session", stdout=StringIO())
        self.assertIn("Telethon", str(cm.exception))
