"""GFX-PKT-TP-PROTECTION-LATENCY — the adaptive ti_signals protection watcher + SYNC stall resilience.

Proves: the watcher DRIVES the existing ladder scoped to ti_signals (Wayond never touched); adaptive
cadence (idle/pre/active); dry-run persists nothing; single-flight advisory lock blocks a duplicate;
a stranded protection SYNC is reclaimed fast (short lease) so ingestion is never blind for minutes.
"""
from decimal import Decimal
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, connections
from django.test import TestCase
from django.utils import timezone

from execution import breakeven
from execution.management.commands.run_tp_protection_watcher import (
    Command, WATCHER_SOURCE, _ADVISORY_LOCK_KEY)
from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan, SignalSourceConfig
from signal_intake.models import PendingSignalApproval
from trading.models import Trade, TradingAccount

User = get_user_model()
TI = "ti_signals"
WAY = "wayond"


def _enable():
    return mock.patch.object(breakeven, "breakeven_enabled", return_value=True)


def _arm_watcher():
    return mock.patch.dict("os.environ", {"TP_WATCHER_ENABLED": "1"}, clear=False)


class WatcherBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="w", email="w@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="W1", is_demo=True)
        SignalSourceConfig.objects.create(source=TI, incremental_protection_enabled=True)
        SignalSourceConfig.objects.create(source=WAY, incremental_protection_enabled=False)
        p = mock.patch.object(breakeven, "_windows_username", return_value="mt5user")
        p.start(); self.addCleanup(p.stop)
        self._n = 0

    def _plan(self, *, source=TI, direction="SELL", entry="4038", sl="4043",
              tps=("4036", "4034", "4032"), states=("open", "open", "open")):
        self._n += 1
        mid = f"w{self._n}"
        appr = PendingSignalApproval.objects.create(
            source=source, message_id=mid, symbol="XAUUSD", direction=direction, stop_loss=sl,
            take_profits=list(tps), status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=self.acct, source=source, message_id=mid, symbol="XAUUSD",
            direction=direction, stop_loss=sl, is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PROMOTED)
        now = timezone.now()
        for i, (tp, st) in enumerate(zip(tps, states), start=1):
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit=tp, stop_loss=sl, lot_size=Decimal("0.40"),
                status=ProposedOrderLeg.Status.PROMOTED)
            ct, cp = (None, None)
            if st == "tp":
                ct, cp = now, Decimal(tp)
            Trade.objects.create(
                account=self.acct, symbol="XAUUSD", side=direction, volume=Decimal("0.40"),
                ticket=f"pos{plan.id}{i}", open_time=now, open_price=Decimal(entry),
                close_time=ct, close_price=cp, comment=f"WAY{plan.id}L{i}")
        return plan

    def _tick(self, idle=30, pre=3, active=1):
        return Command()._do_tick(idle, pre, active)

    def _modify_jobs(self):
        return list(ExecutionJob.objects.filter(job_type="MODIFY_POSITION"))


class SourceScopingTests(WatcherBase):
    def test_watcher_sweep_touches_ti_signals_only(self):
        ti = self._plan(source=TI, states=("tp", "open", "open"))    # TP1 closed → breakeven due
        way = self._plan(source=WAY, states=("tp", "open", "open"))  # Wayond, same shape
        with _enable(), _arm_watcher():
            self._tick()
        jobs = self._modify_jobs()
        self.assertTrue(jobs)
        plan_ids = {(j.payload or {}).get("plan_id") for j in jobs}
        self.assertIn(ti.id, plan_ids)
        self.assertNotIn(way.id, plan_ids)   # Wayond never touched by the watcher

    def test_sweep_sources_filter_isolates(self):
        self._plan(source=TI, states=("tp", "open", "open"))
        self._plan(source=WAY, states=("tp", "open", "open"))
        with _enable():
            res = breakeven.sweep_breakeven(sources={TI})
        self.assertEqual(res["scanned"], 1)          # only the ti_signals plan scanned
        self.assertGreaterEqual(res["open_legs"], 2)


