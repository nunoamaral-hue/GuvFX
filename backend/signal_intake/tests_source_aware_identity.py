"""
T1_SOURCE_AWARE_IDENTITY_AND_DEDUP — regression matrix (35 cases).

Proves the canonical acquisition identity is (provider/source, chat_id, message_id):
a Telegram message_id is chat-scoped, so a same-id message from a DIFFERENT chat is a
DISTINCT row and is NEVER deduped against — nor does it suppress — a historical message
of the same id from another chat (the cross-channel-switch defect this packet fixes).

Also proves the fail-closed chat-identity guard, chat-scoped replay/approvals/updates/
commands/amendments, that the execution read-path is untouched (chat is NOT leaked into
raw_payload), and the DB-level uniqueness on all three identity models.

Categories: identity · fail-closed guard · replay/watermark · approvals · updates ·
commands · amendments · execution-isolation · database.
"""

import datetime as dt

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from signal_intake import services
from signal_intake.acquisition import acquire_message
from signal_intake.listener.adapter import WayondListener
from signal_intake.models import (
    AcquiredMessage,
    MessageAmendment,
    ParserProfile,
    PendingSignalApproval,
    ProviderCommand,
    SignalProvider,
    SignalUpdate,
)
from signal_intake.tests_listener import FakeClient, FakeMsg

SIGNAL_MSG = (
    "XAUUSD | Potential downward movement\n\nXAUUSD | SELL 3350.0\n\n"
    "❌ Stop Loss 3360.0 (100 pips)\n\n✅ TP1 3335.0\n✅ TP2 3320.0"
)
UPDATE_MSG = "TP1 hit! +150 pips. Move SL to 3350.0"
COMMAND_MSG = "Close all"  # classify_command → CLOSE_ALL (actionable)

OLD_CHAT = "1001"
NEW_CHAT = "2002"
O = AcquiredMessage.Outcome


class _Base(TestCase):
    def setUp(self):
        self.profile = ParserProfile.objects.create(slug="wayond_v1")
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id=OLD_CHAT, chat_title="Wayond",
            parser_profile=self.profile, status=SignalProvider.Status.ARMED,
            acquisition_window_seconds=600,
        )
        self.now = timezone.now()

    def _msg(self, mid, chat=OLD_CHAT, text=SIGNAL_MSG, age_s=5, **extra):
        m = {"message_id": mid, "chat_id": chat, "text": text,
             "date": self.now - dt.timedelta(seconds=age_s)}
        m.update(extra)
        return m

    def _acq(self, msg):
        return acquire_message(self.provider, msg, now=self.now)

    def _switch_chat(self, new_chat):
        """Simulate the (future) Telegram channel switch: the SAME provider now points
        at a NEW chat. This packet never performs this at runtime — the test drives it."""
        self.provider.telegram_chat_id = new_chat
        self.provider.save(update_fields=["telegram_chat_id"])


