"""
T1_TELEGRAM_CATCHUP_COMPLETENESS — regression + adversarial matrix for the bounded,
resumable, observable catch-up (WayondListener.catch_up). Proves: pagination beyond 200,
explicit INCOMPLETE on ceiling/error (never a false COMPLETE), chronological ordering,
idempotent resume, local-floor immunity to watermark contamination, STALE preserved (no
resurrection), two-provider isolation, and no duplicate downstream side-effects.
"""
import datetime as dt

from django.core.management import call_command
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
UPDATE_MSG = "TP1 hit! +150 pips. Move SL to 3350.0"  # parses as Kind.UPDATE → SignalUpdate


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
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "200")  # durable resume point
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
        # Adversarial HIGH: these are NON-GENUINE drops → the durable checkpoint must NOT
        # advance past them (else a later re-arm would skip them forever). Resume floor intact.
        self.assertEqual(r.checkpoint, 0)
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "")

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
    def test_26_bounded_env_int_rejects_zero_and_clamps_max(self):
        from signal_intake.listener.adapter import _bounded_env_int
        import os as _os
        from unittest import mock
        for bad in ("0", "-5", "x", ""):
            with mock.patch.dict(_os.environ, {"WAYOND_CATCHUP_PAGE_SIZE": bad}):
                self.assertEqual(_bounded_env_int("WAYOND_CATCHUP_PAGE_SIZE", 200, maximum=1000), 200)
        with mock.patch.dict(_os.environ, {"WAYOND_CATCHUP_PAGE_SIZE": "50"}):
            self.assertEqual(_bounded_env_int("WAYOND_CATCHUP_PAGE_SIZE", 200, maximum=1000), 50)
        with mock.patch.dict(_os.environ, {"WAYOND_CATCHUP_PAGE_SIZE": "999999"}):  # excessive → clamp
            self.assertEqual(_bounded_env_int("WAYOND_CATCHUP_PAGE_SIZE", 200, maximum=1000), 1000)

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
        # one more than capacity genuinely trips the ceiling (reset the durable checkpoint
        # the first pass persisted, so the 2nd pass starts from 0 again)
        AcquiredMessage.objects.all().delete()
        self.prov.refresh_from_db()
        self.prov.watermark_last_message_id = ""
        self.prov.catchup_checkpoint_message_id = ""
        self.prov.save(update_fields=["watermark_last_message_id", "catchup_checkpoint_message_id"])
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
        # Durable checkpoint = last FULLY-PERSISTED page boundary (200): page 2 raised before
        # its per-page checkpoint commit, so 201..250 (committed rows) are re-covered on resume
        # (idempotent) — the checkpoint never claims an un-persisted interval.
        self.assertEqual(r.last_processed_id, 200)
        self.assertEqual(r.checkpoint, 200)
        self.assertEqual(r.retrieved, 250)           # page1 200 + 201..250 attempted before the raise
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "200")  # persisted resume point

    def test_30_intra_pass_redelivery_counted_as_duplicate(self):
        msgs = _msgs(1, 3) + [FakeMsg(2, 1001, text=PING, date=timezone.now(), edit_date=1)]
        r = self._cu(FakeClient(messages=msgs))
        self.assertEqual(r.duplicates, 1)            # the re-delivered id=2 → duplicate, not new
        self.assertEqual(r.newly_acquired, 3)        # ids 1,2,3 acquired once each
        self.assertEqual(AcquiredMessage.objects.filter(message_id="2").count(), 1)


# ---- J. R1 §3 — durable checkpoint vs watermark, persisted-state crash/concurrency ----
# These examine the PERSISTED DB state across two passes (not just the in-memory result):
# the durable ``catchup_checkpoint_message_id`` is the resume floor, it is monotonic, and it
# is immune to a live event contaminating the shared ``watermark_last_message_id`` (the
# telethon.sync hazard that motivated T1 R1 HIGH-1).
class _OneLowIdThenEmptyClient(FakeClient):
    """Serves a single (possibly below-floor) id ONCE, ignoring min_id, then empties.
    Simulates a lower-id historical message physically reaching ``_process_page`` so the
    in-code monotonic guard (``rid > checkpoint``) is actually exercised, not pre-filtered."""
    def __init__(self, low_id):
        super().__init__(messages=[FakeMsg(low_id, 1001, text=PING, date=timezone.now())])
        self._served = False

    def iter_messages(self, entity, min_id=0, limit=200, reverse=False):
        if self._served:
            return []
        self._served = True
        return list(self._messages)   # ignore min_id → force the low id through


