"""WS-C — execution observability: orphaned-SYNC reclaim, stuck-PENDING-order detection,
per-source exposure attribution, and durable auto-router deferral reasons.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import execution_health, risk_controls
from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan
from signal_intake.models import PendingSignalApproval, SignalAuditEvent
from trading.models import Trade, TradingAccount

User = get_user_model()
TI = "ti_signals"
WAY = "wayond"


class ExecutionHealthTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="eh", email="eh@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="EH1", is_demo=True)

    def _job(self, job_type, status, *, lease_delta_s=None, created_delta_s=None, node=None):
        j = ExecutionJob.objects.create(job_type=job_type, account=self.acct, status=status,
                                        terminal_node_id=node, payload={"signal_source": TI, "symbol": "XAUUSD"})
        now = timezone.now()
        fields = {}
        if status == ExecutionJob.Status.RUNNING:
            fields["started_at"] = now
            if lease_delta_s is not None:
                fields["lease_expires_at"] = now + timedelta(seconds=lease_delta_s)
        if created_delta_s is not None:
            fields["created_at"] = now + timedelta(seconds=created_delta_s)
        if fields:
            ExecutionJob.objects.filter(id=j.id).update(**fields)
        return ExecutionJob.objects.get(id=j.id)

    # -- reclaim orphaned SYNC -------------------------------------------------
    def test_reclaims_lease_expired_sync_only(self):
        dead = self._job("SYNC_POSITIONS", "RUNNING", lease_delta_s=-60)     # expired → reclaim
        live = self._job("SYNC_POSITIONS", "RUNNING", lease_delta_s=+300)    # valid → keep
        order = self._job("PLACE_ORDER", "RUNNING", lease_delta_s=-60)       # not SYNC → keep
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["reclaimed"], 1)
        dead.refresh_from_db(); live.refresh_from_db(); order.refresh_from_db()
        self.assertEqual(dead.status, "FAILED")
        self.assertTrue(dead.recovered)
        self.assertEqual(live.status, "RUNNING")
        self.assertEqual(order.status, "RUNNING")

    # -- stuck PENDING order detector -----------------------------------------
    def test_alerts_stuck_pending_order(self):
        from reliability.models import AlertEvent
        stuck = self._job("PLACE_ORDER", "PENDING",
                          created_delta_s=-(execution_health.STUCK_PENDING_ORDER_SECONDS + 60))
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["stuck_alerted"], 1)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"stuck_pending_order:job:{stuck.id}", status="OPEN").exists())
        # deduped on a second pass
        execution_health.sweep_execution_health()
        self.assertEqual(AlertEvent.objects.filter(
            dedup_key=f"stuck_pending_order:job:{stuck.id}").count(), 1)

    def test_fresh_pending_order_and_pending_sync_not_alerted(self):
        self._job("PLACE_ORDER", "PENDING", created_delta_s=-10)            # fresh → no alert
        self._job("SYNC_POSITIONS", "PENDING",
                  created_delta_s=-(execution_health.STUCK_PENDING_ORDER_SECONDS + 60))  # SYNC → no alert
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["stuck_alerted"], 0)

    def test_disabled_is_noop(self):
        self._job("SYNC_POSITIONS", "RUNNING", lease_delta_s=-60)
        with mock.patch.object(execution_health, "execution_health_enabled", return_value=False):
            res = execution_health.sweep_execution_health()
        self.assertEqual(res, {"enabled": False})
        self.assertEqual(ExecutionJob.objects.filter(status="RUNNING").count(), 1)


class ExposureAttributionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ea", email="ea@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="EA1", is_demo=True)

    def _plan(self, source, status, mid):
        appr = PendingSignalApproval.objects.create(
            source=source, message_id=mid, symbol="XAUUSD", direction="BUY", stop_loss="4000",
            take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)
        return SignalExecutionPlan.objects.create(
            approval=appr, account=self.acct, source=source, message_id=mid, symbol="XAUUSD",
            direction="BUY", is_demo=True, signal_timestamp=timezone.now(), status=status)

    def test_attribution_by_source(self):
        self._plan(TI, SignalExecutionPlan.Status.PROMOTED, "t1")
        self._plan(TI, SignalExecutionPlan.Status.PLANNED, "t2")
        self._plan(WAY, SignalExecutionPlan.Status.PROMOTED, "w1")
        attr = risk_controls.exposure_attribution(self.acct.id)
        self.assertEqual(attr[TI]["active_plans"], 2)
        self.assertEqual(attr[WAY]["active_plans"], 1)


class AutoRouterDeferralTests(TestCase):
    def setUp(self):
        self.appr = PendingSignalApproval.objects.create(
            source=TI, message_id="m1", symbol="XAUUSD", direction="BUY", stop_loss="4000",
            take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)

    def test_manual_fall_through_persists_reason(self):
        from execution import auto_router
        from signal_intake.models import AcquiredMessage
        with mock.patch.object(auto_router, "effective_mode",
                               return_value=(auto_router.MODE_MANUAL, "source_not_enabled")):
            auto_router.route_acquired_signal(
                outcome=AcquiredMessage.Outcome.INTAKEN, approval=self.appr)
        ev = SignalAuditEvent.objects.filter(
            approval=self.appr, event=SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED).first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.detail.get("reason"), "source_not_enabled")

    def test_record_deferral_fail_open(self):
        # A broken audit write must never raise into the caller.
        from execution import auto_router
        with mock.patch("signal_intake.models.SignalAuditEvent.objects.create",
                        side_effect=RuntimeError("db down")):
            auto_router._record_deferral(self.appr, "kill_switch")  # must not raise


class UnplannedSignalAlertTests(TestCase):
    """GFX-PKT-TI-SIGNALS-NON-EXECUTION-INCIDENT — a tradeable APPROVED signal with NEITHER a plan
    NOR a durable AUTO_ROUTE_DEFERRED reason is a silent loss → deduped WARN, auto-resolving."""

    def setUp(self):
        from execution.models import SignalSourceConfig
        self.user = User.objects.create_user(username="us", email="us@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="US1", is_demo=True)
        SignalSourceConfig.objects.create(source=TI, auto_demo_execution_enabled=True)
        SignalSourceConfig.objects.create(source=WAY, auto_demo_execution_enabled=False)

    def _appr(self, mid, *, age_s):
        a = PendingSignalApproval.objects.create(
            source=TI, message_id=mid, symbol="XAUUSD", direction="BUY",
            stop_loss="4050", take_profits=["4060", "4061", "4062"],
            status=PendingSignalApproval.Status.APPROVED)
        PendingSignalApproval.objects.filter(id=a.id).update(
            created_at=timezone.now() - timedelta(seconds=age_s))
        return PendingSignalApproval.objects.get(id=a.id)

    def test_silent_unplanned_signal_alerts(self):
        from reliability.models import AlertEvent
        a = self._appr("u1", age_s=execution_health.UNPLANNED_SIGNAL_ALERT_SECONDS + 60)
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["unplanned_alerted"], 1)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"unplanned_tradeable_signal:approval:{a.id}", status="OPEN").exists())

    def test_recent_signal_not_yet_alerted(self):
        self._appr("u2", age_s=10)  # within the synchronous plan window → not stuck
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["unplanned_alerted"], 0)

    def test_durable_reason_suppresses_and_resolves(self):
        from reliability.models import AlertEvent
        a = self._appr("u3", age_s=execution_health.UNPLANNED_SIGNAL_ALERT_SECONDS + 60)
        execution_health.sweep_execution_health()  # opens the alert
        SignalAuditEvent.objects.create(
            event=SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED, approval=a,
            detail={"reason": "auto-demo_rejected:plan_integrity_error"})
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["unplanned_resolved"], 1)
        self.assertFalse(AlertEvent.objects.filter(
            dedup_key=f"unplanned_tradeable_signal:approval:{a.id}", status="OPEN").exists())

    def test_planned_signal_not_alerted(self):
        a = self._appr("u4", age_s=execution_health.UNPLANNED_SIGNAL_ALERT_SECONDS + 60)
        SignalExecutionPlan.objects.create(
            approval=a, account=self.acct, source=TI, message_id="u4", symbol="XAUUSD",
            direction="BUY", is_demo=True, status=SignalExecutionPlan.Status.PLANNED)
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["unplanned_alerted"], 0)

    def test_non_tradeable_source_ignored(self):
        # A signal from a source that is NOT auto-eligible is a manual-only signal, not a silent loss.
        a = PendingSignalApproval.objects.create(
            source=WAY, message_id="w1", symbol="XAUUSD", direction="BUY", stop_loss="1",
            take_profits=["2"], status=PendingSignalApproval.Status.APPROVED)
        PendingSignalApproval.objects.filter(id=a.id).update(
            created_at=timezone.now() - timedelta(seconds=execution_health.UNPLANNED_SIGNAL_ALERT_SECONDS + 60))
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["unplanned_alerted"], 0)


class OrphanedPlaceOrderReconcileTests(TestCase):
    """GFX-PKT-POST-INCIDENT — a lease-expired RUNNING PLACE_ORDER is NEVER re-run (would duplicate);
    it is reconciled against the broker: trade exists → SUCCESS; no trade → alert only."""

    def setUp(self):
        self.user = User.objects.create_user(username="po", email="po@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="PO1", is_demo=True)

    def _po_job(self, plan_id, leg_index, *, lease_delta_s):
        j = ExecutionJob.objects.create(
            job_type="PLACE_ORDER", account=self.acct, status="RUNNING",
            payload={"plan_id": plan_id, "leg_index": leg_index, "signal_source": TI, "symbol": "XAUUSD"})
        ExecutionJob.objects.filter(id=j.id).update(
            started_at=timezone.now(), lease_expires_at=timezone.now() + timedelta(seconds=lease_delta_s))
        return ExecutionJob.objects.get(id=j.id)

    def _trade(self, plan_id, leg_index, ticket):
        return Trade.objects.create(
            account=self.acct, symbol="XAUUSD", side="BUY", volume=Decimal("0.40"),
            ticket=str(ticket), open_time=timezone.now(), open_price=Decimal("4059.21"),
            comment="WAY%sL%s" % (plan_id, leg_index))

    def test_orphan_with_trade_reconciled_to_success(self):
        job = self._po_job(22, 3, lease_delta_s=-120)     # lease expired
        self._trade(22, 3, 224368)                        # the order actually landed
        res = execution_health.sweep_execution_health()
        job.refresh_from_db()
        self.assertEqual(res["place_order_reconciled"], 1)
        self.assertEqual(res["place_order_orphan_alerted"], 0)
        self.assertEqual(job.status, "SUCCESS")           # never re-run; bookkeeping caught up
        self.assertTrue(job.recovered)
        self.assertEqual((job.result or {}).get("ticket"), "224368")

    def test_orphan_without_trade_alerts_and_does_not_rerun(self):
        from reliability.models import AlertEvent
        job = self._po_job(23, 1, lease_delta_s=-120)     # lease expired, NO trade
        res = execution_health.sweep_execution_health()
        job.refresh_from_db()
        self.assertEqual(res["place_order_orphan_alerted"], 1)
        self.assertEqual(res["place_order_reconciled"], 0)
        self.assertEqual(job.status, "RUNNING")           # NOT re-run, NOT marked success (fail-safe)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"orphaned_place_order:job:{job.id}", status="OPEN").exists())

    def test_live_lease_not_touched(self):
        job = self._po_job(24, 1, lease_delta_s=+300)     # lease still valid
        res = execution_health.sweep_execution_health()
        job.refresh_from_db()
        self.assertEqual(res["place_order_reconciled"], 0)
        self.assertEqual(res["place_order_orphan_alerted"], 0)
        self.assertEqual(job.status, "RUNNING")

    def test_alert_auto_resolves_when_trade_appears(self):
        from reliability.models import AlertEvent
        job = self._po_job(25, 2, lease_delta_s=-120)
        execution_health.sweep_execution_health()          # alerts (no trade yet)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"orphaned_place_order:job:{job.id}", status="OPEN").exists())
        self._trade(25, 2, 224400)                         # the trade is ingested late
        res = execution_health.sweep_execution_health()
        job.refresh_from_db()
        self.assertEqual(res["place_order_reconciled"], 1)
        self.assertEqual(job.status, "SUCCESS")
        self.assertFalse(AlertEvent.objects.filter(
            dedup_key=f"orphaned_place_order:job:{job.id}", status="OPEN").exists())


class OrphanedPlaceOrderResolveTests(TestCase):
    """GFX-PKT-POST-INCIDENT (adversarial-review fix) — the orphan alert resolves on the BROKER
    signal (leg trade present), NOT on job status, so a merely-slow worker completing its own job
    doesn't strand a lingering alert, and a genuinely FAILED/missing order keeps the alert open."""

    def setUp(self):
        self.user = User.objects.create_user(username="pr", email="pr@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="PR1", is_demo=True)

    def _po(self, plan_id, leg_index):
        j = ExecutionJob.objects.create(
            job_type="PLACE_ORDER", account=self.acct, status="RUNNING",
            payload={"plan_id": plan_id, "leg_index": leg_index, "signal_source": TI, "symbol": "XAUUSD"})
        ExecutionJob.objects.filter(id=j.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=120))
        return ExecutionJob.objects.get(id=j.id)

    def _trade(self, plan_id, leg_index, ticket):
        Trade.objects.create(account=self.acct, symbol="XAUUSD", side="BUY", volume=Decimal("0.40"),
                             ticket=str(ticket), open_time=timezone.now(), open_price=Decimal("4059"),
                             comment="WAY%sL%s" % (plan_id, leg_index))

    def test_alert_resolves_after_slow_worker_completes_and_trade_ingested(self):
        from reliability.models import AlertEvent
        job = self._po(26, 1)
        execution_health.sweep_execution_health()          # alerts (no trade, job RUNNING)
        key = f"orphaned_place_order:job:{job.id}"
        self.assertTrue(AlertEvent.objects.filter(dedup_key=key, status="OPEN").exists())
        # The worker was only slow: it completes its OWN job (leaves RUNNING) and the trade ingests.
        ExecutionJob.objects.filter(id=job.id).update(status="SUCCESS")
        self._trade(26, 1, 224500)
        res = execution_health.sweep_execution_health()
        self.assertGreaterEqual(res["place_order_orphan_resolved"], 1)
        self.assertFalse(AlertEvent.objects.filter(dedup_key=key, status="OPEN").exists())

    def test_alert_stays_open_when_job_failed_without_trade(self):
        from reliability.models import AlertEvent
        job = self._po(27, 1)
        execution_health.sweep_execution_health()          # alerts
        ExecutionJob.objects.filter(id=job.id).update(status="FAILED")  # order failed → NO trade
        execution_health.sweep_execution_health()
        # A genuinely missing order must NOT be silently resolved just because the job left RUNNING.
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"orphaned_place_order:job:{job.id}", status="OPEN").exists())