# =============================================================================
# A. IDENTITY — the cross-channel collision the packet fixes
# =============================================================================
class IdentityTests(_Base):
    def test_01_cross_channel_same_message_id_two_distinct_rows(self):
        """THE defect: same provider, same message_id, DIFFERENT chat (post-switch) →
        two DISTINCT ledger rows. Pre-fix the new-chat message would dedup against the
        historical old-chat row and be LOST."""
        a_old = self._acq(self._msg("5", chat=OLD_CHAT))
        self._switch_chat(NEW_CHAT)
        a_new = self._acq(self._msg("5", chat=NEW_CHAT))
        self.assertNotEqual(a_old.id, a_new.id)
        self.assertEqual(a_old.chat_id, OLD_CHAT)
        self.assertEqual(a_new.chat_id, NEW_CHAT)
        self.assertEqual(
            AcquiredMessage.objects.filter(provider=self.provider, message_id="5").count(), 2)

    def test_02_cross_channel_new_message_still_intaken_not_suppressed(self):
        """The post-switch same-id message is INTAKEN (a fresh signal), not silently
        dropped as a duplicate."""
        self._acq(self._msg("7", chat=OLD_CHAT))
        self._switch_chat(NEW_CHAT)
        a_new = self._acq(self._msg("7", chat=NEW_CHAT))
        self.assertEqual(a_new.outcome, O.INTAKEN)
        self.assertIsNotNone(a_new.approval)

    def test_03_two_providers_same_message_id_distinct(self):
        p2_profile = ParserProfile.objects.create(slug="wayond_v1_b")
        p2 = SignalProvider.objects.create(
            slug="other", telegram_chat_id=NEW_CHAT, parser_profile=p2_profile,
            status=SignalProvider.Status.ARMED)
        a1 = self._acq(self._msg("9", chat=OLD_CHAT))
        a2 = acquire_message(p2, self._msg("9", chat=NEW_CHAT), now=self.now)
        self.assertNotEqual(a1.id, a2.id)

    def test_04_chat_id_persisted_on_acquired_row(self):
        a = self._acq(self._msg("11", chat=OLD_CHAT))
        self.assertEqual(a.chat_id, OLD_CHAT)

    def test_05_int_chat_id_coerced_to_str(self):
        """Listener supplies chat_id as an int; stored + compared as str (matches the
        provider's string telegram_chat_id — no false mismatch)."""
        a = acquire_message(self.provider, self._msg("12", chat=1001), now=self.now)
        self.assertEqual(a.chat_id, "1001")
        self.assertEqual(a.outcome, O.INTAKEN)


# =============================================================================
# B. FAIL-CLOSED chat-identity guard (§6-G)
# =============================================================================
class FailClosedGuardTests(_Base):
    def test_06_mismatch_quarantined(self):
        a = self._acq(self._msg("20", chat=NEW_CHAT))  # provider chat is OLD_CHAT
        self.assertEqual(a.outcome, O.QUARANTINED)
        self.assertEqual(a.reason, "chat_identity_mismatch")
        self.assertIsNone(a.approval)

    def test_07_mismatch_does_not_advance_watermark(self):
        self._acq(self._msg("21", chat=NEW_CHAT))
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id, "")

    def test_08_missing_chat_on_armed_provider_quarantined(self):
        a = self._acq(self._msg("22", chat=""))
        self.assertEqual(a.outcome, O.QUARANTINED)
        self.assertEqual(a.reason, "chat_identity_missing")

    def test_09_missing_chat_on_non_armed_provider_is_dropped_not_quarantined(self):
        self.provider.status = SignalProvider.Status.PAUSED
        self.provider.save(update_fields=["status"])
        a = self._acq(self._msg("23", chat=""))
        self.assertEqual(a.outcome, O.DROPPED_NOT_ARMED)

    def test_10_provider_without_configured_chat_skips_guard(self):
        """A provider whose trust boundary is not yet configured (blank chat) is not
        chat-guarded — the message is classified normally."""
        self.provider.telegram_chat_id = ""
        self.provider.save(update_fields=["telegram_chat_id"])
        a = self._acq(self._msg("24", chat="9999"))
        self.assertEqual(a.outcome, O.INTAKEN)

    def test_11_mismatch_refusal_is_idempotent(self):
        self._acq(self._msg("25", chat=NEW_CHAT))
        self._acq(self._msg("25", chat=NEW_CHAT))  # re-delivery
        self.assertEqual(
            AcquiredMessage.objects.filter(provider=self.provider, message_id="25").count(), 1)

    def test_12_mismatch_creates_no_approval(self):
        self._acq(self._msg("26", chat=NEW_CHAT))
        self.assertEqual(PendingSignalApproval.objects.count(), 0)