class PersistedCheckpointTests(_Base):
    def test_31_contaminated_watermark_interrupt_restart_resumes_from_checkpoint(self):
        """Scenario A: a catch-up is interrupted after one page (persisting a clean durable
        checkpoint), THEN a live NewMessage(id=9999) contaminates the shared watermark. The
        restart MUST resume from the durable checkpoint (200), never the contaminated
        watermark (9999) — otherwise every historical message below 9999 is silently
        skipped. The resume floor is the checkpoint; the watermark is never read for it."""
        msgs = _msgs(1, 500)
        # pass 1: page 1 (1..200) drains + persists checkpoint; page-2 fetch fails → INCOMPLETE.
        r1 = self._cu(_FailOnCallClient(msgs, fail_on_call=2), page_size=200)
        self.assertFalse(r1.complete)
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "200")  # clean boundary
        # a concurrent live event (telethon.sync dispatch) contaminates the shared watermark
        # with a far-future id — the exact hazard the durable checkpoint exists to survive.
        SignalProvider.objects.filter(id=self.prov.id).update(watermark_last_message_id="9999")
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.watermark_last_message_id, "9999")     # contaminated
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "200")  # checkpoint intact
        # pass 2 (clean restart): resumes from checkpoint 200 — NOT 9999 — so 201..500 drain.
        # Had the resume read the 9999 watermark, it would fetch 0 and lose 201..500 forever.
        r2 = self._cu(FakeClient(messages=msgs), page_size=200)
        self.assertTrue(r2.complete)
        self.assertEqual(AcquiredMessage.objects.count(), 500)   # no historical skip
        self.assertEqual(r2.duplicates, 0)                       # resumed PAST 200, not before
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "500")

    def test_32_checkpoint_is_monotonic_never_regresses(self):
        """Scenario B: a lower-id historical message reaching ``_process_page`` must NOT
        regress the durable checkpoint (a watermark/checkpoint regression would re-open an
        already-drained interval and risk re-delivery / lost-position ambiguity)."""
        self.prov.catchup_checkpoint_message_id = "300"
        self.prov.save(update_fields=["catchup_checkpoint_message_id"])
        r = self._cu(_OneLowIdThenEmptyClient(150), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.checkpoint, 300)                      # 150 < 300 → no regression
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "300")

    def test_33_crash_midpage_resume_is_idempotent_no_gap_no_dup_rows(self):
        """Scenario C: a crash BETWEEN the per-message commits and the page-boundary
        checkpoint write. The durable checkpoint stays at the previous clean boundary, so the
        restart re-covers the committed-but-uncheckpointed rows idempotently (dedup) and
        drains the rest — no gap, no duplicate durable rows."""
        msgs = _msgs(1, 400)

        class _BoomAt251(WayondListener):
            def acquire_raw(self, raw):
                if self._raw_id(raw) == 251:
                    raise RuntimeError("crash mid page 2 at 251")
                return super().acquire_raw(raw)

        r1 = _BoomAt251().catch_up(FakeClient(messages=msgs), self.prov, page_size=200)
        self.assertEqual(r1.status, CatchupStatus.INCOMPLETE_ERROR)
        self.prov.refresh_from_db()
        # page 2 raised before its checkpoint commit → durable checkpoint = page-1 boundary.
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "200")
        self.assertEqual(AcquiredMessage.objects.count(), 250)  # 1..200 + committed 201..250
        # clean restart: re-covers 201..250 (idempotent) + drains 251..400.
        r2 = self._cu(FakeClient(messages=msgs), page_size=200)
        self.assertTrue(r2.complete)
        self.assertEqual(AcquiredMessage.objects.count(), 400)  # no gap
        self.assertEqual(r2.duplicates, 50)                     # 201..250 re-covered, deduped
        self.assertEqual(r2.newly_acquired, 150)                # 251..400 fresh
        self.assertEqual(AcquiredMessage.objects.filter(message_id="250").count(), 1)  # not doubled
        self.assertEqual(AcquiredMessage.objects.filter(message_id="251").count(), 1)


# ---- K. R1 §4 — bounded flood-wait & resource exhaustion (HIGH-2) ----------
class _AlwaysFloodClient(FakeClient):
    """Every fetch raises FloodWaitError (during construction)."""
    def __init__(self, messages, seconds=0):
        super().__init__(messages=messages)
        self._seconds = seconds
        self.flood_calls = 0

    def iter_messages(self, entity, min_id=0, limit=200, reverse=False):
        self.flood_calls += 1
        raise FloodWaitError(self._seconds)


class _AlwaysFloodDuringConsumeClient(FakeClient):
    """Every fetch raises FloodWaitError DURING consumption (lazy iterator), never while
    constructing — the harder case the construction-only guard would miss."""
    def iter_messages(self, entity, min_id=0, limit=200, reverse=False):
        def gen():
            raise FloodWaitError(0)
            yield  # pragma: no cover — unreachable; makes this a generator
        return gen()


