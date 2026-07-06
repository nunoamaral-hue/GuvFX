"""E3-DEMO-PROMOTION — the real demo-order path, disabled by default.

Proves: default (SHADOW) behaviour is unchanged (PLACE_ORDER_SHADOW only, never PLACE_ORDER);
promote_plan_to_demo_jobs creates PLACE_ORDER ONLY under global mode DEMO (and refuses under
SHADOW); the shadow path refuses under DEMO; the auto-router routes to the demo path ONLY when
AUTO_DEMO is fully armed and is a no-op at defaults; and a closed demo trade links back to its
signal via the correlation comment tag. No real order is placed (the worker is never invoked).
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution import auto_router, close_monitor
from execution.models import (
    ExecutionControl,
    ExecutionJob,
    ProposedOrderLeg,
    SignalExecutionPlan,
    SignalSourceConfig,
    TradeOutcomeRecord,
)
from execution.signal_planning import plan_demo_execution
from execution.signal_promotion import (
    PromotionRejected,
    promote_plan_to_demo_jobs,
    promote_plan_to_shadow_jobs,
)
from signal_intake.models import (
    AcquiredMessage,
    ParserProfile,
    PendingSignalApproval,
    SignalProvider,
)
from strategies.models import Strategy, StrategyAssignment
from trading.models import Trade, TradingAccount

User = get_user_model()
Mode = ExecutionControl.SignalExecutionMode
JT = ExecutionJob.JobType
EXECUTABLE_NON_DEMO = {JT.OPEN_TRADE, JT.PLACE_TEST_ORDER}
WAYOND_TEXT = (
    "EURUSD | Potential upward movement\n\nEURUSD | BUY 1.0850\n\n"
    "❌ Stop Loss 1.0800 (50 pips)\n\n✅TP1 1.0900\n✅TP2 1.0950\n✅TP3 1.1000"
)


class E3Base(TestCase):
    def setUp(self):
        self.op = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        call_command("provision_auto_shadow")  # the guvfx-auto-system reviewer
        self.demo = TradingAccount.objects.create(
            user=self.op, name="Demo", account_number="D1", is_demo=True,
        )
        self.parser = ParserProfile.objects.create(
            slug="wayond_v1", certification_level=ParserProfile.CertificationLevel.MEDIUM,
        )
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="-100123",
            parser_profile=self.parser, status=SignalProvider.Status.ARMED,
        )
        self.source_cfg = SignalSourceConfig.objects.create(
            source="wayond", auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"),
        )
        self.strategy = Strategy.objects.create(owner=self.op, name="Wayond Auto")

    def _set_mode(self, mode):
        ctrl = ExecutionControl.get_solo()
        ctrl.signal_execution_mode = mode
        ctrl.auto_execution_enabled = True
        ctrl.kill_switch_engaged = False
        ctrl.save()

    def _assignment(self, execution_mode):
        return StrategyAssignment.objects.create(
            strategy=self.strategy, account=self.demo, is_active=True,
            stage=StrategyAssignment.STAGE_LIVE, execution_mode=execution_mode,
        )

    def _planned_plan(self, mid="p1"):
        approval = PendingSignalApproval.objects.create(
            source="wayond", message_id=mid, provider=self.provider, symbol="EURUSD",
            direction="BUY", entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900"], status=PendingSignalApproval.Status.APPROVED,
        )
        return plan_demo_execution(
            approval, account=self.demo, actor=self.op, signal_timestamp=timezone.now(),
        )


class PromotionTests(E3Base):
    def test_demo_promotion_creates_place_order_under_demo_mode(self):
        self._set_mode(Mode.DEMO)
        plan = self._planned_plan()
        jobs = promote_plan_to_demo_jobs(plan, actor=self.op)
        self.assertTrue(jobs)
        for j in jobs:
            self.assertEqual(j.job_type, JT.PLACE_ORDER)          # REAL order type
            self.assertEqual(j.payload.get("execution_mode"), "DEMO")
            self.assertEqual(j.payload.get("correlation_id"), plan.correlation_id)
            self.assertEqual(j.payload.get("comment"), f"WAY{plan.id}L{j.payload.get('leg_index')}")
            self.assertIsNotNone(j.payload.get("signal_timestamp"))
            self.assertIsNone(j.payload.get("entry_price"))       # market-only
        self.assertFalse(ExecutionJob.objects.filter(job_type=JT.PLACE_ORDER_SHADOW).exists())

    def test_demo_promotion_refused_under_shadow_mode(self):
        self._set_mode(Mode.SHADOW)  # default tier — DEMO promotion must refuse
        plan = self._planned_plan()
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_demo_jobs(plan, actor=self.op)
        self.assertEqual(cm.exception.code, "execution_mode_mismatch")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_shadow_promotion_still_shadow_only_and_refused_under_demo(self):
        # Shadow path under SHADOW mode → PLACE_ORDER_SHADOW only.
        self._set_mode(Mode.SHADOW)
        jobs = promote_plan_to_shadow_jobs(self._planned_plan("s1"), actor=self.op)
        self.assertTrue(all(j.job_type == JT.PLACE_ORDER_SHADOW for j in jobs))
        self.assertFalse(ExecutionJob.objects.filter(job_type=JT.PLACE_ORDER).exists())
        # And the shadow path refuses under DEMO mode (tier mismatch).
        self._set_mode(Mode.DEMO)
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_shadow_jobs(self._planned_plan("s2"), actor=self.op)
        self.assertEqual(cm.exception.code, "execution_mode_mismatch")

    def test_demo_promotion_inherits_demo_only_guard(self):
        # A non-demo account is refused even under DEMO mode (demo-only inherited).
        self._set_mode(Mode.DEMO)
        plan = self._planned_plan()
        plan.account.is_demo = False
        plan.account.save(update_fields=["is_demo"])
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_demo_jobs(plan, actor=self.op)
        self.assertEqual(cm.exception.code, "account_not_demo")
        self.assertEqual(ExecutionJob.objects.count(), 0)


class RouterTests(E3Base):
    def _feed(self, mid="w1"):
        return acquisition_acquire(self.provider, mid)

    def test_default_shadow_mode_never_creates_place_order(self):
        # Fully armed EXCEPT global mode stays SHADOW + assignment AUTO_SHADOW → shadow only.
        self._set_mode(Mode.SHADOW)
        self._assignment(StrategyAssignment.ExecutionMode.AUTO_SHADOW)
        acq = acquisition_acquire(self.provider, "w1")
        self.assertEqual(acq.outcome, AcquiredMessage.Outcome.INTAKEN)
        self.assertTrue(ExecutionJob.objects.filter(job_type=JT.PLACE_ORDER_SHADOW).exists())
        self.assertFalse(ExecutionJob.objects.filter(job_type=JT.PLACE_ORDER).exists())

    def test_armed_auto_demo_creates_place_order(self):
        self._set_mode(Mode.DEMO)
        self._assignment(StrategyAssignment.ExecutionMode.AUTO_DEMO)
        acq = acquisition_acquire(self.provider, "w2")
        approval = PendingSignalApproval.objects.get(pk=acq.approval_id)
        self.assertEqual(approval.status, PendingSignalApproval.Status.APPROVED)
        jobs = list(ExecutionJob.objects.all())
        self.assertTrue(jobs)
        for j in jobs:
            self.assertEqual(j.job_type, JT.PLACE_ORDER)      # real order — ONLY under AUTO_DEMO
        self.assertFalse(ExecutionJob.objects.filter(job_type=JT.PLACE_ORDER_SHADOW).exists())
        self.assertFalse(ExecutionJob.objects.filter(job_type__in=EXECUTABLE_NON_DEMO).exists())

    def test_demo_mode_with_shadow_assignment_is_manual(self):
        # DEMO global mode but only an AUTO_SHADOW assignment → no unique AUTO_DEMO target → MANUAL.
        self._set_mode(Mode.DEMO)
        self._assignment(StrategyAssignment.ExecutionMode.AUTO_SHADOW)
        acq = acquisition_acquire(self.provider, "w3")
        approval = PendingSignalApproval.objects.get(pk=acq.approval_id)
        self.assertEqual(approval.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_defaults_no_place_order(self):
        # No arming at all (default config) → no jobs of any kind.
        self._assignment(StrategyAssignment.ExecutionMode.AUTO_DEMO)  # armed assignment but...
        # ...ExecutionControl left at defaults (auto off, mode SHADOW).
        acq = acquisition_acquire(self.provider, "w4")
        approval = PendingSignalApproval.objects.get(pk=acq.approval_id)
        self.assertEqual(approval.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertEqual(ExecutionJob.objects.count(), 0)


class LinkageTests(E3Base):
    def test_closed_demo_trade_links_via_comment_tag(self):
        # A closed demo trade whose order comment carries WAY{plan.id}L{leg} links back to the
        # plan even with a blank Trade.correlation_id → the outcome candidate is not orphaned.
        self._set_mode(Mode.DEMO)
        plan = self._planned_plan()
        jobs = promote_plan_to_demo_jobs(plan, actor=self.op)
        job = jobs[0]
        trade = Trade.objects.create(
            account=self.demo, ticket="tk1", symbol="EURUSD", side="BUY",
            volume=Decimal("0.01"), open_time=timezone.now(), open_price=Decimal("1.0850"),
            close_time=timezone.now(), close_price=Decimal("1.0900"), profit=Decimal("12"),
            comment=job.payload["comment"], correlation_id="",  # worker left it blank
        )
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get(trade=trade)
        self.assertEqual(rec.outcome, TradeOutcomeRecord.Outcome.WIN)
        self.assertEqual(rec.correlation_id, plan.correlation_id)   # backfilled from the plan
        self.assertEqual(rec.signal_source, "wayond")
        self.assertIsNotNone(rec.execution_job)                     # linked to the demo order job


class DemoGateInheritanceTests(E3Base):
    """The DEMO promotion path inherits the FULL E2a gate matrix — not just the demo-only guard.

    Both paths share ``signal_promotion._validate``, so every gate that blocks a suppressed shadow
    job must equally block a real demo order. Under global mode DEMO, each rejection here proves a
    would-be real ``PLACE_ORDER`` is refused and creates zero jobs. This locks the safety property
    against a future edit that special-cased DEMO (e.g. an ``if mode==DEMO`` early return) skipping
    a gate — the shadow path already asserts these (tests_e2a_promotion); this is the DEMO mirror.
    """

    SRC = "wayond"

    def setUp(self):
        super().setUp()
        self._set_mode(Mode.DEMO)

    def _direct_plan(self, *, mid="g1", tps=("1.0900", "1.0950", "1.1000"),
                     lots=("0.01", "0.01", "0.01"), sl="1.0800", symbol="EURUSD",
                     signal_ts=None):
        """A PLANNED plan built directly on the demo account (bypassing plan_demo_execution so a
        bad-data state can be injected), matching E3Base's source so ``_validate`` reaches its gates.
        """
        approval = PendingSignalApproval.objects.create(
            source=self.SRC, message_id=mid, symbol=symbol, direction="BUY",
            stop_loss=sl, take_profits=list(tps), status=PendingSignalApproval.Status.APPROVED,
        )
        plan = SignalExecutionPlan.objects.create(
            approval=approval, account=self.demo, source=self.SRC, message_id=mid,
            symbol=symbol, direction="BUY", stop_loss=sl, is_demo=self.demo.is_demo,
            signal_timestamp=signal_ts or timezone.now(),
            status=SignalExecutionPlan.Status.PLANNED,
        )
        for i, (tp, lot) in enumerate(zip(tps, lots), start=1):
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit=tp, stop_loss=sl,
                lot_size=Decimal(lot), status=ProposedOrderLeg.Status.PLANNED,
            )
        return plan

    def _assert_demo_rejected(self, plan, code):
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_demo_jobs(plan, actor=self.op)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(ExecutionJob.objects.count(), 0)  # no real order job created

    def test_demo_kill_switch_blocks(self):
        from execution import signal_proposals as bridge
        bridge.engage_kill_switch(actor=self.op)
        self._assert_demo_rejected(self._direct_plan(mid="k1"), "kill_switch_engaged")

    def test_demo_disabled_source_blocks(self):
        SignalSourceConfig.objects.filter(source=self.SRC).update(auto_demo_execution_enabled=False)
        self._assert_demo_rejected(self._direct_plan(mid="k2"), "source_not_enabled")

    def test_demo_stale_signal_blocks(self):
        plan = self._direct_plan(mid="k3", signal_ts=timezone.now() - timedelta(seconds=600))
        self._assert_demo_rejected(plan, "stale_signal")

    def test_demo_missing_stop_loss_blocks(self):
        self._assert_demo_rejected(self._direct_plan(mid="k4", sl=""), "missing_stop_loss")

    def test_demo_missing_take_profit_blocks(self):
        self._assert_demo_rejected(
            self._direct_plan(mid="k5", tps=("",), lots=("0.01",)), "missing_take_profit")

    def test_demo_symbol_not_allowed_blocks(self):
        self._assert_demo_rejected(self._direct_plan(mid="k6", symbol="GBPJPY"), "symbol_not_allowed")

    def test_demo_lot_over_cap_blocks(self):
        self._assert_demo_rejected(
            self._direct_plan(mid="k7", tps=("1.0900",), lots=("0.05",)), "lot_out_of_range")

    def test_demo_total_lot_over_cap_blocks(self):
        # Each leg is within the 0.02 per-leg cap, but the four-leg sum (0.08) exceeds the 0.06 total.
        self._assert_demo_rejected(
            self._direct_plan(
                mid="k8", tps=("1.0900", "1.0950", "1.1000", "1.1050"),
                lots=("0.02", "0.02", "0.02", "0.02")),
            "total_lot_exceeds_cap")


def acquisition_acquire(provider, mid):
    """Feed a real Wayond message through acquire_message (fires the auto-router)."""
    from signal_intake import acquisition
    return acquisition.acquire_message(provider, {
        "message_id": mid, "chat_id": "-100123", "date": timezone.now(), "text": WAYOND_TEXT,
    })
