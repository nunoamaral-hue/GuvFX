"""Phase 2 (Control 5) — FAILED-but-filled backstop in the execution-health sweep.

A PLACE_ORDER the bridge reported FAILED whose leg Trade LATER appears (a lost-ACK the in-process
bridge recovery missed) leaves exposure OPEN that the platform believes ABSENT. The reconciler now
raises ONE deduped CRITICAL alert. ALERT-ONLY: it never auto-flips FAILED->SUCCESS (that would mutate
already-resolved plan state); an operator reconciles, and the alert resolves once the job is no longer
FAILED. Bounded by FAILED_FILLED_LOOKBACK_SECONDS.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import execution_health
from execution.models import ExecutionJob
from trading.models import Trade, TradingAccount

User = get_user_model()


class FailedButFilledReconcileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fbf", email="fbf@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="FBF1", is_demo=True, broker_name="DemoBroker")

    def _failed_job(self, plan_id, leg_index, *, finished_delta_s=-10, job_type="PLACE_ORDER"):
        j = ExecutionJob.objects.create(
            job_type=job_type, account=self.acct, status="FAILED",
            payload={"plan_id": plan_id, "leg_index": leg_index,
                     "signal_source": "ti_signals", "symbol": "XAUUSD"})
        ExecutionJob.objects.filter(id=j.id).update(
            finished_at=timezone.now() + timedelta(seconds=finished_delta_s))
        return ExecutionJob.objects.get(id=j.id)

    def _trade(self, plan_id, leg_index, ticket):
        return Trade.objects.create(
            account=self.acct, symbol="XAUUSD", side="BUY", volume=Decimal("0.40"),
            ticket=str(ticket), open_time=timezone.now(), open_price=Decimal("4059.21"),
            comment="WAY%sL%s" % (plan_id, leg_index))

    def test_failed_with_trade_alerts_critical_and_does_not_flip(self):
        from reliability.models import AlertEvent
        job = self._failed_job(40, 1)
        self._trade(40, 1, 555001)
        res = execution_health.sweep_execution_health()
        job.refresh_from_db()
        self.assertEqual(res["place_order_failed_but_filled_alerted"], 1)
        self.assertEqual(job.status, "FAILED")  # ALERT-ONLY, never auto-flipped
        al = AlertEvent.objects.get(dedup_key=f"failed_but_filled:job:{job.id}")
        self.assertEqual(al.severity, "CRITICAL")
        self.assertEqual(al.status, "OPEN")

    def test_failed_without_trade_no_alert(self):
        self._failed_job(41, 1)  # genuinely failed, no position
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["place_order_failed_but_filled_alerted"], 0)

    def test_dedup_single_alert(self):
        from reliability.models import AlertEvent
        job = self._failed_job(42, 1)
        self._trade(42, 1, 555002)
        execution_health.sweep_execution_health()
        execution_health.sweep_execution_health()  # run again
        self.assertEqual(
            AlertEvent.objects.filter(dedup_key=f"failed_but_filled:job:{job.id}").count(), 1)

    def test_resolves_when_operator_reconciles_job(self):
        from reliability.models import AlertEvent
        job = self._failed_job(43, 1)
        self._trade(43, 1, 555003)
        execution_health.sweep_execution_health()
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"failed_but_filled:job:{job.id}", status="OPEN").exists())
        ExecutionJob.objects.filter(id=job.id).update(status="SUCCESS")  # operator reconciled
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["place_order_failed_but_filled_resolved"], 1)
        self.assertFalse(AlertEvent.objects.filter(
            dedup_key=f"failed_but_filled:job:{job.id}", status="OPEN").exists())

    def test_old_failed_outside_lookback_ignored(self):
        self._failed_job(44, 1, finished_delta_s=-7200)  # 2h ago, beyond the 1h lookback
        self._trade(44, 1, 555004)
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["place_order_failed_but_filled_alerted"], 0)

    def test_trade_on_other_account_does_not_alert(self):
        # account isolation (the primary false-positive guard): a matching WAY Trade on a DIFFERENT
        # account must NOT trigger the alert.
        other_user = User.objects.create_user(username="fbf2", email="fbf2@x.invalid", password="x")
        other = TradingAccount.objects.create(
            user=other_user, name="Demo2", account_number="FBF2", is_demo=True, broker_name="DemoBroker")
        self._failed_job(46, 1)
        Trade.objects.create(
            account=other, symbol="XAUUSD", side="BUY", volume=Decimal("0.40"), ticket="555006",
            open_time=timezone.now(), open_price=Decimal("4059.21"), comment="WAY46L1")  # same comment, WRONG account
        res = execution_health.sweep_execution_health()
        self.assertEqual(res["place_order_failed_but_filled_alerted"], 0)