class BoundedResourceTests(_Base):
    def test_34_repeated_floodwait_is_bounded_and_incomplete(self):
        c = _AlwaysFloodClient(_msgs(1, 50), seconds=0)
        r = self._cu(c, page_size=200)
        self.assertEqual(r.status, CatchupStatus.INCOMPLETE_ERROR)
        self.assertIn("FloodWaitError", r.error)
        # bounded: 1 initial attempt + max_waits(3) retries = 4 calls, then re-raise (no spin).
        self.assertEqual(c.flood_calls, 4)
        self.assertEqual(r.retrieved, 0)             # nothing drained; NOT a false COMPLETE

    def test_35_oversized_floodwait_is_rejected_not_shortened(self):
        # T1 R2 MEDIUM: a server-requested wait LONGER than the permitted single wait must be
        # REJECTED (re-raised → INCOMPLETE), never shortened-and-retried early (rate-limit).
        import signal_intake.listener.adapter as A
        from unittest import mock
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise FloodWaitError(100000)           # server demands far more than permitted

        slept = []
        with mock.patch.object(A.time, "sleep", side_effect=lambda s: slept.append(s)):
            with self.assertRaises(FloodWaitError):
                WayondListener()._floodwait(fn, max_waits=3, max_single_s=5)
        self.assertEqual(slept, [])                # never shortened-and-slept → no premature retry
        self.assertEqual(calls["n"], 1)            # one attempt, then rejected

    def test_36_catch_up_param_clamps_nonpositive_page_size(self):
        for bad in (0, -10):
            AcquiredMessage.objects.all().delete()
            self.prov.catchup_checkpoint_message_id = ""
            self.prov.save(update_fields=["catchup_checkpoint_message_id"])
            r = self._cu(FakeClient(messages=_msgs(1, 5)), page_size=bad)
            self.assertTrue(r.complete)            # clamps to default, never a 0-page noop
            self.assertEqual(r.retrieved, 5)

    def test_37_catch_up_param_clamps_nonpositive_max_pages(self):
        # zero/negative max_pages must fall back to the default — NOT trip an instant ceiling.
        r = self._cu(FakeClient(messages=_msgs(1, 5)), page_size=200, max_pages=0)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 5)
        r2 = self._cu(FakeClient(messages=_msgs(6, 10)), page_size=200, max_pages=-3)
        self.assertTrue(r2.complete)

    def test_38_excessive_env_values_clamped_to_maximum(self):
        from signal_intake.listener.adapter import _bounded_env_int
        import os as _os
        from unittest import mock
        with mock.patch.dict(_os.environ, {"WAYOND_CATCHUP_MAX_PAGES": "100000000"}):
            self.assertEqual(_bounded_env_int("WAYOND_CATCHUP_MAX_PAGES", 500, maximum=10000), 10000)
        with mock.patch.dict(_os.environ, {"WAYOND_CATCHUP_FLOODWAIT_MAX_SINGLE_SECONDS": "999999"}):
            self.assertEqual(
                _bounded_env_int("WAYOND_CATCHUP_FLOODWAIT_MAX_SINGLE_SECONDS", 300, maximum=3600), 3600)
        with mock.patch.dict(_os.environ, {"WAYOND_CATCHUP_FLOODWAIT_MAX_WAITS": "100000"}):
            self.assertEqual(_bounded_env_int("WAYOND_CATCHUP_FLOODWAIT_MAX_WAITS", 3, maximum=100), 100)

    def test_39_floodwait_during_consumption_beyond_budget_is_incomplete(self):
        r = self._cu(_AlwaysFloodDuringConsumeClient(messages=_msgs(1, 50)), page_size=200)
        self.assertEqual(r.status, CatchupStatus.INCOMPLETE_ERROR)
        self.assertIn("FloodWaitError", r.error)
        self.assertEqual(r.retrieved, 0)

    def test_40_time_budget_exceeded_is_incomplete_error(self):
        import signal_intake.listener.adapter as A
        from unittest import mock
        vals = iter([1000.0, 2000.0, 3000.0, 4000.0])   # deadline=1300; first loop check=2000>1300
        with mock.patch.object(A.time, "monotonic", side_effect=lambda: next(vals)):
            r = self._cu(FakeClient(messages=_msgs(1, 50)), page_size=200)
        self.assertEqual(r.status, CatchupStatus.INCOMPLETE_ERROR)
        self.assertEqual(r.error, "time_budget_exceeded")
        self.assertEqual(r.retrieved, 0)               # tripped before any page processed

    def test_41_restart_after_bounded_floodwait_failure_resumes_and_completes(self):
        msgs = _msgs(1, 300)
        r1 = self._cu(_AlwaysFloodClient(msgs, seconds=0), page_size=200)   # all fetches flood
        self.assertFalse(r1.complete)
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "")       # nothing drained
        r2 = self._cu(FakeClient(messages=msgs), page_size=200)             # clean restart
        self.assertTrue(r2.complete)
        self.assertEqual(AcquiredMessage.objects.count(), 300)