# =============================================================================
# C. REPLAY / WATERMARK
# =============================================================================
class ReplayWatermarkTests(_Base):
    def test_13_same_chat_replay_is_idempotent(self):
        a1 = self._acq(self._msg("30"))
        a2 = self._acq(self._msg("30"))
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(AcquiredMessage.objects.filter(message_id="30").count(), 1)

    def test_14_replay_does_not_create_second_approval(self):
        self._acq(self._msg("31"))
        self._acq(self._msg("31"))
        self.assertEqual(
            PendingSignalApproval.objects.filter(source="wayond", message_id="31").count(), 1)

    def test_15_accepted_message_advances_watermark(self):
        self._acq(self._msg("32"))
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id, "32")

    def test_16_post_switch_lower_message_id_not_suppressed_by_old_watermark(self):
        """Watermark contamination guard: a new chat's id sequence is independent. A
        post-switch message with a LOWER id than the old-chat watermark is still a
        distinct, acquired row (never collapsed into the old chat)."""
        self._acq(self._msg("500", chat=OLD_CHAT))  # old-chat high watermark
        self._switch_chat(NEW_CHAT)
        a = self._acq(self._msg("5", chat=NEW_CHAT))  # new-chat low id
        self.assertEqual(a.chat_id, NEW_CHAT)
        self.assertEqual(a.outcome, O.INTAKEN)


# =============================================================================
# D. APPROVALS (PendingSignalApproval chat-aware)
# =============================================================================
class ApprovalTests(_Base):
    def test_17_approval_carries_chat_id(self):
        a = self._acq(self._msg("40"))
        self.assertEqual(a.approval.chat_id, OLD_CHAT)

    def test_18_cross_channel_two_distinct_approvals(self):
        self._acq(self._msg("41", chat=OLD_CHAT))
        self._switch_chat(NEW_CHAT)
        self._acq(self._msg("41", chat=NEW_CHAT))
        self.assertEqual(
            PendingSignalApproval.objects.filter(source="wayond", message_id="41").count(), 2)

    def test_19_same_chat_approval_dedup(self):
        self._acq(self._msg("42"))
        self._acq(self._msg("42"))
        self.assertEqual(
            PendingSignalApproval.objects.filter(source="wayond", message_id="42").count(), 1)

    def test_20_quarantine_intake_branch_carries_chat_id(self):
        """The intake quarantine branch (reached via the legacy/batch path, not the
        acquire path) stamps chat_id so quarantined rows share the chat-scoped key."""
        from intelligence.telegram_source import parse_message
        parsed = parse_message("gm everyone, good luck today", "43")
        appr = services.intake_parsed(parsed, provider=self.provider, chat_id=OLD_CHAT)
        self.assertEqual(appr.status, PendingSignalApproval.Status.QUARANTINED)
        self.assertEqual(appr.chat_id, OLD_CHAT)

    def test_21_legacy_file_intake_has_blank_chat_id(self):
        """The legacy file-intake path (no provider/chat) → chat_id='' partition,
        byte-for-byte as before."""
        appr = services.intake_message(SIGNAL_MSG, "legacymid")
        self.assertEqual(appr.chat_id, "")
        self.assertEqual(appr.source, services.SOURCE)

    def test_22_intake_parsed_dedup_is_chat_scoped(self):
        from intelligence.telegram_source import parse_message
        parsed = parse_message(SIGNAL_MSG, "44")
        a1 = services.intake_parsed(parsed, provider=self.provider, chat_id=OLD_CHAT)
        a2 = services.intake_parsed(parsed, provider=self.provider, chat_id=NEW_CHAT)
        a3 = services.intake_parsed(parsed, provider=self.provider, chat_id=OLD_CHAT)  # replay
        self.assertNotEqual(a1.id, a2.id)
        self.assertEqual(a1.id, a3.id)


# =============================================================================
# E. UPDATES / reply-linking (chat-scoped)
# =============================================================================
class UpdateTests(_Base):
    def test_23_reply_links_within_same_chat(self):
        orig = self._acq(self._msg("50"))
        self._acq(self._msg("51", text=UPDATE_MSG, reply_to_message_id="50"))
        su = SignalUpdate.objects.get(provider=self.provider, message_id="51")
        self.assertEqual(su.chat_id, OLD_CHAT)
        self.assertEqual(su.raw_payload.get("origin_acquired_id"), orig.id)

    def test_24_reply_does_not_bind_across_chats(self):
        """An origin acquired ONLY in another chat is never picked as the reply origin
        (soft link → None, never cross-chat fabrication)."""
        self._acq(self._msg("60", chat=OLD_CHAT))  # origin in OLD chat
        self._switch_chat(NEW_CHAT)
        self._acq(self._msg("61", chat=NEW_CHAT, text=UPDATE_MSG, reply_to_message_id="60"))
        su = SignalUpdate.objects.get(provider=self.provider, message_id="61")
        self.assertIsNone(su.raw_payload.get("origin_acquired_id"))

    def test_25_update_stores_chat_id(self):
        self._acq(self._msg("62", text=UPDATE_MSG))
        su = SignalUpdate.objects.get(provider=self.provider, message_id="62")
        self.assertEqual(su.chat_id, OLD_CHAT)


