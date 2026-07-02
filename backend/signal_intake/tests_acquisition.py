"""
GFX-PKT-SIGNAL-ACQUISITION-MVP-CORE tests (Phase 1 — fixture dicts, no Telegram).

Proves the dispatcher fail-closes every untrusted/stale/edited/media/unknown/
malformed/non-armed message into a STALE/QUARANTINED/DROPPED ledger row, records a
fresh tradeable signal into the existing intake ladder (no order), dedups replays,
records updates, advances the watermark, and NEVER imports execution or places an
order.
"""

import ast
import datetime as dt
import pathlib
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from signal_intake.acquisition import acquire_message
from signal_intake.models import (
    AcquiredMessage,
    ParserProfile,
    PendingSignalApproval,
    SignalProvider,
    SignalUpdate,
)

SIGNAL_MSG = (
    "XAUUSD | Potential downward movement\n\nXAUUSD | SELL 3350.0\n\n"
    "❌ Stop Loss 3360.0 (100 pips)\n\n✅ TP1 3335.0\n✅ TP2 3320.0"
)
UPDATE_MSG = "TP1 hit! +150 pips. Move SL to 3350.0"


class AcquisitionDispatcherTests(TestCase):
    def setUp(self):
        self.profile = ParserProfile.objects.create(slug="wayond_v1")
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="1001", chat_title="Wayond",
            parser_profile=self.profile, status=SignalProvider.Status.ARMED,
            acquisition_window_seconds=600,
        )
        self.now = timezone.now()

    def _msg(self, mid, text=SIGNAL_MSG, age_s=5, **extra):
        m = {"message_id": mid, "chat_id": "1001", "text": text,
             "date": self.now - dt.timedelta(seconds=age_s)}
        m.update(extra)
        return m

    def _acq(self, msg):
        return acquire_message(self.provider, msg, now=self.now)

    O = AcquiredMessage.Outcome

    # --- fresh tradeable signal -> intake (no order) -----------------------
    def test_fresh_signal_intaken(self):
        acq = self._acq(self._msg("1"))
        self.assertEqual(acq.outcome, self.O.INTAKEN)
        appr = acq.approval
        self.assertIsNotNone(appr)
        self.assertEqual(appr.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertEqual(appr.provider_id, self.provider.id)   # linked
        self.assertEqual(appr.source, "wayond")                # provider slug
        self.assertTrue(appr.correlation_id)
        self.assertEqual(PendingSignalApproval.objects.count(), 1)

    def test_watermark_and_last_signal_advance(self):
        self._acq(self._msg("7"))
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id, "7")
        self.assertIsNotNone(self.provider.last_signal_at)

    # --- dedup / replay ----------------------------------------------------
    def test_duplicate_is_idempotent(self):
        a1 = self._acq(self._msg("1"))
        a2 = self._acq(self._msg("1"))  # replay
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(AcquiredMessage.objects.filter(provider=self.provider, message_id="1").count(), 1)
        self.assertEqual(PendingSignalApproval.objects.count(), 1)  # not re-created

    # --- staleness window --------------------------------------------------
    def test_stale_message_dismissed(self):
        acq = self._acq(self._msg("2", age_s=20 * 60))  # 20 min > 600s
        self.assertEqual(acq.outcome, self.O.STALE)
        self.assertIsNone(acq.approval)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)  # never parsed

    # --- edit / media / empty guards --------------------------------------
    def test_edited_message_quarantined(self):
        acq = self._acq(self._msg("3", edit_date=1))
        self.assertEqual(acq.outcome, self.O.QUARANTINED)
        self.assertEqual(acq.reason, "edited_message")
        self.assertEqual(PendingSignalApproval.objects.count(), 0)

    def test_media_message_quarantined(self):
        acq = self._acq(self._msg("4", text="", media=True))
        self.assertEqual(acq.outcome, self.O.QUARANTINED)
        self.assertEqual(acq.reason, "media")

    def test_empty_text_quarantined(self):
        acq = self._acq(self._msg("5", text="   "))
        self.assertEqual(acq.outcome, self.O.QUARANTINED)

    # --- update recording --------------------------------------------------
    def test_update_recorded_not_intaken(self):
        acq = self._acq(self._msg("6", text=UPDATE_MSG))
        self.assertEqual(acq.outcome, self.O.UPDATE)
        self.assertIsNone(acq.approval)
        self.assertEqual(SignalUpdate.objects.filter(provider=self.provider).count(), 1)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)

    # --- unknown / malformed ----------------------------------------------
    def test_unknown_message_quarantined(self):
        acq = self._acq(self._msg("8", text="gm everyone, good luck today"))
        self.assertEqual(acq.outcome, self.O.QUARANTINED)

    def test_unknown_parser_profile_fails_closed(self):
        self.profile.slug = "does-not-exist"
        self.profile.save(update_fields=["slug"])
        acq = self._acq(self._msg("9"))
        self.assertEqual(acq.outcome, self.O.QUARANTINED)
        self.assertTrue(acq.reason.startswith("dispatch_error"))
        self.assertEqual(PendingSignalApproval.objects.count(), 0)  # fail closed, no signal

    # --- non-armed provider ------------------------------------------------
    def test_paused_provider_dropped(self):
        self.provider.status = SignalProvider.Status.PAUSED
        self.provider.save(update_fields=["status"])
        acq = self._acq(self._msg("10"))
        self.assertEqual(acq.outcome, self.O.DROPPED_NOT_ARMED)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id, "")  # not advanced

    # --- onboard command ---------------------------------------------------
    def test_onboard_command_creates_and_arms(self):
        ParserProfile.objects.get_or_create(slug="wayond_v1")
        call_command("onboard_provider", "--slug", "prov2", "--chat-id", "2002",
                     "--parser", "wayond_v1", stdout=StringIO())
        p = SignalProvider.objects.get(slug="prov2")
        self.assertEqual(p.status, SignalProvider.Status.ONBOARDING)
        call_command("onboard_provider", "--slug", "prov2", "--arm", stdout=StringIO())
        p.refresh_from_db()
        self.assertEqual(p.status, SignalProvider.Status.ARMED)

    def test_onboard_arm_without_chat_id_refused(self):
        from django.core.management.base import CommandError
        ParserProfile.objects.get_or_create(slug="wayond_v1")
        call_command("onboard_provider", "--slug", "prov3", "--parser", "wayond_v1", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("onboard_provider", "--slug", "prov3", "--arm", stdout=StringIO())

    # --- boundary proof ----------------------------------------------------
    def test_acquisition_does_not_import_execution_or_order_send(self):
        src = pathlib.Path(__file__).with_name("acquisition.py").read_text()
        tree = ast.parse(src)
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                imported.add(n.module or "")
        self.assertFalse(any(m.startswith("execution") for m in imported),
                         f"acquisition must not import execution; got {imported}")
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertNotIn("order_send", attrs)
        self.assertNotIn("MetaTrader5", src)