# ---- L. R1 §5 — completeness reporting semantics (MEDIUM) ------------------
# COMPLETE means the RETRIEVAL was exhausted within bounds — never that every retrieved
# message became a durable row, nor a tradeable approval. The counters keep those distinct,
# and the durable checkpoint only ever advances past DURABLY-ACCOUNTED messages.
class CompletenessReportingTests(_Base):
    def test_42_reporting_distinguishes_retrieved_ignored_acquired(self):
        # ids 4,5 are from a foreign chat with no provider → retrieved but NOT durably acquired.
        msgs = _msgs(1, 3, chat=1001) + _msgs(4, 5, chat=9999)
        r = self._cu(FakeClient(messages=msgs), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 5)        # everything fetched is counted retrieved…
        self.assertEqual(r.ignored, 2)          # …but 4,5 had no provider → ignored
        self.assertEqual(r.newly_acquired, 3)   # only 1,2,3 are durable rows
        self.assertEqual(AcquiredMessage.objects.count(), 3)

    def test_43_checkpoint_not_advanced_past_unaccounted_higher_id(self):
        """The durable checkpoint must stop at the highest DURABLY-ACCOUNTED id, never jump
        past a retrieved-but-unaccounted higher id. (Foreign-chat ids here stand in for the
        production case — a transient acquire failure on an authorized message — which raises
        → INCOMPLETE with the same non-advance; either way the checkpoint never over-claims.)"""
        msgs = _msgs(1, 3, chat=1001) + _msgs(4, 5, chat=9999)
        r = self._cu(FakeClient(messages=msgs), page_size=200)
        self.assertEqual(r.checkpoint, 3)                         # not 5
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "3")

    def test_44_complete_does_not_imply_all_tradeable(self):
        now = timezone.now()
        old = now - dt.timedelta(seconds=1200)
        msgs = [
            FakeMsg(1, 1001, text=SIGNAL, date=now - dt.timedelta(seconds=5)),  # fresh → tradeable
            FakeMsg(2, 1001, text=PING, date=now - dt.timedelta(seconds=5)),    # non-signal → quarantined
            FakeMsg(3, 1001, text=SIGNAL, date=old),                            # aged signal → STALE
        ]
        r = self._cu(FakeClient(messages=msgs))
        self.assertTrue(r.complete)
        self.assertEqual(r.retrieved, 3)             # retrieved…
        self.assertEqual(r.newly_acquired, 3)        # …all durable rows…
        self.assertEqual(r.stale, 1)                 # …one aged out…
        self.assertEqual(r.tradeable_approvals, 1)   # …only one is tradeable
        self.assertEqual(PendingSignalApproval.objects.filter(
            status=PendingSignalApproval.Status.PENDING_APPROVAL).count(), 1)