# =============================================================================
# F. COMMANDS (ProviderCommand chat-aware)
# =============================================================================
class CommandTests(_Base):
    def test_26_command_recorded_with_chat_id(self):
        self._acq(self._msg("70", text=COMMAND_MSG, reply_to_message_id="1"))
        cmd = ProviderCommand.objects.get(provider=self.provider, message_id="70")
        self.assertEqual(cmd.chat_id, OLD_CHAT)
        self.assertEqual(cmd.command_type, ProviderCommand.Type.CLOSE_ALL)

    def test_27_command_idempotent_same_chat(self):
        self._acq(self._msg("71", text=COMMAND_MSG, reply_to_message_id="1"))
        self._acq(self._msg("71", text=COMMAND_MSG, reply_to_message_id="1"))
        self.assertEqual(
            ProviderCommand.objects.filter(provider=self.provider, message_id="71").count(), 1)

    def test_28_cross_channel_two_distinct_commands(self):
        self._acq(self._msg("72", chat=OLD_CHAT, text=COMMAND_MSG, reply_to_message_id="1"))
        self._switch_chat(NEW_CHAT)
        self._acq(self._msg("72", chat=NEW_CHAT, text=COMMAND_MSG, reply_to_message_id="1"))
        self.assertEqual(
            ProviderCommand.objects.filter(provider=self.provider, message_id="72").count(), 2)


