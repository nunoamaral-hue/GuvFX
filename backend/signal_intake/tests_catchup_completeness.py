"""
T1_TELEGRAM_CATCHUP_COMPLETENESS — regression + adversarial matrix for the bounded,
resumable, observable catch-up (WayondListener.catch_up). Proves: pagination beyond 200,
explicit INCOMPLETE on ceiling/error (never a false COMPLETE), chronological ordering,
idempotent resume, local-floor immunity to watermark contamination, STALE preserved (no
resurrection), two-provider isolation, and no duplicate downstream side-effects.
"""
import datetime as dt

from django.test import TestCase
from django.utils import timezone

from signal_intake.listener.adapter import CatchupStatus, WayondListener
from signal_intake.models import (
    AcquiredMessage, ParserProfile, PendingSignalApproval, SignalProvider,
)
from signal_intake.tests_listener import FakeClient, FakeMsg, FloodWaitError

SIGNAL = ("XAUUSD | Potential downward movement\n\nXAUUSD | SELL 3350.0\n\n"
          "❌ Stop Loss 3360.0 (100 pips)\n\n✅ TP1 3335.0\n✅ TP2 3320.0")
PING = "gm all"  # non-signal → QUARANTINED (fast, genuine, no approval)


def _msgs(lo, hi, chat=1001, text=PING, now=None, age_s=5):
    now = now or timezone.now()
    return [FakeMsg(i, chat, text=text, date=now - dt.timedelta(seconds=age_s))
            for i in range(lo, hi + 1)]


class _FailOnCallClient(FakeClient):
    def __init__(self, messages, fail_on_call=2, exc=None):
        super().__init__(messages=messages)
        self._calls, self._fail = 0, fail_on_call
        self._exc = exc or RuntimeError("page fetch boom")

    def iter_messages(self, *a, **k):
        self._calls += 1
        if self._calls == self._fail:
            raise self._exc
        return super().iter_messages(*a, **k)


class _FloodDuringConsumeClient(FakeClient):
    """Raises FloodWaitError mid-consumption on the first fetch, then succeeds."""
    def __init__(self, messages):
        super().__init__(messages=messages)
        self._flooded = False

    def iter_messages(self, entity, min_id=0, limit=200, reverse=False):
        rows = super().iter_messages(entity, min_id=min_id, limit=limit, reverse=reverse)
        outer = self

        def gen():
            if not outer._flooded:
                outer._flooded = True
                raise FloodWaitError(0)
            for r in rows:
                yield r
        return gen()


class _Base(TestCase):
    def setUp(self):
        self.profile = ParserProfile.objects.create(slug="wayond_v1")
        self.prov = SignalProvider.objects.create(
            slug="wayond", telegram_chat_id="1001", parser_profile=self.profile,
            status=SignalProvider.Status.ARMED, acquisition_window_seconds=600)

    def _cu(self, client, provider=None, **kw):
        return WayondListener().catch_up(client, provider or self.prov, **kw)


# ---- A. pagination & completeness (1-7, 20) --------------------------------
class PaginationTests(_Base):
    def test_01_empty_backlog(self):
        r = self._cu(FakeClient(messages=[]))
        self.assertTrue(r.complete); self.assertEqual(r.retrieved, 0)

    def test_02_one_message(self):
        r = self._cu(FakeClient(messages=_msgs(1, 1)))
        self.assertTrue(r.complete); self.assertEqual(r.retrieved, 1)

    def test_03_exactly_200(self):
        r = self._cu(FakeClient(messages=_msgs(1, 200)), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 200)
        self.assertEqual(AcquiredMessage.objects.count(), 200)

    def test_04_201_crosses_page_boundary(self):
        r = self._cu(FakeClient(messages=_msgs(1, 201)), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 201)   # the fix: 201 > old 200 cap
        self.assertTrue(AcquiredMessage.objects.filter(message_id="201").exists())

    def test_05_250_messages(self):
        r = self._cu(FakeClient(messages=_msgs(1, 250)), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 250)
        self.assertEqual(r.pages, 2)
        self.assertEqual(AcquiredMessage.objects.count(), 250)

    def test_06_1000_messages(self):
        r = self._cu(FakeClient(messages=_msgs(1, 1000)), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 1000)
        self.assertEqual(r.pages, 5)

    def test_07_partial_final_page(self):
        r = self._cu(FakeClient(messages=_msgs(1, 150)), page_size=200)
        self.assertTrue(r.complete); self.assertEqual(r.retrieved, 150)

    def test_20_repeat_catchup_no_new(self):
        c = FakeClient(messages=_msgs(1, 50))
        self._cu(c)
        self.prov.refresh_from_db()   # as catch_up_all does — fresh provider (watermark now 50)
        r2 = self._cu(c)              # nothing newer than the watermark
        self.assertTrue(r2.complete)
        self.assertEqual(r2.retrieved, 0)
        self.assertEqual(AcquiredMessage.objects.count(), 50)  # no duplicate rows


