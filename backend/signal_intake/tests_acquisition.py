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
    MessageAmendment,
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

    def test_indeterminate_date_dismissed_fail_closed(self):
        acq = self._acq({"message_id": "2b", "chat_id": "1001", "text": SIGNAL_MSG})  # no date
        self.assertEqual(acq.outcome, self.O.STALE)
        self.assertEqual(acq.reason, "indeterminate_date")
        self.assertEqual(PendingSignalApproval.objects.count(), 0)  # freshness unknown → not parsed

    # --- WAYOND-EDIT-MEDIA policy (ratified PR #72) -----------------------
    def test_edited_entry_intaken_flagged_for_human_review(self):
        # Edited entry is NOT dropped — surfaced to the human-approval gate FLAGGED.
        acq = self._acq(self._msg("3", edit_date=1))
        self.assertEqual(acq.outcome, self.O.INTAKEN)
        self.assertEqual(acq.reason, "edited_review")
        appr = acq.approval
        self.assertIsNotNone(appr)
        self.assertTrue(appr.source_edited)                                     # visibly flagged
        self.assertEqual(appr.status, PendingSignalApproval.Status.PENDING_APPROVAL)  # human-gated, not auto-traded

    def test_media_only_message_quarantined(self):
        # Screenshot-only (media, no parseable text) still quarantines.
        acq = self._acq(self._msg("4", text="", media=True))
        self.assertEqual(acq.outcome, self.O.QUARANTINED)
        self.assertEqual(acq.reason, "media_only")

    def test_empty_text_quarantined(self):
        acq = self._acq(self._msg("5", text="   "))
        self.assertEqual(acq.outcome, self.O.QUARANTINED)
        self.assertEqual(acq.reason, "empty_text")

    def test_media_with_text_is_parsed_media_retained_as_evidence(self):
        acq = self._acq(self._msg("m1", text=SIGNAL_MSG, media=True))
        self.assertEqual(acq.outcome, self.O.INTAKEN)          # text-bearing media parsed
        self.assertIsNotNone(acq.approval)
        self.assertFalse(acq.approval.source_edited)
        self.assertTrue(acq.raw_payload.get("media"))
        self.assertIsNotNone(acq.raw_payload.get("media_evidence"))  # evidence retained

    def test_edited_update_recorded_never_intaken(self):
        acq = self._acq(self._msg("u-e", text=UPDATE_MSG, edit_date=1))
        self.assertEqual(acq.outcome, self.O.UPDATE)
        su = SignalUpdate.objects.get(provider=self.provider, message_id="u-e")
        self.assertTrue(su.raw_payload.get("edited"))
        self.assertEqual(PendingSignalApproval.objects.count(), 0)   # never intaken

    def test_reply_update_links_to_originating_message(self):
        orig = self._acq(self._msg("orig1"))                         # original entry
        upd = self._acq(self._msg("rep1", text=UPDATE_MSG, reply_to_message_id="orig1"))
        self.assertEqual(upd.outcome, self.O.UPDATE)
        su = SignalUpdate.objects.get(provider=self.provider, message_id="rep1")
        self.assertEqual(su.reply_to_message_id, "orig1")
        self.assertEqual(su.raw_payload.get("origin_acquired_id"), orig.id)   # linked

    def test_edit_same_values_records_amendment_no_reflag(self):
        # WAYOND-EDIT-DIFF: an edit that does NOT change entry/SL/TP records an
        # immutable amendment but does not re-flag the approval; original untouched.
        a1 = self._acq(self._msg("e1"))                         # clean entry -> INTAKEN
        self.assertFalse(a1.approval.source_edited)
        a2 = self._acq(self._msg("e1", edit_date=1))            # edit, same values
        self.assertEqual(a1.id, a2.id)                          # original deduped
        self.assertEqual(PendingSignalApproval.objects.count(), 1)   # no duplicate approval
        am = MessageAmendment.objects.get(original=a1, message_id="e1")
        self.assertEqual(am.changed_fields, {})
        self.assertFalse(am.approval_reflagged)
        a1.approval.refresh_from_db()
        self.assertFalse(a1.approval.source_edited)             # not re-flagged (unchanged)

    def test_edit_changing_sl_flags_approval_for_rereview(self):
        # An edit that CHANGES entry/SL/TP creates an amendment with the diff AND flags
        # the related approval for human re-review — never auto-applied.
        orig_text = ("XAUUSD | SELL 3350.0\n❌ Stop Loss 3360.0 (100 pips)\n✅ TP1 3335.0")
        edited_text = ("XAUUSD | SELL 3350.0\n❌ Stop Loss 3400.0 (500 pips)\n✅ TP1 3335.0")
        a1 = self._acq(self._msg("e2", text=orig_text))
        self.assertEqual(a1.outcome, self.O.INTAKEN)
        a2 = self._acq(self._msg("e2", text=edited_text, edit_date=1))
        self.assertEqual(a1.id, a2.id)                          # original not overwritten
        am = MessageAmendment.objects.get(original=a1, message_id="e2")
        self.assertEqual(am.changed_fields.get("stop_loss"), ["3360.0", "3400.0"])
        self.assertTrue(am.approval_reflagged)
        a1.approval.refresh_from_db()
        self.assertTrue(a1.approval.source_edited)              # flagged for re-review
        self.assertEqual(a1.approval.stop_loss, "3360.0")       # ORIGINAL value NOT auto-applied
        self.assertEqual(a1.approval.status,
                         PendingSignalApproval.Status.PENDING_APPROVAL)  # not auto-actioned

    def test_edit_is_idempotent_no_duplicate_amendment(self):
        a1 = self._acq(self._msg("e3"))
        self._acq(self._msg("e3", edit_date=1))
        self._acq(self._msg("e3", edit_date=2))                 # same edited text re-delivered
        self.assertEqual(MessageAmendment.objects.filter(original=a1).count(), 1)

    def test_amended_update_is_recorded_only(self):
        a1 = self._acq(self._msg("u1", text=UPDATE_MSG))        # original update
        self.assertEqual(a1.outcome, self.O.UPDATE)
        edited = "TP2 hit! +250 pips. Move SL to 3400.0"        # edited update (changed text)
        self._acq(self._msg("u1", text=edited, edit_date=1))
        am = MessageAmendment.objects.get(original=a1, message_id="u1")
        self.assertEqual(am.reparsed_kind, "UPDATE")
        self.assertTrue(am.raw_payload.get("amended_update"))
        self.assertEqual(SignalUpdate.objects.filter(message_id="u1").count(), 2)  # orig + amended
        self.assertEqual(PendingSignalApproval.objects.count(), 0)   # updates never intaken

    def test_true_duplicate_unchanged_records_no_amendment(self):
        a1 = self._acq(self._msg("d1"))
        self._acq(self._msg("d1"))                              # identical, no edit
        self.assertEqual(MessageAmendment.objects.filter(original=a1).count(), 0)  # dedup preserved

    def test_media_evidence_is_a_bounded_reference_not_bytes(self):
        # A buggy/hostile listener passing a huge blob must NOT land bytes in the DB.
        big = {"file_id": "abc123", "type": "photo", "bytes": "X" * 100000, "junk": [1] * 9999}
        acq = self._acq(self._msg("mv", text=SIGNAL_MSG, media=big))
        ev = acq.raw_payload.get("media_evidence")
        self.assertEqual(ev, {"file_id": "abc123", "type": "photo"})   # only whitelisted refs
        self.assertNotIn("bytes", ev)
        self.assertNotIn("junk", ev)

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
