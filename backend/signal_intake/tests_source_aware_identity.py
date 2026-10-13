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

    def test_10_armed_provider_without_configured_chat_fails_closed(self):
        """HIGH-1: an ARMED provider with NO established chat trust boundary must REFUSE,
        not accept. Blank-chat + armed → QUARANTINED, never intaken."""
        self.provider.telegram_chat_id = ""
        self.provider.save(update_fields=["telegram_chat_id"])
        a = self._acq(self._msg("24", chat="9999"))
        self.assertEqual(a.outcome, O.QUARANTINED)
        self.assertEqual(a.reason, "provider_chat_unconfigured")
        self.assertIsNone(a.approval)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id, "")  # no advance

    def test_10b_non_armed_blank_chat_is_dropped_not_quarantined(self):
        """Preserve legacy/onboarding behaviour: a NON-armed provider with no chat is a
        normal arm-gate DROP (DROPPED_NOT_ARMED), not a chat-identity refusal."""
        self.provider.status = SignalProvider.Status.ONBOARDING
        self.provider.telegram_chat_id = ""
        self.provider.save(update_fields=["status", "telegram_chat_id"])
        a = self._acq(self._msg("24b", chat="9999"))
        self.assertEqual(a.outcome, O.DROPPED_NOT_ARMED)

    def test_10c_config_drift_chat_cleared_after_arming_fails_closed(self):
        """Configuration drift: a provider armed WITH a chat whose chat is later cleared
        (admin edit / drift) must fail closed on the next message."""
        a_ok = self._acq(self._msg("24c0", chat=OLD_CHAT))   # armed + chat → normal
        self.assertEqual(a_ok.outcome, O.INTAKEN)
        self.provider.telegram_chat_id = ""                  # drift: chat cleared, still ARMED
        self.provider.save(update_fields=["telegram_chat_id"])
        a = self._acq(self._msg("24c1", chat=OLD_CHAT))
        self.assertEqual(a.outcome, O.QUARANTINED)
        self.assertEqual(a.reason, "provider_chat_unconfigured")

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

    def test_38_approval_chat_backfilled_from_persisted_evidence(self):
        """AMBER-1: the approval chat is taken from PERSISTED acquisition evidence (the
        linked AcquiredMessage.chat_id), NOT from mutable provider metadata."""
        appr = PendingSignalApproval.objects.create(
            source="wayond", chat_id="", message_id="bf1", provider=self.provider)
        AcquiredMessage.objects.create(
            provider=self.provider, chat_id=OLD_CHAT, message_id="bf1",
            outcome=O.INTAKEN, approval=appr)  # the immutable evidence
        self._backfill()
        appr.refresh_from_db()
        self.assertEqual(appr.chat_id, OLD_CHAT)

    def test_38b_approval_with_provider_but_no_evidence_stays_blank(self):
        """Mutable provider metadata is NOT proof of a historical chat: an approval with a
        provider but NO linked acquisition evidence is left '' (never inferred)."""
        appr = PendingSignalApproval.objects.create(
            source="wayond", chat_id="", message_id="bf1b", provider=self.provider)
        self._backfill()
        appr.refresh_from_db()
        self.assertEqual(appr.chat_id, "")

    def test_39_approval_without_provider_stays_blank(self):
        """No provider / legacy file-intake ⇒ no evidence ⇒ chat_id is never fabricated."""
        appr = PendingSignalApproval.objects.create(
            source=services.SOURCE, chat_id="", message_id="bf2")  # provider=None
        self._backfill()
        appr.refresh_from_db()
        self.assertEqual(appr.chat_id, "")

    def test_39b_ambiguous_evidence_stays_blank(self):
        """Ambiguous persisted evidence (two distinct chats) ⇒ unresolved ⇒ left ''."""
        appr = PendingSignalApproval.objects.create(
            source="wayond", chat_id="", message_id="bf2b", provider=self.provider)
        AcquiredMessage.objects.create(provider=self.provider, chat_id=OLD_CHAT,
                                       message_id="bf2b", outcome=O.INTAKEN, approval=appr)
        AcquiredMessage.objects.create(provider=self.provider, chat_id=NEW_CHAT,
                                       message_id="bf2b", outcome=O.INTAKEN, approval=appr)
        self._backfill()
        appr.refresh_from_db()
        self.assertEqual(appr.chat_id, "")

    def test_40_backfill_never_mutates_the_append_only_ledgers(self):
        """The backfill is approval-only; it NEVER updates AcquiredMessage/SignalUpdate/
        ProviderCommand (their chat_id is their own write-time evidence)."""
        gap = AcquiredMessage.objects.create(
            provider=self.provider, chat_id="", message_id="bf3", outcome=O.INTAKEN)
        kept = AcquiredMessage.objects.create(
            provider=self.provider, chat_id=NEW_CHAT, message_id="bf4", outcome=O.INTAKEN)
        self._backfill()
        gap.refresh_from_db()
        kept.refresh_from_db()
        self.assertEqual(gap.chat_id, "")         # untouched (no inference)
        self.assertEqual(kept.chat_id, NEW_CHAT)   # untouched


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
        self.assertEqual(n.retrieved, 0)  # floor excludes ids 5,6 → silent loss (the hazard)
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
        self.assertEqual(n.retrieved, 2)
        self.assertEqual(AcquiredMessage.objects.filter(chat_id=NEW_CHAT).count(), 2)