# ---- B. failure & resume (8, 9, 10, 22) ------------------------------------
class FailureResumeTests(_Base):
    def test_08_failure_after_page_one_is_incomplete(self):
        r = self._cu(_FailOnCallClient(_msgs(1, 500), fail_on_call=2), page_size=200)
        self.assertEqual(r.status, CatchupStatus.INCOMPLETE_ERROR)
        self.assertFalse(r.complete)
        self.assertEqual(r.retrieved, 200)   # page 1 processed, page 2 failed
        self.assertTrue(r.error)

    def test_09_failure_during_processing_is_incomplete(self):
        class _Boom(WayondListener):
            def acquire_raw(self, raw):
                if self._raw_id(raw) == 120:
                    raise RuntimeError("acquire boom")
                return super().acquire_raw(raw)
        r = _Boom().catch_up(FakeClient(messages=_msgs(1, 300)), self.prov, page_size=200)
        self.assertEqual(r.status, CatchupStatus.INCOMPLETE_ERROR)
        self.assertFalse(r.complete)

    def test_10_interrupt_then_restart_resumes_and_completes(self):
        msgs = _msgs(1, 500)
        r1 = self._cu(_FailOnCallClient(msgs, fail_on_call=2), page_size=200)
        self.assertFalse(r1.complete)
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.watermark_last_message_id, "200")  # contiguous checkpoint
        r2 = self._cu(FakeClient(messages=msgs), page_size=200)       # clean restart
        self.assertTrue(r2.complete)
        self.assertEqual(AcquiredMessage.objects.count(), 500)        # all, no gap
        self.assertEqual(r2.duplicates, 0)                            # resumed past 200

    def test_22_ceiling_yields_incomplete_never_false_complete(self):
        r = self._cu(FakeClient(messages=_msgs(1, 1000)), page_size=100, max_pages=3)
        self.assertEqual(r.status, CatchupStatus.INCOMPLETE_CEILING)
        self.assertFalse(r.complete)
        self.assertEqual(r.pages, 3)
        self.assertEqual(r.retrieved, 300)   # bounded; the rest explicitly NOT claimed done


# ---- C. concurrency / watermark-contamination (11, 12, 13) -----------------
class ConcurrencyTests(_Base):
    def test_11_duplicate_live_event_during_catchup(self):
        # message 250 already acquired (as if by the live handler) before catch-up reaches it
        self.prov.telegram_chat_id = "1001"
        AcquiredMessage.objects.create(provider=self.prov, chat_id="1001", message_id="250",
                                       outcome=AcquiredMessage.Outcome.QUARANTINED)
        r = self._cu(FakeClient(messages=_msgs(1, 300)), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.duplicates, 1)                               # 250 deduped
        self.assertEqual(AcquiredMessage.objects.filter(message_id="250").count(), 1)

    def test_12_local_floor_immune_to_concurrent_watermark_bump(self):
        """A concurrent live event bumps the shared watermark mid-pass; the LOCAL floor must
        keep processing the historical backlog (no skip)."""
        class _Bump(WayondListener):
            def __init__(self, prov):
                super().__init__(); self._prov = prov; self._done = False
            def acquire_raw(self, raw):
                if not self._done:   # simulate a live NewMessage(id=9999) poisoning the watermark
                    self._done = True
                    SignalProvider.objects.filter(id=self._prov.id).update(
                        watermark_last_message_id="9999")
                return super().acquire_raw(raw)
        r = _Bump(self.prov).catch_up(FakeClient(messages=_msgs(101, 200)), self.prov, page_size=50)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 100)   # all 101..200 processed despite watermark=9999
        self.assertEqual(AcquiredMessage.objects.filter(provider=self.prov).count(), 100)

    def test_13_full_backlog_drained_before_handing_off(self):
        # historical low ids are fully drained in the pass (not left for 'after a live event')
        r = self._cu(FakeClient(messages=_msgs(101, 460)), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 360)
        self.assertTrue(AcquiredMessage.objects.filter(message_id="101").exists())
        self.assertTrue(AcquiredMessage.objects.filter(message_id="460").exists())


