"""GFX-PKT-PRODUCTION-STABILISATION — exposure double-count fix + per-source unlimited daily cap.

The exposure gate double-counted a PROMOTED-and-filled plan (as both open positions AND active
signal legs), falsely tripping ``account_exposure_exceeded`` the moment a second signal overlapped
an open one — blocking every valid overlapping TI signal. The daily cap is now per-source and can
be set to 0 = unlimited so a source processes signals indefinitely (other gates still apply).
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import risk_controls, signal_planning as planning
from execution.models import (
    SignalExecutionPlan, ProposedOrderLeg, SignalSourceConfig,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount, Trade

User = get_user_model()
TI = "ti_signals"


class ExposureDoubleCountTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True)

    def _promoted(self, mid, lots, *, open_trades):
        appr = PendingSignalApproval.objects.create(
            source=TI, message_id=mid, symbol="XAUUSD", direction="BUY", stop_loss="4000",
            take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=self.demo, source=TI, message_id=mid, symbol="XAUUSD",
            direction="BUY", is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PROMOTED)
        for i, lot in enumerate(lots, start=1):
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit="4010", lot_size=Decimal(lot),
                status=ProposedOrderLeg.Status.PROMOTED)
            if open_trades:
                Trade.objects.create(
                    account=self.demo, symbol="XAUUSD", side="BUY", volume=Decimal(lot),
                    ticket=f"t{plan.id}{i}", open_time=timezone.now(), open_price=Decimal("4005"),
                    comment=f"WAY{plan.id}L{i}")  # close_time null → open
        return plan

    def test_filled_legs_not_double_counted(self):
        self._promoted("m1", ("0.40", "0.40", "0.40"), open_trades=True)  # 1.20 filled
        self.assertEqual(risk_controls._open_position_lots(self.demo.id), Decimal("1.20"))
        # the same lots must NOT also count as active signal lots (that was the double-count)
        self.assertEqual(risk_controls._active_signal_lots(self.demo.id), Decimal("0"))

    def test_inflight_leg_still_counted(self):
        self._promoted("m2", ("0.40",), open_trades=False)  # promoted, not yet an open trade
        self.assertEqual(risk_controls._active_signal_lots(self.demo.id), Decimal("0.40"))

    def test_second_overlapping_signal_not_falsely_rejected(self):
        self._promoted("mA", ("0.40", "0.40", "0.40"), open_trades=True)  # 1.20 open
        apprB = PendingSignalApproval.objects.create(
            source=TI, message_id="mB", symbol="XAUUSD", direction="BUY", stop_loss="4000",
            take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)
        planB = SignalExecutionPlan.objects.create(
            approval=apprB, account=self.demo, source=TI, message_id="mB", symbol="XAUUSD",
            direction="BUY", is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PLANNED)
        legsB = [ProposedOrderLeg.objects.create(plan=planB, leg_index=i, take_profit="4010",
                 lot_size=Decimal("0.40"), status=ProposedOrderLeg.Status.PLANNED) for i in (1, 2, 3)]
        with mock.patch.object(risk_controls, "MARGIN_GUARD_ENABLED", False):
            reason = risk_controls.evaluate_promotion_risk(planB, legsB)
        # 1.20 open + 0 active (deduped) + 1.20 new = 2.40 == cap → allowed (was falsely rejected)
        self.assertIsNone(reason)


class DailyCapPerSourceUnlimitedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op2", email="op2@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D2", is_demo=True)

    def _cfg(self, cap):
        return SignalSourceConfig.objects.create(
            source=TI, auto_demo_execution_enabled=True, total_lot_target=Decimal("1.20"),
            max_lot_per_leg=Decimal("0.40"), max_total_lot=Decimal("1.20"), daily_group_cap=cap)

    def _seed_closed(self, n):
        for i in range(n):
            appr = PendingSignalApproval.objects.create(
                source=TI, message_id=f"c{i}", symbol="XAUUSD", direction="BUY", stop_loss="4000",
                take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)
            SignalExecutionPlan.objects.create(
                approval=appr, account=self.demo, source=TI, message_id=f"c{i}", symbol="XAUUSD",
                direction="BUY", is_demo=True, status=SignalExecutionPlan.Status.CLOSED)

    def _fresh_approval(self):
        return PendingSignalApproval.objects.create(
            source=TI, message_id="new", symbol="XAUUSD", direction="BUY", entry="4005",
            stop_loss="4000", take_profit="4010", take_profits=["4010", "4020", "4030"],
            status=PendingSignalApproval.Status.APPROVED)

    def test_unlimited_cap_never_blocks_on_daily_limit(self):
        self._cfg(cap=0)          # 0 = unlimited
        self._seed_closed(40)     # 40 groups today — far past the old 24
        plan = planning.plan_demo_execution(self._fresh_approval(), account=self.demo)
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)  # not daily-rejected

    def test_finite_cap_still_blocks(self):
        self._cfg(cap=3)
        self._seed_closed(3)
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(self._fresh_approval(), account=self.demo)
        self.assertEqual(ctx.exception.code, "daily_limit_exceeded")