# ---- M. R1 adversarial-review fixes (45-48) --------------------------------
class AdversarialR1FixTests(_Base):
    def test_45_paused_catchup_does_not_strand_checkpoint_rearm_acquires(self):
        """Findings 1/4/5 (triple-confirmed): a non-armed provider's catch-up must NOT advance
        the durable checkpoint past DROPPED_NOT_ARMED (non_genuine) rows. Otherwise re-arming
        (onboard_provider --arm does NOT reset the checkpoint) would skip those ids forever and
        silently lose an in-window signal — the subsystem's worst outcome."""
        self.prov.status = SignalProvider.Status.PAUSED
        self.prov.save(update_fields=["status"])
        fresh = FakeMsg(100, 1001, text=SIGNAL, date=timezone.now() - dt.timedelta(seconds=5))
        r1 = self._cu(FakeClient(messages=[fresh]))
        self.assertTrue(r1.complete)
        self.assertEqual(r1.checkpoint, 0)                 # NOT advanced past the non-genuine drop
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "")   # resume floor intact
        self.assertEqual(AcquiredMessage.objects.filter(
            message_id="100", non_genuine=True,
            outcome=AcquiredMessage.Outcome.DROPPED_NOT_ARMED).count(), 1)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)      # not tradeable while paused
        # operator re-arms through the real path (changes status only, never the checkpoint)
        call_command("onboard_provider", "--slug", "wayond", "--arm")
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "")   # still intact after arm
        # the still-in-window signal (id=100) is now GENUINELY acquired — not skipped
        r2 = self._cu(FakeClient(messages=[fresh]))
        self.assertTrue(r2.complete)
        self.assertEqual(r2.tradeable_approvals, 1)        # the signal was NOT lost
        self.assertEqual(PendingSignalApproval.objects.filter(
            status=PendingSignalApproval.Status.PENDING_APPROVAL).count(), 1)
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "100")  # now advanced (genuine)

    def test_46_live_acquired_during_pass_counted_duplicate_not_new(self):
        """Finding 2: a message the concurrent live handler acquires DURING this catch-up pass
        (acquired_at >= pass start, absent from the local 'seen' set) must be counted a
        DUPLICATE when catch-up re-fetches it — not newly_acquired/tradeable. The per-message
        acquire cutoff fixes the old pass-start heuristic that miscounted it."""
        now = timezone.now()
        live_sig = FakeMsg(5, 1001, text=SIGNAL, date=now - dt.timedelta(seconds=5))

        class _LiveRace(WayondListener):
            def __init__(self):
                super().__init__(); self._fired = False

            def acquire_raw(self, raw):
                if not self._fired and self._raw_id(raw) == 1:
                    self._fired = True
                    WayondListener().acquire_raw(live_sig)   # live NewMessage wins id=5 mid-pass
                return super().acquire_raw(raw)

        msgs = _msgs(1, 4) + [live_sig]   # catch-up backlog includes id 5
        r = _LiveRace().catch_up(FakeClient(messages=msgs), self.prov)
        self.assertTrue(r.complete)
        self.assertEqual(r.duplicates, 1)                 # id 5 already acquired by the live handler
        self.assertEqual(r.newly_acquired, 4)             # ids 1..4 only (NOT 5)
        self.assertEqual(r.tradeable_approvals, 0)        # catch-up did not NEWLY acquire the signal
        self.assertEqual(AcquiredMessage.objects.filter(message_id="5").count(), 1)  # no duplicate row
        self.assertEqual(PendingSignalApproval.objects.filter(
            status=PendingSignalApproval.Status.PENDING_APPROVAL).count(), 1)         # the live one

    def test_47_floodwait_respects_wallclock_deadline(self):
        """Finding 3: a flood-wait sleep that would run PAST the wall-clock budget must abort
        (re-raise → INCOMPLETE_ERROR) rather than overshoot the budget and starve live dispatch."""
        import signal_intake.listener.adapter as A
        from unittest import mock
        clock = {"t": 1000.0}
        c = _AlwaysFloodClient(_msgs(1, 50), seconds=300)   # each flood demands 300s
        with mock.patch.object(A.time, "monotonic", side_effect=lambda: clock["t"]), \
             mock.patch.object(A.time, "sleep",
                               side_effect=lambda s: clock.__setitem__("t", clock["t"] + s)):
            r = self._cu(c, page_size=200, time_budget_s=30)   # 30s budget << one 300s flood sleep
        self.assertEqual(r.status, CatchupStatus.INCOMPLETE_ERROR)
        self.assertIn("FloodWaitError", r.error)
        # the first 300s sleep would overshoot the 30s budget → abort BEFORE sleeping (not 3×300s)
        self.assertEqual(c.flood_calls, 1)

    def test_48_shared_chat_id_resolves_deterministically_and_warns(self):
        """Finding 6 (LOW): two active providers sharing a chat id resolve deterministically
        (stable, lowest slug), the chat is caught up ONCE (not per-provider), and the collision
        is warned. The second provider's slug sorts BEFORE 'wayond' but its id is HIGHER, so the
        assertion discriminates slug-ordering (not an id/creation-order accident)."""
        shared = SignalProvider.objects.create(
            slug="aaa_shared", telegram_chat_id="1001", parser_profile=self.profile,
            status=SignalProvider.Status.ARMED, acquisition_window_seconds=600)
        self.assertGreater(shared.id, self.prov.id)            # higher id, lower slug
        lis = WayondListener()
        r1 = lis.provider_for_chat("1001")
        r2 = lis.provider_for_chat("1001")
        self.assertEqual(r1.id, r2.id)                         # stable across calls
        self.assertEqual(r1.slug, "aaa_shared")                # lowest slug wins (not lowest id)
        with self.assertLogs("signal_intake.listener.adapter", level="WARNING") as cm:
            results = lis.catch_up_all(FakeClient(messages=_msgs(1, 3, chat=1001)))
        self.assertEqual(len(results), 1)                      # chat caught up ONCE, not per-provider
        self.assertTrue(any("share" in m for m in cm.output))  # collision warned