# ---- D. two-provider isolation (14) ----------------------------------------
class TwoProviderTests(_Base):
    def setUp(self):
        super().setUp()
        self.ti = SignalProvider.objects.create(
            slug="ti_signals", telegram_chat_id="-100777", parser_profile=self.profile,
            status=SignalProvider.Status.ARMED, acquisition_window_seconds=600)

    def test_14_overlapping_ids_distinct_per_chat_and_isolated(self):
        lis = WayondListener()
        r_w = lis.catch_up(FakeClient(messages=_msgs(1, 5, chat=1001)), self.prov)
        r_t = lis.catch_up(FakeClient(messages=_msgs(1, 5, chat=-100777)), self.ti)
        self.assertTrue(r_w.complete and r_t.complete)
        # same message_id "3" exists under BOTH chats as distinct rows
        self.assertEqual(AcquiredMessage.objects.filter(message_id="3").count(), 2)
        self.assertEqual(AcquiredMessage.objects.filter(chat_id="1001").count(), 5)
        self.assertEqual(AcquiredMessage.objects.filter(chat_id="-100777").count(), 5)
        self.prov.refresh_from_db(); self.ti.refresh_from_db()
        self.assertEqual(self.prov.watermark_last_message_id, "5")  # independent watermarks
        self.assertEqual(self.ti.watermark_last_message_id, "5")


# ---- E. staleness / edits (15, 16, 17) -------------------------------------
class StalenessTests(_Base):
    def test_15_stale_messages_complete_but_not_tradeable(self):
        old = timezone.now() - dt.timedelta(seconds=1200)
        msgs = [FakeMsg(i, 1001, text=SIGNAL, date=old) for i in range(1, 6)]
        r = self._cu(FakeClient(messages=msgs))
        self.assertTrue(r.complete)          # catch-up COMPLETE…
        self.assertEqual(r.stale, 5)         # …but all STALE
        self.assertEqual(r.tradeable_approvals, 0)   # no resurrection into approvals
        self.assertEqual(PendingSignalApproval.objects.count(), 0)

    def test_16_fresh_signals_become_tradeable_approvals(self):
        msgs = [FakeMsg(i, 1001, text=SIGNAL, date=timezone.now() - dt.timedelta(seconds=5))
                for i in range(1, 4)]
        r = self._cu(FakeClient(messages=msgs))
        self.assertTrue(r.complete)
        self.assertEqual(r.tradeable_approvals, 3)
        self.assertEqual(r.stale, 0)

    def test_17_edited_message_in_backlog_is_handled(self):
        # an edited duplicate id in the page just dedups/amends, never double-acquires
        msgs = _msgs(1, 3) + [FakeMsg(2, 1001, text=PING, date=timezone.now(), edit_date=1)]
        r = self._cu(FakeClient(messages=msgs))
        self.assertTrue(r.complete)
        self.assertEqual(AcquiredMessage.objects.filter(message_id="2").count(), 1)  # not doubled


# ---- F. provider state / auth (18, 19) -------------------------------------
class ProviderStateTests(_Base):
    def test_18_paused_provider_drops_but_completes(self):
        self.prov.status = SignalProvider.Status.PAUSED
        self.prov.save(update_fields=["status"])
        r = self._cu(FakeClient(messages=_msgs(1, 250)), page_size=200)
        self.assertTrue(r.complete)                       # pass still drains the backlog
        self.assertEqual(r.retrieved, 250)
        self.assertEqual(
            AcquiredMessage.objects.filter(outcome=AcquiredMessage.Outcome.DROPPED_NOT_ARMED).count(), 250)

    def test_19_unknown_chat_messages_are_ignored_by_source_allowlist(self):
        # Source authentication = the provider_for_chat allowlist: a message from a chat with
        # NO configured provider is IGNORED (no ledger row), never acquired. (The
        # provider+mismatched-chat refusal path is covered in tests_source_aware_identity.)
        r = self._cu(FakeClient(messages=_msgs(1, 3, chat=9999)))  # chat 9999 has no provider
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 3)                 # fetched…
        self.assertEqual(r.newly_acquired, 0)            # …but ignored (no provider)
        self.assertEqual(AcquiredMessage.objects.count(), 0)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)


# ---- G. flood-wait during iteration (21) -----------------------------------
class FloodWaitTests(_Base):
    def test_21_floodwait_during_consumption_is_retried(self):
        r = self._cu(_FloodDuringConsumeClient(_msgs(1, 50)), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 50)   # recovered after the mid-consume flood-wait


