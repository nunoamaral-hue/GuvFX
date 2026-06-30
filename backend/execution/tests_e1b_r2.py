"""
EXEC-E1b-R2 tests — fail-closed robustness fixes.

Covers: naive signal timestamps are made aware (no TypeError during the age
calculation); HELD/VOIDED creation races return the existing plan idempotently;
invalid/NaN lot values become a clean HELD; and the no-order guarantee holds.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import signal_planning as planning
from execution.models import (
    ExecutionJob,
    SignalExecutionPlan,
    SignalSourceConfig,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()
SRC = PendingSignalApproval.Source.WAYOND_TELEGRAM


def _approved(message_id, *, tps=("1.0900", "1.0950", "1.1000"), sl="1.0800",
              symbol="EURUSD", direction="BUY", raw_payload=None):
    return PendingSignalApproval.objects.create(
        source=SRC, message_id=message_id, symbol=symbol, direction=direction,
        entry="1.0850", stop_loss=sl, take_profit=(tps[0] if tps else ""),
        take_profits=list(tps), raw_payload=raw_payload or {},
        status=PendingSignalApproval.Status.APPROVED,
    )


class NaiveTimestampTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True
        )
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)

    def test_naive_payload_timestamp_does_not_raise_and_plans(self):
        # A NAIVE ISO string near now (the old code would raise TypeError here).
        naive_now = timezone.now().replace(tzinfo=None, microsecond=0).isoformat()
        plan = planning.plan_demo_execution(
            _approved("n1", raw_payload={"signal_timestamp": naive_now}),
            account=self.demo,
        )
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)
        self.assertEqual(plan.legs.count(), 3)
        self.assertIsNotNone(plan.signal_timestamp)
        self.assertFalse(timezone.is_naive(plan.signal_timestamp))
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_naive_old_timestamp_is_voided_not_crashed(self):
        old_naive = (timezone.now() - timedelta(seconds=600)).replace(tzinfo=None).isoformat()
        plan = planning.plan_demo_execution(
            _approved("n2", raw_payload={"date": old_naive}), account=self.demo
        )
        self.assertEqual(plan.status, SignalExecutionPlan.Status.VOIDED)
        self.assertEqual(plan.hold_reason, "stale_signal")
        self.assertEqual(plan.legs.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_naive_override_is_made_aware(self):
        naive_override = timezone.now().replace(tzinfo=None)
        plan = planning.plan_demo_execution(
            _approved("n3"), account=self.demo, signal_timestamp=naive_override
        )
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)
        self.assertFalse(timezone.is_naive(plan.signal_timestamp))

    def test_aware_helper(self):
        naive = timezone.now().replace(tzinfo=None)
        self.assertFalse(timezone.is_naive(planning._aware(naive)))
        aware = timezone.now()
        self.assertEqual(planning._aware(aware), aware)
        self.assertIsNone(planning._aware(None))


class HoldVoidIdempotencyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op2", email="op2@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D2", is_demo=True
        )
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)

    def _common(self, approval):
        return dict(
            approval=approval, account=self.demo, source=SRC, chat_id="",
            message_id=approval.message_id, symbol="EURUSD", direction="BUY",
            entry="1.0850", stop_loss="", is_demo=True, account_environment="",
            signal_timestamp=timezone.now(), proposed_by=self.user,
        )

    def test_hold_race_returns_existing_plan(self):
        # Direct call simulates a race: the 2nd create hits the OneToOne
        # constraint and the IntegrityError fallback returns the existing plan.
        a = _approved("h1", sl="")
        p1 = planning._hold(self._common(a), self.user, "missing_stop_loss")
        p2 = planning._hold(self._common(a), self.user, "missing_stop_loss")
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(SignalExecutionPlan.objects.filter(approval=a).count(), 1)

    def test_void_race_returns_existing_plan(self):
        a = _approved("v1")
        p1 = planning._void(self._common(a), self.user, "stale_signal")
        p2 = planning._void(self._common(a), self.user, "stale_signal")
        self.assertEqual(p1.id, p2.id)

    def test_missing_sl_plan_is_idempotent_via_planner(self):
        a = _approved("h2", sl="")
        p1 = planning.plan_demo_execution(a, account=self.demo)
        p2 = planning.plan_demo_execution(a, account=self.demo)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(p1.status, SignalExecutionPlan.Status.HELD)


class InvalidLotTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op3", email="op3@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D3", is_demo=True
        )
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)

    def test_split_volume_rejects_nan_and_inf(self):
        for bad in (float("nan"), float("inf"), "not_a_number", Decimal("-0.01"), 0):
            with self.assertRaises(planning.VolumeSplitError):
                planning.split_volume(bad, 3)

    def test_invalid_total_lot_is_held_not_crashed(self):
        plan = planning.plan_demo_execution(
            _approved("i1"), account=self.demo, total_lot="not_a_number"
        )
        self.assertEqual(plan.status, SignalExecutionPlan.Status.HELD)
        self.assertEqual(plan.hold_reason, "volume_split_invalid")
        self.assertEqual(plan.legs.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_nan_total_lot_is_held(self):
        plan = planning.plan_demo_execution(
            _approved("i2"), account=self.demo, total_lot=float("nan")
        )
        self.assertEqual(plan.status, SignalExecutionPlan.Status.HELD)
        self.assertEqual(plan.hold_reason, "volume_split_invalid")


class NoOrderStillHoldsTests(TestCase):
    def test_no_execution_job_after_r2_paths(self):
        user = User.objects.create_user(username="op4", email="op4@x.invalid", password="x")
        demo = TradingAccount.objects.create(
            user=user, name="Demo", account_number="D4", is_demo=True
        )
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)
        naive = timezone.now().replace(tzinfo=None).isoformat()
        planning.plan_demo_execution(_approved("o1", raw_payload={"date": naive}), account=demo)
        # Different symbol so it is a separate group (EURUSD's slot is taken).
        planning.plan_demo_execution(_approved("o2", sl="", symbol="GBPUSD"), account=demo)  # held
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(
            ExecutionJob.objects.filter(status=ExecutionJob.Status.PENDING).count(), 0
        )