# ---- N. R2 adversarial-review fixes — checkpoint gap across mixed outcomes (49-53) -------
# HIGH (T1 R2 §2-4): the durable checkpoint must represent the highest SAFELY-ACCOUNTED
# boundary — it must never advance PAST an earlier unaccounted retrieval (ignored OR
# non-genuine) just because a LATER genuine message succeeded. Else the earlier message is
# never re-fetched and is silently lost once the provider is (re-)armed.
class AdversarialR2CheckpointGapTests(_Base):
    def test_49_non_genuine_then_genuine_does_not_skip(self):
        """Scenario A: in ONE pass id 100 is non-genuine (provider PAUSED) and id 101 is
        genuine (provider ARMED mid-pass). The checkpoint must hold below 100; on restart 100
        is re-fetched and genuinely acquired — never skipped, no duplicate approval."""
        self.prov.catchup_checkpoint_message_id = "99"
        self.prov.save(update_fields=["catchup_checkpoint_message_id"])
        now = timezone.now()
        m100 = FakeMsg(100, 1001, text=SIGNAL, date=now - dt.timedelta(seconds=5))
        m101 = FakeMsg(101, 1001, text=SIGNAL, date=now - dt.timedelta(seconds=5))
        pid = self.prov.id

        class _ArmMidPass(WayondListener):
            def acquire_raw(self, raw):
                rid = self._raw_id(raw)
                if rid == 100:   # PAUSED when 100 is processed → non-genuine DROP
                    SignalProvider.objects.filter(id=pid).update(status=SignalProvider.Status.PAUSED)
                elif rid == 101:  # ARMED again when 101 is processed → genuine
                    SignalProvider.objects.filter(id=pid).update(status=SignalProvider.Status.ARMED)
                return super().acquire_raw(raw)

        r1 = _ArmMidPass().catch_up(FakeClient(messages=[m100, m101]), self.prov)
        self.assertTrue(r1.complete)
        self.assertFalse(r1.durably_complete)          # a boundary is held back
        self.assertEqual(r1.checkpoint, 99)            # NOT advanced past the non-genuine 100
        self.assertEqual(r1.unaccounted_floor, 100)
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "99")
        self.assertTrue(AcquiredMessage.objects.filter(message_id="100", non_genuine=True).exists())
        self.assertEqual(PendingSignalApproval.objects.filter(
            status=PendingSignalApproval.Status.PENDING_APPROVAL).count(), 1)   # 101 only
        # Restart (provider ARMED): 100 re-fetched + genuinely acquired — not skipped, no dup.
        r2 = self._cu(FakeClient(messages=[m100, m101]))
        self.assertTrue(r2.durably_complete)
        self.assertEqual(r2.checkpoint, 101)
        self.assertTrue(AcquiredMessage.objects.filter(message_id="100", non_genuine=False).exists())
        self.assertEqual(PendingSignalApproval.objects.filter(
            status=PendingSignalApproval.Status.PENDING_APPROVAL).count(), 2)   # 100 + 101

    def test_50_ignored_then_genuine_does_not_skip(self):
        """Scenario B: id 100 resolves to no provider (ignored); id 101 is genuine. The
        checkpoint must not advance past the ignored 100 just because 101 succeeded."""
        self.prov.catchup_checkpoint_message_id = "99"
        self.prov.save(update_fields=["catchup_checkpoint_message_id"])
        now = timezone.now()
        msgs = [FakeMsg(100, 9999, text=PING, date=now),                       # foreign → ignored
                FakeMsg(101, 1001, text=SIGNAL, date=now - dt.timedelta(seconds=5))]
        r = self._cu(FakeClient(messages=msgs))
        self.assertTrue(r.complete)
        self.assertFalse(r.durably_complete)
        self.assertEqual(r.ignored, 1)
        self.assertEqual(r.unaccounted_floor, 100)
        self.assertEqual(r.checkpoint, 99)             # NOT advanced past the ignored 100
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "99")

    def test_51_unaccounted_at_page_end_blocks_next_page_advance(self):
        """Scenario C: an unaccounted id at the END of page 1 stops the durable checkpoint;
        genuine messages on page 2 cannot advance it past that boundary."""
        now = timezone.now()
        msgs = [FakeMsg(1, 1001, text=PING, date=now),
                FakeMsg(2, 1001, text=PING, date=now),
                FakeMsg(3, 9999, text=PING, date=now),   # ignored — end of page 1
                FakeMsg(4, 1001, text=PING, date=now),   # page 2
                FakeMsg(5, 1001, text=PING, date=now)]
        r = self._cu(FakeClient(messages=msgs), page_size=3)
        self.assertTrue(r.complete)
        self.assertEqual(r.pages, 2)
        self.assertEqual(r.unaccounted_floor, 3)
        self.assertEqual(r.checkpoint, 2)              # held at 2 (<3); page-2 (4,5) can't pass 3
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "2")

    def test_52_interrupt_in_mixed_page_loses_no_evidence_no_dup(self):
        """Scenario D: a mixed page is interrupted after a genuine message committed. Restart
        re-accounts everything with no lost durable evidence and no duplicate downstream."""
        now = timezone.now()
        msgs = [FakeMsg(1, 1001, text=SIGNAL, date=now - dt.timedelta(seconds=5)),  # genuine signal
                FakeMsg(2, 9999, text=PING, date=now),                              # ignored
                FakeMsg(3, 1001, text=SIGNAL, date=now - dt.timedelta(seconds=5))]  # genuine signal

        class _BoomAt3(WayondListener):
            def acquire_raw(self, raw):
                if self._raw_id(raw) == 3:
                    raise RuntimeError("crash at 3")
                return super().acquire_raw(raw)

        r1 = _BoomAt3().catch_up(FakeClient(messages=msgs), self.prov, page_size=10)
        self.assertEqual(r1.status, CatchupStatus.INCOMPLETE_ERROR)
        # msg 1 committed (genuine, approval). Page raised before its checkpoint commit → the
        # persisted checkpoint stays at the start (fail-closed re-scan — never a skip).
        self.assertTrue(AcquiredMessage.objects.filter(message_id="1", non_genuine=False).exists())
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "")
        approvals_after_crash = PendingSignalApproval.objects.filter(
            status=PendingSignalApproval.Status.PENDING_APPROVAL).count()
        self.assertEqual(approvals_after_crash, 1)   # msg 1 only
        # Restart clean: 1 re-fetched (dedup, no new approval), 3 newly acquired.
        r2 = self._cu(FakeClient(messages=msgs))
        self.assertTrue(r2.complete)
        self.assertEqual(PendingSignalApproval.objects.filter(
            status=PendingSignalApproval.Status.PENDING_APPROVAL).count(), 2)   # 1 + 3, no dup
        self.assertEqual(AcquiredMessage.objects.filter(message_id="1", non_genuine=False).count(), 1)

    def test_53_armed_then_paused_midpass_holds_checkpoint(self):
        """Scenario E: provider transitions ARMED→PAUSED mid-pass; messages after the
        transition are non-genuine (fail-closed). The checkpoint holds at the last genuine id
        before the transition, never past the first non-genuine one."""
        self.prov.catchup_checkpoint_message_id = "0"
        self.prov.save(update_fields=["catchup_checkpoint_message_id"])
        now = timezone.now()
        msgs = [FakeMsg(i, 1001, text=PING, date=now) for i in (10, 11, 12)]
        pid = self.prov.id

        class _PauseMidPass(WayondListener):
            def acquire_raw(self, raw):
                if self._raw_id(raw) == 11:   # PAUSE before 11 → 11 and 12 become non-genuine
                    SignalProvider.objects.filter(id=pid).update(status=SignalProvider.Status.PAUSED)
                return super().acquire_raw(raw)

        r = _PauseMidPass().catch_up(FakeClient(messages=msgs), self.prov)
        self.assertTrue(r.complete)
        self.assertFalse(r.durably_complete)
        self.assertEqual(r.checkpoint, 10)             # last genuine before the transition
        self.assertEqual(r.unaccounted_floor, 11)
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.catchup_checkpoint_message_id, "10")