# =============================================================================
# M. QUARANTINE-ID POISONING ACROSS AN AUTHORIZED CUTOVER (GATE E HIGH #2)
# =============================================================================
class QuarantinePoisonTests(_Base):
    def test_47_quarantined_untrusted_id_never_suppresses_genuine_after_cutover(self):
        """The exact PD scenario. A NEW_CHAT id=5 rejected while OLD_CHAT is configured
        must NOT suppress a genuine NEW_CHAT id=5 after an authorized switch to NEW_CHAT.
        The refusal (immutable) and the genuine row coexist; the refusal is never promoted."""
        # A genuine OLD_CHAT message advances the (trusted) watermark.
        self._acq(self._msg("3", chat=OLD_CHAT))
        # 1-3. NEW_CHAT id=5 arrives while OLD_CHAT is configured → fail-closed QUARANTINE.
        poison = self._acq(self._msg("5", chat=NEW_CHAT))
        self.assertEqual(poison.outcome, O.QUARANTINED)
        self.assertEqual(poison.reason, "chat_identity_mismatch")
        self.assertTrue(poison.non_genuine)
        self.assertIsNone(poison.approval)
        # 4. Authorized cutover to NEW_CHAT (watermark is set → guard requires the reset).
        call_command("onboard_provider", "--slug", "wayond",
                     "--chat-id", NEW_CHAT, "--reset-watermark")
        self.provider.refresh_from_db()
        # 5-6. A genuine, fresh NEW_CHAT id=5 → MUST be processed, not suppressed.
        genuine = self._acq(self._msg("5", chat=NEW_CHAT))
        self.assertNotEqual(genuine.id, poison.id)
        self.assertFalse(genuine.non_genuine)
        self.assertEqual(genuine.outcome, O.INTAKEN)
        self.assertIsNotNone(genuine.approval)
        # Both coexist: immutable refusal evidence preserved, genuine row created.
        rows = AcquiredMessage.objects.filter(
            provider=self.provider, chat_id=NEW_CHAT, message_id="5")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.filter(non_genuine=True).count(), 1)
        self.assertEqual(rows.filter(non_genuine=False).count(), 1)

    def test_48_refusal_row_is_idempotent(self):
        """Re-delivery of the same untrusted message dedups to the one refusal row."""
        self._acq(self._msg("9", chat=NEW_CHAT))
        self._acq(self._msg("9", chat=NEW_CHAT))
        self.assertEqual(
            AcquiredMessage.objects.filter(message_id="9", non_genuine=True).count(), 1)

    def test_49b_non_armed_provider_edit_never_creates_approval(self):
        """R1/R2: the arm gate holds on the EDIT path. An armed provider acquires a
        non-signal (genuine QUARANTINED); the provider is PAUSED; an edit turns it into a
        signal → must NOT create an approval or an amendment. The paused delivery lands in
        the NON-GENUINE partition (a DROPPED record), never touching the genuine row."""
        orig = self._acq(self._msg("80", text="gm everyone, good luck today"))  # armed → QUARANTINED
        self.assertIsNone(orig.approval)
        self.assertFalse(orig.non_genuine)  # genuine (armed + trusted) content-quarantine
        self.provider.status = SignalProvider.Status.PAUSED
        self.provider.save(update_fields=["status"])
        self._acq(self._msg("80", text=SIGNAL_MSG, edit_date=1))  # edit while PAUSED
        self.assertEqual(PendingSignalApproval.objects.count(), 0)   # no approval surfaced
        from signal_intake.models import MessageAmendment
        self.assertEqual(MessageAmendment.objects.count(), 0)        # no amendment side-effects
        rows = AcquiredMessage.objects.filter(message_id="80")
        self.assertEqual(rows.filter(non_genuine=False).count(), 1)  # the genuine row, untouched
        self.assertEqual(rows.filter(non_genuine=True, outcome=O.DROPPED_NOT_ARMED).count(), 1)

    def test_49_genuine_then_refusal_also_coexist(self):
        """Symmetric: a genuine row then a later untrusted same-id claim coexist; the
        untrusted claim never overwrites or dedups against the genuine acquisition."""
        genuine = self._acq(self._msg("12", chat=OLD_CHAT))
        self.assertEqual(genuine.outcome, O.INTAKEN)
        self.assertFalse(genuine.non_genuine)
        # A forged OLD-chat id=12 from a different (untrusted) chat claim: switch provider
        # to a THIRD chat so the OLD_CHAT message is now a mismatch.
        self.provider.telegram_chat_id = "3003"
        self.provider.save(update_fields=["telegram_chat_id"])
        refusal = self._acq(self._msg("12", chat=OLD_CHAT))  # now mismatched → refused
        self.assertTrue(refusal.non_genuine)
        self.assertNotEqual(refusal.id, genuine.id)
        self.assertEqual(
            AcquiredMessage.objects.filter(message_id="12", chat_id=OLD_CHAT).count(), 2)


