"""GFX-PKT-TP-PROTECTION-OPTIMISATION WS-A/E/F — durable protection-latency instrumentation.

Proves: explicit + tested broker→UTC conversion; per-leg segment computation from authoritative durable
data; MISSING data is UNKNOWN (None) NEVER a fabricated zero; the worker stamps close_ingested_at on the
None→closed transition; the broker soft-deferral floor is quantified; the ops block is source-aware.
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import protection_latency as pl
from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan, SignalSourceConfig
from signal_intake.models import PendingSignalApproval
from trading.models import Trade, TradingAccount

User = get_user_model()
UTC = datetime.timezone.utc


class BrokerConversionTests(TestCase):
    def test_broker_to_utc_explicit_offset(self):
        dt = datetime.datetime(2026, 7, 16, 10, 15, 58, tzinfo=UTC)  # broker-labelled
        with mock.patch.object(pl, "BROKER_UTC_OFFSET_HOURS", 3):
            self.assertEqual(pl.broker_to_utc(dt), dt - datetime.timedelta(hours=3))
        self.assertIsNone(pl.broker_to_utc(None))

    def test_secs_unknown_never_zero_for_missing(self):
        a = datetime.datetime(2026, 7, 16, 7, 0, 0, tzinfo=UTC)
        self.assertIsNone(pl._secs(None, a))       # UNKNOWN, not 0
        self.assertIsNone(pl._secs(a, None))
        self.assertEqual(pl._secs(a, a + datetime.timedelta(seconds=5)), 5.0)


class LatencyBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pl", email="pl@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="PL1", is_demo=True)
        SignalSourceConfig.objects.create(source="ti_signals", incremental_protection_enabled=True)

    def _plan(self, pid_hint="p1", direction="SELL"):
        appr = PendingSignalApproval.objects.create(
            source="ti_signals", message_id=pid_hint, symbol="XAUUSD", direction=direction,
            stop_loss="4032.15", take_profits=["4026.72", "4025.30", "4023.41"],
            status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=self.acct, source="ti_signals", message_id=pid_hint, symbol="XAUUSD",
            direction=direction, stop_loss="4032.15", is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.CLOSED)
        for i, tp in enumerate(("4026.72", "4025.30", "4023.41"), start=1):
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit=tp, stop_loss="4032.15", lot_size=Decimal("0.40"),
                status=ProposedOrderLeg.Status.PROMOTED)
        return plan

    def _trade(self, plan, leg, *, close_broker=None, close_ingested=None, entry="4028.92"):
        t = Trade.objects.create(
            account=self.acct, symbol="XAUUSD", side=plan.direction, volume=Decimal("0.40"),
            ticket=f"pos{plan.id}{leg}", open_time=timezone.now(), open_price=Decimal(entry),
            close_time=close_broker, close_price=(Decimal("4025.30") if close_broker else None),
            comment=f"WAY{plan.id}L{leg}")
        if close_ingested is not None:
            Trade.objects.filter(id=t.id).update(close_ingested_at=close_ingested)
        return t

    def _modify(self, plan, leg, stage, *, created, started, finished, status="SUCCESS",
                verified_sl="4025.3", prior_sl="4028.92", error=""):
        j = ExecutionJob.objects.create(
            job_type="MODIFY_POSITION", account=self.acct, status=status,
            payload={"plan_id": plan.id, "leg_index": leg, "protection_stage": stage,
                     "signal_source": "ti_signals", "ticket": f"pos{plan.id}{leg}"},
            result={"verified_sl": verified_sl, "prior_sl": prior_sl, "requested_sl": verified_sl,
                    **({"error": error} if error else {})},
            started_at=started, finished_at=finished, error_message=error)
        ExecutionJob.objects.filter(id=j.id).update(created_at=created)
        return j


class LegLatencyTests(LatencyBase):
    def test_full_tp2_locked_segments(self):
        plan = self._plan()
        # broker TP2 close 07:15:58 (UTC+3 label) → UTC 07:12:58 with offset 3; ingested 07:21:04 UTC.
        close_broker = datetime.datetime(2026, 7, 16, 10, 15, 58, tzinfo=UTC)
        ingested = datetime.datetime(2026, 7, 16, 7, 21, 4, tzinfo=UTC)
        self._trade(plan, 1, close_broker=datetime.datetime(2026, 7, 16, 10, 14, 31, tzinfo=UTC))
        self._trade(plan, 2, close_broker=close_broker, close_ingested=ingested)  # TP2 leg = trigger
        self._trade(plan, 3)  # leg 3 open at ingest time; protection target
        leg3 = plan.legs.get(leg_index=3)
        leg3.protection_stage = "TP2_LOCKED"; leg3.breakeven_attempts = 1
        leg3.save(update_fields=["protection_stage", "breakeven_attempts"])
        self._modify(plan, 3, "TP2_LOCKED",
                     created=datetime.datetime(2026, 7, 16, 7, 22, 3, tzinfo=UTC),
                     started=datetime.datetime(2026, 7, 16, 7, 22, 5, tzinfo=UTC),
                     finished=datetime.datetime(2026, 7, 16, 7, 26, 6, tzinfo=UTC))
        with mock.patch.object(pl, "BROKER_UTC_OFFSET_HOURS", 3):
            rec = pl.leg_protection_latency(plan, 3)
        seg = rec["segments"]
        self.assertEqual(rec["final_stage"], "TP2_LOCKED")
        self.assertEqual(rec["verified_sl"], "4025.3")
        self.assertEqual(seg["B_ingestion_to_detection"], 59.0)          # 07:21:04 → 07:22:03
        self.assertEqual(seg["D_enqueue_to_claim"], 2.0)                 # 07:22:03 → 07:22:05
        self.assertEqual(seg["worker_claim_to_verified"], 241.0)        # 07:22:05 → 07:26:06
        self.assertEqual(seg["system_ingestion_to_verified"], 302.0)   # offset-INDEPENDENT
        # A uses the offset: broker_to_utc(10:15:58)=07:15:58 → ingested 07:21:04 = 306s
        self.assertEqual(seg["A_broker_close_to_ingestion"], 306.0)
        self.assertFalse(rec["offset_assumed"]["verified"])            # honestly flagged unverified

    def test_missing_data_is_unknown_not_zero(self):
        plan = self._plan()
        self._trade(plan, 3)                      # leg 3 open; no trigger close, no modify, no ingest
        leg3 = plan.legs.get(leg_index=3)
        leg3.protection_stage = "TP2_LOCKED"
        leg3.save(update_fields=["protection_stage"])
        rec = pl.leg_protection_latency(plan, 3)
        seg = rec["segments"]
        for k in ("A_broker_close_to_ingestion", "B_ingestion_to_detection", "D_enqueue_to_claim",
                  "system_ingestion_to_verified", "H_broker_close_to_verified"):
            self.assertIsNone(seg[k])             # UNKNOWN, never 0
        self.assertIsNone(rec["verified_at"])


class FloorStatsTests(LatencyBase):
    def test_soft_deferral_window_measured(self):
        plan = self._plan()
        # Anchored to NOW, not to a calendar date. protection_floor_stats() filters on a ROLLING
        # `days=7` window, so a hard-coded base silently ages out of it and the test starts failing on a
        # date unrelated to any code change. It did: the old base of 2026-07-16 07:22 crossed the window
        # boundary at 07:24 UTC on 2026-07-23 and turned CI red mid-run — one job on a commit passed and
        # its sibling on the SAME commit failed. One day back keeps the fixture inside the window for good
        # while staying entirely in the past.
        base = timezone.now() - datetime.timedelta(days=1)
        # 3 deferred attempts then a verified success 240s after first attempt.
        for k in range(3):
            self._modify(plan, 3, "TP2_LOCKED", created=base + datetime.timedelta(minutes=k),
                         started=base + datetime.timedelta(minutes=k), finished=base + datetime.timedelta(minutes=k),
                         status="FAILED", error="deferred: sl_within_stops_level")
        self._modify(plan, 3, "TP2_LOCKED", created=base + datetime.timedelta(minutes=3),
                     started=base + datetime.timedelta(minutes=4),
                     finished=base + datetime.timedelta(seconds=240))
        stats = pl.protection_floor_stats(source="ti_signals", days=7)
        self.assertEqual(stats["deferred_groups"], 1)
        self.assertEqual(stats["resolved_naturally"], 1)
        self.assertEqual(stats["deferral_window"]["n"], 1)
        self.assertEqual(stats["deferral_window"]["max_s"], 240.0)
        self.assertIn("TP2_LOCKED", stats["by_stage"])


class OpsBlockTests(LatencyBase):
    def test_tp_protection_block_shape(self):
        from reliability.services.operations_summary import _tp_protection_block
        block = _tp_protection_block(timezone.now())
        for k in ("sla", "broker_floor", "recent_legs", "broker_utc_offset_hours_assumed"):
            self.assertIn(k, block)
        self.assertIn(block["sla"]["status"], ("HEALTHY", "WARNING", "UNKNOWN"))


class CloseIngestedAtTests(LatencyBase):
    def _deal(self, pid, entry_type, price, t, comment="WAY1L3", side="1"):
        return {"position_id": pid, "entry": entry_type, "type": side, "symbol": "XAUUSD",
                "volume": 0.40, "price": price, "profit": 5.0, "commission": 0.0, "swap": 0.0,
                "comment": comment, "magic": 0, "time": int(t.timestamp())}

    def test_worker_stamps_close_ingested_at_on_close(self):
        from mt5_trade_ingest_worker import upsert_trades
        o = datetime.datetime(2026, 7, 16, 10, 3, 0, tzinfo=UTC)
        c = datetime.datetime(2026, 7, 16, 10, 26, 0, tzinfo=UTC)
        # 1) open only → no close, no close_ingested_at
        upsert_trades(self.acct, [self._deal("900", 0, 4028.92, o)])
        tr = Trade.objects.get(account=self.acct, ticket="900")
        self.assertIsNone(tr.close_time)
        self.assertIsNone(tr.close_ingested_at)
        # 2) now the exit deal appears → close_time set AND close_ingested_at stamped (authoritative)
        upsert_trades(self.acct, [self._deal("900", 0, 4028.92, o), self._deal("900", 1, 4025.30, c)])
        tr.refresh_from_db()
        self.assertIsNotNone(tr.close_time)
        self.assertIsNotNone(tr.close_ingested_at)
        first_stamp = tr.close_ingested_at
        # 3) idempotent re-sync → close_ingested_at NEVER moves
        upsert_trades(self.acct, [self._deal("900", 0, 4028.92, o), self._deal("900", 1, 4025.30, c)])
        tr.refresh_from_db()
        self.assertEqual(tr.close_ingested_at, first_stamp)


class ErrorNullResilienceTests(LatencyBase):
    """Review SHOULD_FIX: a MODIFY result with a JSON-null ``error`` must NOT raise (which would blank
    the whole /operations TP-protection section via the ops block's try/except)."""

    def test_null_error_in_result_does_not_raise(self):
        plan = self._plan()
        self._trade(plan, 2, close_broker=timezone.now(), close_ingested=timezone.now())
        self._trade(plan, 3)
        leg3 = plan.legs.get(leg_index=3)
        leg3.protection_stage = "TP2_LOCKED"
        leg3.save(update_fields=["protection_stage"])
        j = ExecutionJob.objects.create(
            job_type="MODIFY_POSITION", account=self.acct, status="SUCCESS",
            payload={"plan_id": plan.id, "leg_index": 3, "protection_stage": "TP2_LOCKED",
                     "signal_source": "ti_signals"},
            result={"ok": True, "verified_sl": "4025.3", "error": None},  # JSON null error
            started_at=timezone.now(), finished_at=timezone.now())
        # must not raise (previously None + str -> TypeError)
        rec = pl.leg_protection_latency(plan, 3)
        self.assertEqual(rec["final_stage"], "TP2_LOCKED")
        self.assertEqual(rec["defer_count"], 0)
        stats = pl.protection_floor_stats(source="ti_signals", days=7)
        self.assertEqual(stats["deferred_groups"], 0)