# ---- O. R2 MEDIUM — FloodWait rate-limit compliance (54-56) ----------------
class FloodWaitR2Tests(_Base):
    def test_54_permitted_floodwait_is_honoured_in_full(self):
        import signal_intake.listener.adapter as A
        from unittest import mock
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise FloodWaitError(5)
            return "ok"

        slept = []
        with mock.patch.object(A.time, "sleep", side_effect=lambda s: slept.append(s)):
            out = WayondListener()._floodwait(fn, max_waits=3, max_single_s=10)
        self.assertEqual(out, "ok")
        self.assertEqual(slept, [5])               # FULL requested wait honoured, not shortened
        self.assertEqual(calls["n"], 2)            # exactly one retry after the honoured wait

    def test_55_floodwait_retry_count_and_total_time_bounded(self):
        import signal_intake.listener.adapter as A
        from unittest import mock
        slept = []

        def fn():
            raise FloodWaitError(2)                # permitted (<= max_single_s) → honoured each time

        with mock.patch.object(A.time, "sleep", side_effect=lambda s: slept.append(s)):
            with self.assertRaises(FloodWaitError):
                WayondListener()._floodwait(fn, max_waits=3, max_single_s=10)
        self.assertEqual(slept, [2, 2, 2])         # exactly max_waits honoured waits, then re-raise
        self.assertLessEqual(sum(slept), 3 * 10)   # total bounded by max_waits * max_single_s

    def test_56_floodwait_pass_does_not_make_expired_signal_tradeable(self):
        old = timezone.now() - dt.timedelta(seconds=1200)

        class _FloodOnceThenAged(FakeClient):
            def __init__(self, msgs):
                super().__init__(messages=msgs); self._f = False

            def iter_messages(self, entity, min_id=0, limit=200, reverse=False):
                rows = super().iter_messages(entity, min_id=min_id, limit=limit, reverse=reverse)
                outer = self

                def gen():
                    if not outer._f:
                        outer._f = True
                        raise FloodWaitError(0)    # permitted; honoured (0s), then retry
                    for r in rows:
                        yield r
                return gen()

        msgs = [FakeMsg(i, 1001, text=SIGNAL, date=old) for i in range(1, 4)]
        r = self._cu(_FloodOnceThenAged(msgs), page_size=200)
        self.assertTrue(r.complete)
        self.assertEqual(r.stale, 3)               # aged → STALE despite the flood-wait…
        self.assertEqual(r.tradeable_approvals, 0)  # …never resurrected into a tradeable approval
        self.assertEqual(PendingSignalApproval.objects.count(), 0)