class ProtectionWatcherHealthTests(TestCase):
    """GFX-PKT-TP-PROTECTION-LATENCY WS-L — deduped, auto-resolving alerts for the fast-protection path:
    a stale (armed) watcher heartbeat, and repeated protection-SYNC strands (the bridge/MT5 stall)."""

    def setUp(self):
        self.user = User.objects.create_user(username="pw", email="pw@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="PW1", is_demo=True)

    def _beat(self, age_s, interval=90):
        from reliability.models import Heartbeat
        Heartbeat.objects.update_or_create(
            source="tp_protection_watcher",
            defaults={"last_beat_at": timezone.now() - timedelta(seconds=age_s),
                      "expected_interval_s": interval, "detail": {"state": "idle"}})

    def test_stale_armed_watcher_alerts_then_resolves(self):
        from reliability.models import AlertEvent
        self._beat(age_s=400, interval=90)     # >3x interval → stale
        with mock.patch.dict("os.environ", {"TP_WATCHER_ENABLED": "1"}, clear=False):
            r = execution_health.detect_protection_watcher_health(timezone.now())
        self.assertEqual(r["watcher_stale_alerted"], 1)
        self.assertTrue(AlertEvent.objects.filter(dedup_key="tp_watcher_stale", status="OPEN").exists())
        # fresh beat → auto-resolves
        self._beat(age_s=5, interval=90)
        with mock.patch.dict("os.environ", {"TP_WATCHER_ENABLED": "1"}, clear=False):
            execution_health.detect_protection_watcher_health(timezone.now())
        self.assertFalse(AlertEvent.objects.filter(dedup_key="tp_watcher_stale", status="OPEN").exists())

    def test_not_armed_watcher_never_alerts(self):
        from reliability.models import AlertEvent
        # No heartbeat at all, but watcher disabled → not a fault.
        with mock.patch.dict("os.environ", {"TP_WATCHER_ENABLED": "0"}, clear=False):
            execution_health.detect_protection_watcher_health(timezone.now())
        self.assertFalse(AlertEvent.objects.filter(dedup_key="tp_watcher_stale", status="OPEN").exists())

    def test_repeated_sync_strands_alert(self):
        from reliability.models import AlertEvent
        now = timezone.now()
        for i in range(3):
            ExecutionJob.objects.create(
                job_type="SYNC_POSITIONS", account=self.acct, status="FAILED", recovered=True,
                payload={"breakeven_sync": True}, finished_at=now - timedelta(minutes=5))
        r = execution_health.detect_protection_watcher_health(now)
        self.assertEqual(r["sync_stall_alerted"], 1)
        self.assertTrue(AlertEvent.objects.filter(dedup_key="protection_sync_stall", status="OPEN").exists())