# =============================================================================
# G. AMENDMENTS / edits (chat inheritance)
# =============================================================================
class AmendmentTests(_Base):
    def test_29_edit_non_signal_into_signal_approval_inherits_original_chat(self):
        orig = self._acq(self._msg("80", text="gm everyone, good luck today"))  # QUARANTINED
        self.assertIsNone(orig.approval)
        self._acq(self._msg("80", text=SIGNAL_MSG, edit_date=1))  # edited into a signal
        appr = PendingSignalApproval.objects.get(source="wayond", message_id="80",
                                                 status=PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertEqual(appr.chat_id, OLD_CHAT)
        self.assertTrue(appr.source_edited)

    def test_30_amended_update_inherits_original_chat(self):
        self._acq(self._msg("81", text=UPDATE_MSG))
        self._acq(self._msg("81", text=UPDATE_MSG + " (revised)", edit_date=1))
        for su in SignalUpdate.objects.filter(provider=self.provider, message_id="81"):
            self.assertEqual(su.chat_id, OLD_CHAT)


# =============================================================================
# H. EXECUTION-ISOLATION (downstream read-path unchanged — DARK)
# =============================================================================
class ExecutionIsolationTests(_Base):
    def test_31_chat_id_not_leaked_into_raw_payload(self):
        """execution/signal_planning reads approval.raw_payload['chat_id']; this packet
        must NOT start populating it (that would change plan identity + command-engine
        correlation). chat lives in the new field only."""
        a = self._acq(self._msg("90"))
        self.assertNotIn("chat_id", a.approval.raw_payload)
        self.assertEqual(a.approval.chat_id, OLD_CHAT)  # the field IS set

    def test_32_acquisition_still_imports_no_execution(self):
        import signal_intake.acquisition as acq
        import inspect
        src = inspect.getsource(acq)
        self.assertNotIn("import execution", src)
        self.assertNotIn("from execution", src)

    def test_33_intake_creates_no_execution_job(self):
        from execution.models import ExecutionJob
        before = ExecutionJob.objects.count()
        self._acq(self._msg("91"))
        self.assertTrue(PendingSignalApproval.objects.filter(message_id="91").exists())
        self.assertEqual(ExecutionJob.objects.count(), before)  # no order side-effect


# =============================================================================
# I. DATABASE — constraint integrity
# =============================================================================
class DatabaseConstraintTests(_Base):
    def test_34_acquired_same_provider_chat_message_rejected(self):
        AcquiredMessage.objects.create(
            provider=self.provider, chat_id=OLD_CHAT, message_id="x", outcome=O.INTAKEN)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AcquiredMessage.objects.create(
                provider=self.provider, chat_id=OLD_CHAT, message_id="x", outcome=O.INTAKEN)

    def test_35_acquired_same_provider_message_diff_chat_allowed(self):
        AcquiredMessage.objects.create(
            provider=self.provider, chat_id=OLD_CHAT, message_id="y", outcome=O.INTAKEN)
        AcquiredMessage.objects.create(
            provider=self.provider, chat_id=NEW_CHAT, message_id="y", outcome=O.INTAKEN)
        self.assertEqual(AcquiredMessage.objects.filter(message_id="y").count(), 2)

    def test_36_approval_uniqueness_is_chat_scoped(self):
        PendingSignalApproval.objects.create(source="wayond", chat_id=OLD_CHAT, message_id="z")
        # different chat → allowed
        PendingSignalApproval.objects.create(source="wayond", chat_id=NEW_CHAT, message_id="z")
        # same (source, chat, message) → rejected
        with self.assertRaises(IntegrityError), transaction.atomic():
            PendingSignalApproval.objects.create(source="wayond", chat_id=OLD_CHAT, message_id="z")
        self.assertEqual(PendingSignalApproval.objects.filter(message_id="z").count(), 2)

    def test_37_provider_command_uniqueness_is_chat_scoped(self):
        ProviderCommand.objects.create(provider=self.provider, chat_id=OLD_CHAT, message_id="c")
        ProviderCommand.objects.create(provider=self.provider, chat_id=NEW_CHAT, message_id="c")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderCommand.objects.create(provider=self.provider, chat_id=OLD_CHAT, message_id="c")
        self.assertEqual(ProviderCommand.objects.filter(message_id="c").count(), 2)


# =============================================================================
# J. MIGRATION BACKFILL — deterministic, historical-data-safe (GATE B)
# =============================================================================
class BackfillTests(_Base):
    def _backfill(self):
        import importlib
        from django.apps import apps as django_apps
        mod = importlib.import_module(
            "signal_intake.migrations.0010_source_aware_chat_identity")
        mod.backfill_chat_id(django_apps, None)

    def test_38_approval_chat_backfilled_from_provider(self):
        appr = PendingSignalApproval.objects.create(
            source="wayond", chat_id="", message_id="bf1", provider=self.provider)
        self._backfill()
        appr.refresh_from_db()
        self.assertEqual(appr.chat_id, OLD_CHAT)

    def test_39_approval_without_provider_stays_blank(self):
        """No provider ⇒ no deterministic chat ⇒ chat_id is never fabricated."""
        appr = PendingSignalApproval.objects.create(
            source=services.SOURCE, chat_id="", message_id="bf2")  # provider=None
        self._backfill()
        appr.refresh_from_db()
        self.assertEqual(appr.chat_id, "")

    def test_40_acquired_gap_filled_only_when_blank(self):
        gap = AcquiredMessage.objects.create(
            provider=self.provider, chat_id="", message_id="bf3", outcome=O.INTAKEN)
        kept = AcquiredMessage.objects.create(
            provider=self.provider, chat_id=NEW_CHAT, message_id="bf4", outcome=O.INTAKEN)
        self._backfill()
        gap.refresh_from_db()
        kept.refresh_from_db()
        self.assertEqual(gap.chat_id, OLD_CHAT)    # '' → provider chat
        self.assertEqual(kept.chat_id, NEW_CHAT)   # already set → untouched


# =============================================================================
# K. FAIL-CLOSED guard consistency on the EDIT/amendment path (GATE E finding)
# =============================================================================
class AmendmentGuardTests(_Base):
    def test_41_untrusted_chat_edit_creates_no_approval(self):
        """An untrusted-chat message is quarantined on the fresh path; an EDIT of it into
        a valid signal must NOT run the amendment side-effects (which could surface an
        approval) — the amendment branch is gated on a trusted chat."""
        a1 = self._acq(self._msg("5", chat=NEW_CHAT, text="gm everyone, good luck today"))
        self.assertEqual(a1.outcome, O.QUARANTINED)
        self.assertEqual(a1.reason, "chat_identity_mismatch")
        self._acq(self._msg("5", chat=NEW_CHAT, text=SIGNAL_MSG, edit_date=1))  # edit → signal
        self.assertEqual(PendingSignalApproval.objects.count(), 0)              # no approval
        self.assertEqual(MessageAmendment.objects.count(), 0)                   # no amendment
        self.assertEqual(AcquiredMessage.objects.filter(message_id="5").count(), 1)


# =============================================================================
# L. CHANNEL-SWITCH watermark guard + catch-up floor (GATE E HIGH finding)
# =============================================================================
class WatermarkCutoverGuardTests(_Base):
    def test_42_switch_without_reset_is_refused(self):
        self.provider.watermark_last_message_id = "150000"
        self.provider.save(update_fields=["watermark_last_message_id"])
        with self.assertRaises(CommandError):
            call_command("onboard_provider", "--slug", "wayond", "--chat-id", NEW_CHAT)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.telegram_chat_id, OLD_CHAT)          # unchanged
        self.assertEqual(self.provider.watermark_last_message_id, "150000")  # unchanged

    def test_43_switch_with_reset_clears_watermark(self):
        self.provider.watermark_last_message_id = "150000"
        self.provider.save(update_fields=["watermark_last_message_id"])
        call_command("onboard_provider", "--slug", "wayond",
                     "--chat-id", NEW_CHAT, "--reset-watermark")
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.telegram_chat_id, NEW_CHAT)
        self.assertEqual(self.provider.watermark_last_message_id, "")

    def test_44_same_chat_is_not_a_switch(self):
        """Re-setting the SAME chat id is not a switch — no refusal, watermark untouched."""
        self.provider.watermark_last_message_id = "99"
        self.provider.save(update_fields=["watermark_last_message_id"])
        call_command("onboard_provider", "--slug", "wayond", "--chat-id", OLD_CHAT)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id, "99")

    def test_45_stranded_watermark_floor_drops_new_chat_lowids(self):
        """Documents the hazard the guard prevents: a stale high watermark makes catch_up's
        min_id floor skip low-id new-chat messages entirely, BEFORE acquisition runs."""
        self.provider.telegram_chat_id = NEW_CHAT
        self.provider.watermark_last_message_id = "150000"
        self.provider.save(update_fields=["telegram_chat_id", "watermark_last_message_id"])
        client = FakeClient(messages=[
            FakeMsg(5, int(NEW_CHAT), text=SIGNAL_MSG, date=self.now),
            FakeMsg(6, int(NEW_CHAT), text=SIGNAL_MSG, date=self.now)])
        n = WayondListener().catch_up(client, self.provider)
        self.assertEqual(n, 0)  # floor excludes ids 5,6 → silent loss (the hazard)
        self.assertEqual(AcquiredMessage.objects.filter(chat_id=NEW_CHAT).count(), 0)

    def test_46_reset_watermark_enables_new_chat_catchup(self):
        """After --reset-watermark the floor is 0, so catch_up acquires the new-chat
        low-id messages (the dedup fix then keeps them distinct from the old chat)."""
        self.provider.watermark_last_message_id = "150000"
        self.provider.save(update_fields=["watermark_last_message_id"])
        call_command("onboard_provider", "--slug", "wayond",
                     "--chat-id", NEW_CHAT, "--reset-watermark")
        self.provider.refresh_from_db()
        client = FakeClient(messages=[
            FakeMsg(5, int(NEW_CHAT), text=SIGNAL_MSG, date=self.now),
            FakeMsg(6, int(NEW_CHAT), text=SIGNAL_MSG, date=self.now)])
        n = WayondListener().catch_up(client, self.provider)
        self.assertEqual(n, 2)
        self.assertEqual(AcquiredMessage.objects.filter(chat_id=NEW_CHAT).count(), 2)
