"""GFX-BETA-P0-A — customer-owned per-leg lot ACTIVATION (Option B).

Wires the (previously inert) ``AssignmentLegSizing.lot_per_leg`` override into the live planning split.
A customer with an override row sizes every leg at their lot (source-capped); an assignment with NO row
keeps the EXACT source-global sizing (support@ / Customer-Zero byte-identical). No order is placed:
planning is no-order and promotion creates SUPPRESSED shadow jobs (no bridge, no order_send).

Companion to ``strategies/tests_leg_sizing.py`` (which proves the model's min/step/max ``validate_lot``
and the owner-scoped GET/PUT + IDOR of the config API). This file proves the EXECUTION wiring.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import signal_planning as planning
from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan, SignalSourceConfig
from signal_intake.models import PendingSignalApproval
from strategies.models import (AssignmentLegSizing, Strategy, StrategyAssignment,
                               effective_lot_per_leg)
from trading.models import TradingAccount

User = get_user_model()
TI = "ti_signals"


class _Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True, broker_name="DemoBroker")
        # ti_signals owns the operator 0.40/leg (1.20 total) policy (the support@ effective sizing).
        SignalSourceConfig.objects.create(
            source=TI, auto_demo_execution_enabled=True, total_lot_target=Decimal("1.20"),
            max_lot_per_leg=Decimal("0.40"), max_total_lot=Decimal("1.20"))
        self.strategy = Strategy.objects.create(owner=self.user, name="Wayond WIM")
        self.asn = StrategyAssignment.objects.create(
            strategy=self.strategy, account=self.demo, is_active=True,
            stage=StrategyAssignment.STAGE_LIVE, execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
            signal_source=TI)

    def _approval(self, mid, *, tps=("4010", "4020", "4030")):
        return PendingSignalApproval.objects.create(
            source=TI, message_id=mid, symbol="XAUUSD", direction="BUY",
            entry="4000", stop_loss="3990", take_profit=tps[0],
            take_profits=list(tps), status=PendingSignalApproval.Status.APPROVED)

    def _plan(self, mid, **kw):
        return planning.plan_demo_execution(self._approval(mid), account=self.demo, **kw)

    def _lots(self, plan):
        return [l.lot_size for l in plan.legs.order_by("leg_index")]


class NoOverrideUnchanged(_Base):
    """Items 2, 20 — an assignment with NO override row keeps the exact source-global sizing."""

    def test_no_row_three_leg_is_source_global_040(self):
        plan = self._plan("n1", assignment=self.asn)
        self.assertEqual(self._lots(plan), [Decimal("0.40")] * 3)
        self.assertEqual(plan.total_lot, Decimal("1.20"))
        self.assertEqual(ExecutionJob.objects.count(), 0)  # no order

    def test_no_assignment_arg_is_source_global(self):
        # Callers that pass no assignment (legacy) are byte-identical to before.
        plan = self._plan("n2")
        self.assertEqual(self._lots(plan), [Decimal("0.40")] * 3)

    def test_effective_resolver_no_row_returns_source_cap(self):
        self.assertEqual(effective_lot_per_leg(self.asn), Decimal("0.40"))  # support@ preservation


class OverrideConsumed(_Base):
    """Items 12, 13, 15 — a customer override row sizes every leg at the customer's lot."""

    def _with_lot(self, lot):
        AssignmentLegSizing.objects.create(assignment=self.asn, lot_per_leg=Decimal(lot))

    def test_override_001_three_leg_gives_001_per_leg(self):
        self._with_lot("0.01")
        plan = self._plan("o1", assignment=self.asn)
        self.assertEqual(self._lots(plan), [Decimal("0.01")] * 3)   # 13 — per-leg semantics
        self.assertEqual(plan.total_lot, Decimal("0.03"))           # 12 — customer value consumed

    def test_override_005_gives_005_per_leg(self):
        self._with_lot("0.05")
        self.assertEqual(self._lots(self._plan("o2", assignment=self.asn)), [Decimal("0.05")] * 3)

    def test_provider_global_120_cannot_override_customer(self):
        # 15 — with a 0.01 row the plan is 0.01/leg, NOT the provider-global 0.40/1.20.
        self._with_lot("0.01")
        plan = self._plan("o3", assignment=self.asn)
        self.assertNotIn(Decimal("0.40"), self._lots(plan))
        self.assertLess(plan.total_lot, Decimal("1.20"))

    def test_override_clamped_to_source_cap(self):
        # A customer value ABOVE the operator source cap can only REDUCE to the cap — never exceed it.
        self._with_lot("5.00")
        self.assertEqual(self._lots(self._plan("o4", assignment=self.asn)), [Decimal("0.40")] * 3)


class LegCountSemantics(_Base):
    """Item 6 — per-leg lot holds across 1 / 2 / 3 TP legs (total scales, per-leg constant)."""

    def _with_lot(self, lot):
        AssignmentLegSizing.objects.create(assignment=self.asn, lot_per_leg=Decimal(lot))

    def test_one_leg(self):
        self._with_lot("0.01")
        plan = planning.plan_demo_execution(
            self._approval("l1", tps=("4010",)), account=self.demo, assignment=self.asn)
        self.assertEqual(self._lots(plan), [Decimal("0.01")])
        self.assertEqual(plan.total_lot, Decimal("0.01"))

    def test_two_leg(self):
        self._with_lot("0.01")
        plan = planning.plan_demo_execution(
            self._approval("l2", tps=("4010", "4020")), account=self.demo, assignment=self.asn)
        self.assertEqual(self._lots(plan), [Decimal("0.01")] * 2)
        self.assertEqual(plan.total_lot, Decimal("0.02"))


class PromotePayloadMatches(_Base):
    """Item 14 — the promoted ExecutionJob payload lots equal the planned per-leg lots."""

    def setUp(self):
        super().setUp()
        from execution.models import ExecutionControl
        ctrl = ExecutionControl.get_solo()
        ctrl.signal_execution_mode = ExecutionControl.SignalExecutionMode.DEMO
        ctrl.save()

    def test_job_payload_lots_match_planned(self):
        AssignmentLegSizing.objects.create(assignment=self.asn, lot_per_leg=Decimal("0.01"))
        plan = self._plan("pp1", assignment=self.asn)
        from execution.signal_promotion import promote_plan_to_demo_jobs
        promote_plan_to_demo_jobs(plan, actor=self.user)
        jobs = ExecutionJob.objects.filter(job_type="PLACE_ORDER").order_by("id")
        self.assertTrue(jobs.exists())
        for j in jobs:
            self.assertEqual(Decimal(str((j.payload or {}).get("lots"))), Decimal("0.01"))


class FailClosed(_Base):
    """Item 11 — a fail-closed resolution never sizes larger than the source-global default."""

    def test_helper_returns_none_on_error(self):
        # A broken assignment object (attribute access raises) → None → source-global sizing, never larger.
        cfg = SignalSourceConfig.objects.get(source=TI)

        class _Boom:
            @property
            def leg_sizing(self):
                raise RuntimeError("boom")
        self.assertIsNone(planning._customer_leg_size_override(_Boom(), cfg))

    def test_none_assignment_is_none(self):
        cfg = SignalSourceConfig.objects.get(source=TI)
        self.assertIsNone(planning._customer_leg_size_override(None, cfg))