# ---- H. no duplicate downstream side-effects (23, 24, 25) ------------------
class NoDuplicateSideEffectTests(_Base):
    def test_23_no_duplicate_approvals_on_resume(self):
        msgs = [FakeMsg(i, 1001, text=SIGNAL, date=timezone.now() - dt.timedelta(seconds=5))
                for i in range(1, 6)]
        self._cu(_FailOnCallClient(msgs, fail_on_call=99), page_size=2)  # succeeds fully (no fail)
        before = PendingSignalApproval.objects.count()
        self._cu(FakeClient(messages=msgs), page_size=2)                 # re-run
        self.assertEqual(PendingSignalApproval.objects.count(), before)  # idempotent

    def test_24_no_execution_plan_created(self):
        from execution.models import SignalExecutionPlan
        self._cu(FakeClient(messages=_msgs(1, 20)))
        self.assertEqual(SignalExecutionPlan.objects.count(), 0)

    def test_25_no_execution_job_created(self):
        from execution.models import ExecutionJob
        before = ExecutionJob.objects.count()
        self._cu(FakeClient(messages=_msgs(1, 20)))
        self.assertEqual(ExecutionJob.objects.count(), before)


# ---- I. R1 adversarial-review fixes (26-30) --------------------------------
class ReviewFixTests(_Base):
    def test_26_positive_env_int_rejects_zero_and_invalid(self):
        from signal_intake.listener.adapter import _positive_env_int
        import os as _os
        from unittest import mock
        for bad in ("0", "-5", "x", ""):
            with mock.patch.dict(_os.environ, {"WAYOND_CATCHUP_PAGE_SIZE": bad}):
                self.assertEqual(_positive_env_int("WAYOND_CATCHUP_PAGE_SIZE", 200), 200)
        with mock.patch.dict(_os.environ, {"WAYOND_CATCHUP_PAGE_SIZE": "50"}):
            self.assertEqual(_positive_env_int("WAYOND_CATCHUP_PAGE_SIZE", 200), 50)

    def test_27_zero_page_size_does_not_produce_false_complete_noop(self):
        # a 0/invalid page_size must clamp to the default and FULLY drain, never report a
        # COMPLETE no-op over a non-empty backlog (the MEDIUM fail-open finding)
        r = self._cu(FakeClient(messages=_msgs(1, 5)), page_size=0)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 5)                 # NOT 0
        self.assertEqual(AcquiredMessage.objects.count(), 5)

    def test_28_exact_multiple_backlog_is_complete_not_ceiling(self):
        # backlog == page_size * max_pages → the empty confirming fetch proves exhaustion
        r = self._cu(FakeClient(messages=_msgs(1, 300)), page_size=100, max_pages=3)
        self.assertTrue(r.complete)
        self.assertEqual(r.status, CatchupStatus.COMPLETE)
        self.assertEqual(r.retrieved, 300)
        # one more than capacity genuinely trips the ceiling
        AcquiredMessage.objects.all().delete()
        self.prov.refresh_from_db()
        self.prov.watermark_last_message_id = ""
        self.prov.save(update_fields=["watermark_last_message_id"])
        r2 = self._cu(FakeClient(messages=_msgs(1, 301)), page_size=100, max_pages=3)
        self.assertEqual(r2.status, CatchupStatus.INCOMPLETE_CEILING)

    def test_29_last_processed_id_accurate_after_mid_page_error(self):
        class _Boom(WayondListener):
            def acquire_raw(self, raw):
                if self._raw_id(raw) == 251:
                    raise RuntimeError("acquire boom at 251")
                return super().acquire_raw(raw)
        r = _Boom().catch_up(FakeClient(messages=_msgs(1, 400)), self.prov, page_size=200)
        self.assertEqual(r.status, CatchupStatus.INCOMPLETE_ERROR)
        self.assertEqual(r.last_processed_id, 250)   # 201..250 durably processed before 251 raised
        self.assertEqual(r.retrieved, 250)           # page1 200 + 201..250

    def test_30_intra_pass_redelivery_counted_as_duplicate(self):
        msgs = _msgs(1, 3) + [FakeMsg(2, 1001, text=PING, date=timezone.now(), edit_date=1)]
        r = self._cu(FakeClient(messages=msgs))
        self.assertEqual(r.duplicates, 1)            # the re-delivered id=2 → duplicate, not new
        self.assertEqual(r.newly_acquired, 3)        # ids 1,2,3 acquired once each
        self.assertEqual(AcquiredMessage.objects.filter(message_id="2").count(), 1)