class CadenceTests(WatcherBase):
    def test_idle_when_no_eligible_plan(self):
        with _enable(), _arm_watcher():
            self.assertEqual(self._tick(idle=30, pre=3, active=1), 30)
        hb = self._heartbeat()
        self.assertEqual(hb["state"], "idle")

    def test_pre_cadence_all_legs_open_before_tp1(self):
        self._plan(source=TI, states=("open", "open", "open"))
        with _enable(), _arm_watcher():
            self.assertEqual(self._tick(idle=30, pre=3, active=1), 3)
        self.assertEqual(self._heartbeat()["state"], "pre")

    def test_active_cadence_when_protection_due(self):
        self._plan(source=TI, states=("tp", "open", "open"))   # TP1 closed → breakeven enqueued → hot
        with _enable(), _arm_watcher():
            self.assertEqual(self._tick(idle=30, pre=3, active=1), 1)
        self.assertEqual(self._heartbeat()["state"], "active")

    def test_disabled_watcher_idles_and_does_not_sweep(self):
        self._plan(source=TI, states=("tp", "open", "open"))
        with _enable():   # ladder armed, but TP_WATCHER_ENABLED off
            cad = self._tick(idle=30, pre=3, active=1)
        self.assertEqual(cad, 30)
        self.assertFalse(self._modify_jobs())          # no sweep ran
        self.assertEqual(self._heartbeat()["state"], "disabled")

    def _heartbeat(self):
        from reliability.models import Heartbeat
        return Heartbeat.objects.get(source=WATCHER_SOURCE).detail


class DryRunTests(WatcherBase):
    def test_dry_run_persists_nothing(self):
        self._plan(source=TI, states=("tp", "open", "open"))
        with _enable(), _arm_watcher():
            cad = Command()._tick(dry_run=True, idle=30, pre=3, active=1)
        self.assertEqual(cad, 1)                       # cadence still computed (hot)
        self.assertFalse(self._modify_jobs())          # rolled back → nothing enqueued


class SingleFlightTests(WatcherBase):
    def test_duplicate_watcher_blocked_by_advisory_lock(self):
        # A second DB session holds the lock → this watcher's acquire must fail (single-flight).
        other = connections.create_connection(DEFAULT_DB_ALIAS)
        try:
            with other.cursor() as c:
                c.execute("SELECT pg_try_advisory_lock(%s)", [_ADVISORY_LOCK_KEY])
                self.assertTrue(c.fetchone()[0])
            self.assertFalse(Command()._acquire_lock())   # blocked
        finally:
            with other.cursor() as c:
                c.execute("SELECT pg_advisory_unlock(%s)", [_ADVISORY_LOCK_KEY])
            other.close()

    def test_lock_acquire_release_roundtrip(self):
        cmd = Command()
        self.assertTrue(cmd._acquire_lock())
        cmd._release_lock()
        self.assertTrue(cmd._acquire_lock())           # re-acquire after release
        cmd._release_lock()


class SyncStallResilienceTests(WatcherBase):
    def test_stranded_protection_sync_reclaimed_by_tick(self):
        # A protection SYNC stuck RUNNING with an expired (short) lease is reclaimed by the tick so a
        # fresh sync can ingest the close — instead of blinding the ladder for minutes.
        stuck = ExecutionJob.objects.create(
            job_type="SYNC_POSITIONS", account=self.acct, status="RUNNING",
            payload={"breakeven_sync": True, "windows_username": "mt5user"},
            lease_expires_at=timezone.now() - timedelta(seconds=5))
        self._plan(source=TI, states=("tp", "open", "open"))
        with _enable(), _arm_watcher():
            self._tick()
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, "FAILED")       # reclaimed
        self.assertTrue(stuck.recovered)


class ProtectionSyncLeaseTests(WatcherBase):
    """GFX-PKT-TP-PROTECTION-LATENCY — the claim-site fix: a protection sync (breakeven_sync) gets a
    SHORT lease so a stranded one is reclaimed fast; a plain sync keeps the long default lease."""

    def _claim(self):
        from rest_framework.test import APIClient
        from execution.models import WorkerIdentity
        WorkerIdentity.objects.get_or_create(
            worker_id="w-lease",
            defaults={"worker_secret_hash": WorkerIdentity.hash_secret("s"),
                      "status": WorkerIdentity.Status.ACTIVE})
        return APIClient().get("/api/execution/jobs/next/?job_type=SYNC_POSITIONS",
                               HTTP_X_WORKER_ID="w-lease", HTTP_X_WORKER_SECRET="s")

    def test_protection_sync_gets_short_lease(self):
        job = ExecutionJob.objects.create(
            job_type="SYNC_POSITIONS", account=self.acct, status="PENDING",
            payload={"breakeven_sync": True})
        with mock.patch.dict("os.environ", {"EXECUTION_SYNC_LEASE_TTL_SECONDS": "60"}, clear=False):
            resp = self._claim()
        self.assertEqual(resp.status_code, 200)
        job.refresh_from_db()
        self.assertAlmostEqual((job.lease_expires_at - job.started_at).total_seconds(), 60, delta=5)

    def test_plain_sync_keeps_default_lease(self):
        job = ExecutionJob.objects.create(
            job_type="SYNC_POSITIONS", account=self.acct, status="PENDING", payload={})
        resp = self._claim()
        self.assertEqual(resp.status_code, 200)
        job.refresh_from_db()
        self.assertAlmostEqual((job.lease_expires_at - job.started_at).total_seconds(), 300, delta=5)
