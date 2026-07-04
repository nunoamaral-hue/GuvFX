"""
GFX-PKT-SIGNAL-ACQUISITION-LISTENER-BUILD tests — FAKE Telethon only.

No real Telegram: a fake client/events/messages exercise normalisation, provider
lookup, watermark catch-up, flood-wait handling, dry-run, the live run() wiring, and
the read-only boundary. Proves the listener feeds ONLY acquire_message and never
sends / downloads / imports execution.
"""

import ast
import json
import os
import pathlib
import re
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from signal_intake.listener import (
    ListenerNotAuthorized,
    WayondListener,
    normalize_message,
)
from signal_intake.models import (
    AcquiredMessage,
    ParserProfile,
    PendingSignalApproval,
    SignalProvider,
)

SIGNAL_MSG = ("XAUUSD | SELL 3350.0\n❌ Stop Loss 3360.0 (100 pips)\n✅ TP1 3335.0")


class FakeMsg:
    """A raw Telethon-ish message (attribute access)."""
    def __init__(self, id, chat_id, text="", date=None, reply_to_msg_id=None,
                 edit_date=None, media=None):
        self.id = id
        self.chat_id = chat_id
        self.message = text
        self.date = date
        self.reply_to_msg_id = reply_to_msg_id
        self.edit_date = edit_date
        self.media = media


class _Photo:
    def __init__(self, id):
        self.id = id


class FakeMediaPhoto:
    def __init__(self, id):
        self.photo = _Photo(id)


class _MediaWithId:
    """A media object whose reference id may be hostile/large (bounds test)."""
    def __init__(self, id):
        self.photo = _Photo(id)


class FloodWaitError(Exception):
    """Named exactly like Telethon's so the adapter detects it by class name."""
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"flood wait {seconds}")


class FakeClient:
    def __init__(self, messages=None, authorized=True):
        self._messages = messages or []
        self._authorized = authorized
        self.handlers = []
        self.ran = False
        self.disconnected = False

    def is_user_authorized(self):
        return self._authorized

    def iter_messages(self, entity, min_id=0, limit=200, reverse=False):
        msgs = [m for m in self._messages if getattr(m, "id", 0) > min_id]
        msgs.sort(key=lambda m: m.id, reverse=not reverse)
        return msgs[:limit]

    def add_event_handler(self, cb, event):
        self.handlers.append((cb, event))

    def run_until_disconnected(self):
        self.ran = True

    def disconnect(self):
        self.disconnected = True


class FakeEvents:
    class NewMessage:
        def __init__(self, chats=None):
            self.chats = chats

    class MessageEdited:
        def __init__(self, chats=None):
            self.chats = chats


class NormalizeTests(SimpleTestCase):
    def test_normalizes_telethon_object_media_is_reference_not_bytes(self):
        raw = FakeMsg(7, 1001, text="hi", reply_to_msg_id=3, edit_date=99,
                      media=FakeMediaPhoto(555))
        m = normalize_message(raw)
        self.assertEqual(m["message_id"], 7)
        self.assertEqual(m["chat_id"], 1001)
        self.assertEqual(m["text"], "hi")
        self.assertEqual(m["reply_to_message_id"], 3)
        self.assertEqual(m["edit_date"], 99)
        self.assertEqual(m["media"], {"type": "FakeMediaPhoto", "id": 555})  # ref, no bytes

    def test_normalizes_fixture_dict(self):
        m = normalize_message({"message_id": 5, "chat_id": 1001, "text": "gm",
                               "reply_to_message_id": 2, "media": {"type": "photo"}})
        self.assertEqual((m["message_id"], m["chat_id"], m["text"]), (5, 1001, "gm"))
        self.assertEqual(m["reply_to_message_id"], 2)
        self.assertEqual(m["media"], {"type": "photo"})