# =============================================================================
# O. NON-ARMED ACQUISITION POISONING ACROSS A RE-ARM (R2 HIGH-A)
# =============================================================================
class NonArmedPoisonTests(_Base):
    def test_53_paused_drop_does_not_poison_rearmed_genuine(self):
        """PD R2 HIGH-A sequence: PAUSED → DROP → REARM → FRESH GENUINE → REPLAY. A drop
        recorded while disabled must never occupy/suppress the genuine identity."""
        from execution.models import ExecutionJob
        self.provider.status = SignalProvider.Status.PAUSED
        self.provider.save(update_fields=["status"])
        # PAUSED → id=100 → DROPPED_NOT_ARMED (non-genuine), no watermark advance.
        drop = self._acq(self._msg("100", chat=OLD_CHAT))
        self.assertEqual(drop.outcome, O.DROPPED_NOT_ARMED)
        self.assertTrue(drop.non_genuine)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id, "")  # no premature advance
        # REARM.
        self.provider.status = SignalProvider.Status.ARMED
        self.provider.save(update_fields=["status"])
        # FRESH GENUINE id=100 → must be a NEW genuine acquisition + exactly one approval.
        before_jobs = ExecutionJob.objects.count()
        genuine = self._acq(self._msg("100", chat=OLD_CHAT))
        self.assertNotEqual(genuine.id, drop.id)
        self.assertFalse(genuine.non_genuine)
        self.assertEqual(genuine.outcome, O.INTAKEN)
        self.assertIsNotNone(genuine.approval)
        self.assertEqual(
            PendingSignalApproval.objects.filter(source="wayond", message_id="100").count(), 1)
        # Drop preserved; exactly one genuine + one non-genuine row.
        rows = AcquiredMessage.objects.filter(
            provider=self.provider, chat_id=OLD_CHAT, message_id="100")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.filter(non_genuine=True).count(), 1)
        self.assertEqual(rows.filter(non_genuine=False).count(), 1)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.watermark_last_message_id, "100")  # genuine advanced it
        # REPLAY of the genuine → idempotent; no new approval, no execution job.
        replay = self._acq(self._msg("100", chat=OLD_CHAT))
        self.assertEqual(replay.id, genuine.id)
        self.assertEqual(
            PendingSignalApproval.objects.filter(source="wayond", message_id="100").count(), 1)
        self.assertEqual(ExecutionJob.objects.count(), before_jobs)  # no order intention

    def test_54_paused_drop_replay_is_idempotent(self):
        self.provider.status = SignalProvider.Status.PAUSED
        self.provider.save(update_fields=["status"])
        self._acq(self._msg("101", chat=OLD_CHAT))
        self._acq(self._msg("101", chat=OLD_CHAT))  # replay while paused
        self.assertEqual(
            AcquiredMessage.objects.filter(message_id="101", non_genuine=True).count(), 1)


