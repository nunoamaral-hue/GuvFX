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