# ---- P. R2 §6 — read-only checkpoint-initialization reconciliation gate (57-61) ----------
class ReconcileCheckpointCommandTests(_Base):
    def _run(self, *args):
        from io import StringIO
        out = StringIO()
        call_command("reconcile_catchup_checkpoint", *args, stdout=out)
        return out.getvalue()

    def _seed_ledger(self, lo, hi, outcome=None):
        outcome = outcome or AcquiredMessage.Outcome.INTAKEN
        for i in range(lo, hi + 1):
            AcquiredMessage.objects.create(provider=self.prov, chat_id="1001",
                                           message_id=str(i), outcome=outcome)

    def test_57_consistent_watermark_passes_and_is_read_only(self):
        self.prov.watermark_last_message_id = "5"
        self.prov.save(update_fields=["watermark_last_message_id"])
        self._seed_ledger(1, 5)
        before_wm, before_ckpt = self.prov.watermark_last_message_id, self.prov.catchup_checkpoint_message_id
        out = self._run("--slug", "wayond")
        self.assertIn("[PASS]", out)
        self.prov.refresh_from_db()
        self.assertEqual(self.prov.watermark_last_message_id, before_wm)          # unchanged
        self.assertEqual(self.prov.catchup_checkpoint_message_id, before_ckpt)    # no write

    def test_58_watermark_ahead_of_ledger_is_review(self):
        self.prov.watermark_last_message_id = "900"        # ahead of any acquired row
        self.prov.save(update_fields=["watermark_last_message_id"])
        self._seed_ledger(100, 100)
        out = self._run("--slug", "wayond")
        self.assertIn("[REVIEW]", out)
        self.assertIn("AHEAD", out)

    def test_59_empty_watermark_is_review(self):
        self.prov.watermark_last_message_id = ""
        self.prov.save(update_fields=["watermark_last_message_id"])
        out = self._run("--slug", "wayond")
        self.assertIn("[REVIEW]", out)

    def test_60_gate_exits_nonzero_on_review(self):
        from django.core.management.base import CommandError
        self.prov.watermark_last_message_id = ""
        self.prov.save(update_fields=["watermark_last_message_id"])
        with self.assertRaises(CommandError):
            self._run("--slug", "wayond", "--gate")

    def test_61_json_output_is_machine_readable(self):
        import json
        self.prov.watermark_last_message_id = "5"
        self.prov.save(update_fields=["watermark_last_message_id"])
        self._seed_ledger(1, 5)
        data = json.loads(self._run("--slug", "wayond", "--json"))
        self.assertIn("providers", data)
        self.assertIn("caveat", data)
        self.assertEqual(data["providers"][0]["verdict"], "PASS")
        self.assertFalse(data["any_review"])


# ---- Q. R2 adversarial-review FIX regressions (62-64) ----------------------
class AdversarialR2FixRegressionTests(_Base):
    def test_62_signal_update_crash_replay_is_idempotent(self):
        """R2 adversarial MEDIUM: the fresh SignalUpdate is written in autocommit BEFORE the
        AcquiredMessage commits, so a crash between them re-runs _classify on restart. Simulate
        by deleting the AcquiredMessage (as if it never committed) and re-processing — the
        SignalUpdate must NOT double-create (it now dedups per (provider, chat, message, edited))."""
        from signal_intake.models import SignalUpdate
        lis = WayondListener()
        raw = FakeMsg(500, 1001, text=UPDATE_MSG, date=timezone.now())
        lis.acquire_raw(raw)   # fresh UPDATE → AcquiredMessage(500, UPDATE) + SignalUpdate(500)
        self.assertEqual(SignalUpdate.objects.filter(message_id="500").count(), 1)
        self.assertEqual(
            AcquiredMessage.objects.filter(message_id="500", non_genuine=False).count(), 1)
        AcquiredMessage.objects.filter(message_id="500").delete()   # simulate the crash
        lis.acquire_raw(raw)   # restart re-processes 500 → _classify → _record_update again
        self.assertEqual(SignalUpdate.objects.filter(message_id="500").count(), 1)   # NOT 2
        self.assertEqual(
            AcquiredMessage.objects.filter(message_id="500", non_genuine=False).count(), 1)

    def test_63_reconcile_gate_fails_closed_on_no_match(self):
        """R2 adversarial LOW: a --gate run that matches NO provider reconciled nothing and must
        NOT read green (fail-closed), rather than exit 0."""
        from io import StringIO
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("reconcile_catchup_checkpoint", "--slug", "does-not-exist", "--gate",
                         stdout=StringIO())

    def test_64_reconcile_excludes_non_genuine_quarantine_from_genuine(self):
        """R2 adversarial LOW: a chat-refusal row is QUARANTINED + non_genuine=True; it must NOT
        count as a genuine acquisition, so a watermark above the true genuine max is correctly
        flagged REVIEW (not a false PASS masked by the non-genuine row)."""
        from io import StringIO
        self.prov.watermark_last_message_id = "900"
        self.prov.save(update_fields=["watermark_last_message_id"])
        AcquiredMessage.objects.create(provider=self.prov, chat_id="1001", message_id="100",
                                       outcome=AcquiredMessage.Outcome.INTAKEN, non_genuine=False)
        AcquiredMessage.objects.create(provider=self.prov, chat_id="1001", message_id="900",
                                       outcome=AcquiredMessage.Outcome.QUARANTINED, non_genuine=True)
        out = StringIO()
        call_command("reconcile_catchup_checkpoint", "--slug", "wayond", stdout=out)
        text = out.getvalue()
        self.assertIn("[REVIEW]", text)        # wm 900 AHEAD of the true genuine max (100)
        self.assertIn("AHEAD", text)