# =============================================================================
# N. ADMIN chat-id edit is guarded (R1 renewed-review finding)
# =============================================================================
class AdminWatermarkGuardTests(_Base):
    def _admin(self):
        from django.contrib.admin.sites import AdminSite
        from signal_intake.admin import SignalProviderAdmin
        return SignalProviderAdmin(SignalProvider, AdminSite())

    def test_50_chat_id_locked_in_admin_once_watermark_exists(self):
        self.provider.watermark_last_message_id = "8000"
        self.provider.save(update_fields=["watermark_last_message_id"])
        ro = self._admin().get_readonly_fields(None, obj=self.provider)
        self.assertIn("telegram_chat_id", ro)  # cannot strand the watermark via admin

    def test_51_chat_id_editable_in_admin_when_no_watermark(self):
        self.provider.watermark_last_message_id = ""
        self.provider.save(update_fields=["watermark_last_message_id"])
        ro = self._admin().get_readonly_fields(None, obj=self.provider)
        self.assertNotIn("telegram_chat_id", ro)  # no stranding risk → editable

    def test_52_chat_id_editable_in_admin_on_add(self):
        ro = self._admin().get_readonly_fields(None, obj=None)  # add form
        self.assertNotIn("telegram_chat_id", ro)


# =============================================================================
# P. EDIT/AMENDMENT re-applies the acquisition staleness window (R2 cert finding)
# =============================================================================
class AmendmentStalenessTests(_Base):
    def test_55_stale_signal_edit_is_not_resurrected_as_approval(self):
        """A signal the fresh path dismissed as STALE must NOT be resurrected into a fresh
        PENDING approval via an edit — the amendment re-applies the acquisition window."""
        from signal_intake.models import MessageAmendment
        stale = self._acq(self._msg("70", age_s=1200))      # 20 min > 600s window → STALE
        self.assertEqual(stale.outcome, O.STALE)
        self.assertIsNone(stale.approval)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)
        self._acq(self._msg("70", age_s=1200, edit_date=1))  # edit of the stale message
        self.assertEqual(PendingSignalApproval.objects.count(), 0)                   # not resurrected
        self.assertTrue(MessageAmendment.objects.filter(message_id="70").exists())   # evidence kept

    def test_56_fresh_non_signal_edited_into_signal_still_approves(self):
        """Parity: a FRESH edit that turns a non-signal into a signal still surfaces an
        approval — the window gate only blocks STALE ones."""
        self._acq(self._msg("71", text="gm everyone, good luck today", age_s=5))  # fresh non-signal
        self._acq(self._msg("71", text=SIGNAL_MSG, age_s=5, edit_date=1))          # fresh edit → signal
        self.assertEqual(
            PendingSignalApproval.objects.filter(
                source="wayond", message_id="71",
                status=PendingSignalApproval.Status.PENDING_APPROVAL).count(), 1)