class ListenerCoreTests(TestCase):
    def setUp(self):
        self.profile = ParserProfile.objects.create(slug="wayond_v1")
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="1001", chat_title="Wayond",
            parser_profile=self.profile, status=SignalProvider.Status.ARMED,
            acquisition_window_seconds=600,
        )

    def _msg(self, mid, text=SIGNAL_MSG, chat=1001, **extra):
        return FakeMsg(mid, chat, text=text, date=timezone.now(), **extra)

    def test_acquire_raw_feeds_dispatcher(self):
        acq = WayondListener().acquire_raw(self._msg(1))
        self.assertIsNotNone(acq)
        self.assertEqual(acq.outcome, AcquiredMessage.Outcome.INTAKEN)
        self.assertEqual(AcquiredMessage.objects.filter(message_id="1").count(), 1)

    def test_dry_run_writes_nothing(self):
        out = WayondListener(dry_run=True).acquire_raw(self._msg(2))
        self.assertIsNone(out)
        self.assertEqual(AcquiredMessage.objects.count(), 0)      # preview only
        self.assertEqual(PendingSignalApproval.objects.count(), 0)

    def test_unknown_chat_ignored(self):
        out = WayondListener().acquire_raw(self._msg(3, chat=9999))  # no provider
        self.assertIsNone(out)
        self.assertEqual(AcquiredMessage.objects.count(), 0)

    def test_media_bounded_end_to_end_no_blob_stored(self):
        # A hostile/large media id must land only as a bounded reference in the DB.
        raw = self._msg(4, text=SIGNAL_MSG)
        raw.media = _MediaWithId("X" * 100000)                   # 100 KB string id
        acq = WayondListener().acquire_raw(raw)
        ev = acq.raw_payload.get("media_evidence")
        self.assertEqual(ev["type"], "_MediaWithId")
        self.assertLessEqual(len(str(ev.get("id", ""))), 256)    # bounded, not a blob

    def test_catch_up_respects_watermark(self):
        self.provider.watermark_last_message_id = "2"
        self.provider.save(update_fields=["watermark_last_message_id"])
        client = FakeClient(messages=[self._msg(1), self._msg(2), self._msg(3)])
        n = WayondListener().catch_up(client, self.provider)
        self.assertEqual(n, 1)                                    # only id > 2
        self.assertTrue(AcquiredMessage.objects.filter(message_id="3").exists())
        self.assertFalse(AcquiredMessage.objects.filter(message_id="1").exists())

    def test_run_wires_handlers_catches_up_and_blocks(self):
        client = FakeClient(messages=[self._msg(10)])
        with self.assertLogs("signal_intake.listener.adapter", level="INFO") as cm:
            WayondListener().run(client, FakeEvents)
        self.assertEqual(len(client.handlers), 2)                 # NewMessage + MessageEdited
        self.assertTrue(client.ran)                               # run_until_disconnected called
        self.assertTrue(AcquiredMessage.objects.filter(message_id="10").exists())  # caught up
        self.assertTrue(any("heartbeat" in m for m in cm.output))

    def test_run_refuses_unauthorised_session(self):
        with self.assertRaises(ListenerNotAuthorized):
            WayondListener().run(FakeClient(authorized=False), FakeEvents)

    def test_subscribed_chat_ids_excludes_inactive(self):
        SignalProvider.objects.create(
            slug="dead", name="Dead", telegram_chat_id="2002",
            parser_profile=self.profile, status=SignalProvider.Status.RETIRED)
        self.assertEqual(WayondListener().subscribed_chat_ids(), ["1001"])


class FloodWaitTests(SimpleTestCase):
    def test_floodwait_sleeps_requested_seconds_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise FloodWaitError(seconds=7)
            return "ok"

        with mock.patch("signal_intake.listener.adapter.time.sleep") as slept:
            result = WayondListener()._floodwait(flaky)
        self.assertEqual(result, "ok")
        slept.assert_called_once_with(7)

    def test_non_floodwait_propagates(self):
        with self.assertRaises(ValueError):
            WayondListener()._floodwait(lambda: (_ for _ in ()).throw(ValueError("x")))


class CommandTests(TestCase):
    def setUp(self):
        self.profile = ParserProfile.objects.create(slug="wayond_v1")
        SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="1001", chat_title="Wayond",
            parser_profile=self.profile, status=SignalProvider.Status.ARMED)

    def _fixture(self, dry):
        path = os.path.join(tempfile.mkdtemp(), "fx.json")
        with open(path, "w") as fh:
            json.dump({"messages": [{"message_id": 1, "chat_id": 1001, "text": SIGNAL_MSG,
                                     "date": timezone.now().timestamp()}]}, fh)
        return path

    def test_fixture_dry_run_writes_nothing(self):
        call_command("run_wayond_listener", "--fixture", self._fixture(True),
                     "--dry-run", stdout=StringIO())
        self.assertEqual(AcquiredMessage.objects.count(), 0)

    def test_fixture_replay_writes(self):
        call_command("run_wayond_listener", "--fixture", self._fixture(False), stdout=StringIO())
        self.assertEqual(AcquiredMessage.objects.filter(message_id="1").count(), 1)

    def test_no_args_errors(self):
        with self.assertRaises(CommandError):
            call_command("run_wayond_listener", stdout=StringIO())

    def test_live_without_creds_errors_before_any_telegram(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_STRING_SESSION")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(CommandError):
                call_command("run_wayond_listener", "--live", stdout=StringIO())


class BoundaryTests(SimpleTestCase):
    # Any message-mutating / sending / downloading Telethon method is forbidden.
    CALL = re.compile(
        r"\.(send_message|send_file|send|reply|respond|forward|forward_to|"
        r"forward_messages|edit|edit_message|delete|delete_messages|pin|unpin|"
        r"mark_read|download_media|download)\s*\(")

    def _sources(self):
        pkg = pathlib.Path(__file__).with_name("listener")
        files = list(pkg.rglob("*.py"))
        files.append(pathlib.Path(__file__).with_name("management")
                     / "commands" / "run_wayond_listener.py")
        return files

    def test_listener_never_imports_execution(self):
        for py in self._sources():
            tree = ast.parse(py.read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    self.assertFalse(any(a.name.startswith("execution") for a in n.names),
                                     f"{py.name} imports execution")
                elif isinstance(n, ast.ImportFrom):
                    self.assertFalse((n.module or "").startswith("execution"),
                                     f"{py.name} imports execution")

    def test_listener_never_sends_or_downloads(self):
        for py in self._sources():
            self.assertIsNone(self.CALL.search(py.read_text()),
                              f"{py.name} calls a send/download method")

    def test_listener_only_sink_is_acquire_message(self):
        # The adapter's only DB write path is acquire_message (no ExecutionJob etc.).
        src = (pathlib.Path(__file__).with_name("listener") / "adapter.py").read_text()
        self.assertIn("acquire_message", src)
        self.assertNotIn("ExecutionJob", src)
        self.assertNotIn("order_send", src)
