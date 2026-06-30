"""
EXEC-E1b tests — non-executable multi-leg demo PLAN.

Central claim: building a plan NEVER creates an ExecutionJob, places no order, and
is structurally invisible to the worker claim path. Plus: 3 TPs → 3 legs (shared
SL, distinct TP); deterministic capped volume split; missing SL/TP held safely;
stale signal voided; disabled source / unknown symbol blocked; idempotent dedup;
per-group caps; and a static no-order guard.
"""

import ast
import importlib
import pathlib
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from execution import signal_planning as planning
from execution.models import (
    PLAN_MAX_CONCURRENT_GROUPS,
    PLAN_MAX_GROUPS_PER_DAY,
    SIGNAL_MAX_LOT_SIZE,
    ExecutionJob,
    PlanAuditEvent,
    ProposedOrderLeg,
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


class VolumeSplitTests(TestCase):
    def test_three_legs_equal(self):
        legs, meta = planning.split_volume(Decimal("0.03"), 3)
        self.assertEqual(legs, [Decimal("0.01"), Decimal("0.01"), Decimal("0.01")])

    def test_two_legs_remainder_to_leg1(self):
        legs, _ = planning.split_volume(Decimal("0.03"), 2)
        self.assertEqual(legs, [Decimal("0.02"), Decimal("0.01")])  # remainder → leg 1

    def test_single_leg_capped_to_per_leg_max(self):
        legs, meta = planning.split_volume(Decimal("0.03"), 1)
        self.assertEqual(legs, [Decimal(str(SIGNAL_MAX_LOT_SIZE))])  # 0.02 cap
        self.assertTrue(meta["capped"])

    def test_each_leg_within_cap(self):
        legs, _ = planning.split_volume(Decimal("0.06"), 3)
        self.assertEqual(legs, [Decimal("0.02"), Decimal("0.02"), Decimal("0.02")])

    def test_insufficient_total_raises(self):
        with self.assertRaises(planning.VolumeSplitError):
            planning.split_volume(Decimal("0.01"), 3)  # 1 unit cannot fill 3 legs


class PlanBuilderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="op", email="op@example.invalid", password="x"
        )
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True
        )
        SignalSourceConfig.objects.create(
            source=SRC, auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03")
        )

    # ----- the central no-order guarantees -------------------------------

    def test_three_tps_make_one_plan_three_legs_no_job(self):
        before = ExecutionJob.objects.count()
        plan = planning.plan_demo_execution(_approved("m1"), account=self.demo, actor=self.user)
        self.assertIsInstance(plan, SignalExecutionPlan)
        self.assertNotIsInstance(plan, ExecutionJob)
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)
        legs = list(plan.legs.order_by("leg_index"))
        self.assertEqual(len(legs), 3)
        self.assertEqual(ExecutionJob.objects.count(), before)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_legs_share_sl_and_have_distinct_tps(self):
        plan = planning.plan_demo_execution(
            _approved("m2", tps=("1.0900", "1.0950", "1.1000"), sl="1.0800"),
            account=self.demo,
        )
        legs = list(plan.legs.order_by("leg_index"))
        self.assertEqual([l.take_profit for l in legs], ["1.0900", "1.0950", "1.1000"])
        self.assertTrue(all(l.stop_loss == "1.0800" for l in legs))
        self.assertTrue(all(l.order_type == "MARKET" for l in legs))

    def test_volume_split_deterministic_and_capped(self):
        plan = planning.plan_demo_execution(_approved("m3"), account=self.demo)
        lots = [l.lot_size for l in plan.legs.order_by("leg_index")]
        self.assertEqual(lots, [Decimal("0.01"), Decimal("0.01"), Decimal("0.01")])
        self.assertEqual(plan.total_lot, Decimal("0.03"))
        self.assertTrue(all(l <= Decimal(str(SIGNAL_MAX_LOT_SIZE)) for l in lots))

    def test_two_tps_make_two_legs(self):
        plan = planning.plan_demo_execution(
            _approved("m4", tps=("1.0900", "1.0950")), account=self.demo
        )
        self.assertEqual(plan.legs.count(), 2)

    def test_worker_claim_path_cannot_see_plans_or_legs(self):
        planning.plan_demo_execution(_approved("m5"), account=self.demo)
        self.assertEqual(
            ExecutionJob.objects.filter(status=ExecutionJob.Status.PENDING).count(), 0
        )
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(SignalExecutionPlan.objects.count(), 1)
        self.assertEqual(ProposedOrderLeg.objects.count(), 3)

    def test_plan_status_choices_have_no_claimable_state(self):
        statuses = {s for s, _ in SignalExecutionPlan.Status.choices}
        self.assertFalse(statuses & {"PENDING", "RUNNING"})

    # ----- hold / void safely --------------------------------------------

    def test_missing_sl_is_held_with_no_legs(self):
        plan = planning.plan_demo_execution(
            _approved("h1", sl=""), account=self.demo
        )
        self.assertEqual(plan.status, SignalExecutionPlan.Status.HELD)
        self.assertEqual(plan.hold_reason, "missing_stop_loss")
        self.assertEqual(plan.legs.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_missing_tp_is_held_with_no_legs(self):
        plan = planning.plan_demo_execution(
            _approved("h2", tps=()), account=self.demo
        )
        self.assertEqual(plan.status, SignalExecutionPlan.Status.HELD)
        self.assertEqual(plan.hold_reason, "missing_take_profit")
        self.assertEqual(plan.legs.count(), 0)

    def test_stale_signal_is_voided_with_no_legs(self):
        future = timezone.now() + timedelta(seconds=300)
        plan = planning.plan_demo_execution(
            _approved("h3"), account=self.demo, now=future
        )
        self.assertEqual(plan.status, SignalExecutionPlan.Status.VOIDED)
        self.assertEqual(plan.hold_reason, "stale_signal")
        self.assertEqual(plan.legs.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    # ----- policy / config rejections ------------------------------------

    def test_disabled_source_blocks_planning(self):
        SignalSourceConfig.objects.filter(source=SRC).update(auto_demo_execution_enabled=False)
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(_approved("r1"), account=self.demo)
        self.assertEqual(ctx.exception.code, "source_not_enabled")
        self.assertEqual(SignalExecutionPlan.objects.count(), 0)

    def test_unknown_symbol_blocks_planning(self):
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(
                _approved("r2", symbol="BTCUSD"), account=self.demo
            )
        self.assertEqual(ctx.exception.code, "symbol_not_allowed")

    def test_live_account_blocked(self):
        live = TradingAccount.objects.create(
            user=self.user, name="Live", account_number="L1", is_demo=False
        )
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(_approved("r3"), account=live)
        self.assertEqual(ctx.exception.code, "account_not_demo")

    def test_kill_switch_blocks_planning(self):
        from execution import signal_proposals as bridge
        bridge.engage_kill_switch(actor=self.user, reason="test")
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(_approved("r4"), account=self.demo)
        self.assertEqual(ctx.exception.code, "kill_switch_engaged")

    def test_unapproved_blocked(self):
        pending = PendingSignalApproval.objects.create(
            source=SRC, message_id="r5", symbol="EURUSD", direction="BUY",
            stop_loss="1.0800", take_profits=["1.0900"],
            status=PendingSignalApproval.Status.PENDING_APPROVAL,
        )
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(pending, account=self.demo)
        self.assertEqual(ctx.exception.code, "approval_not_approved")

    # ----- idempotency + caps --------------------------------------------

    def test_duplicate_is_idempotent(self):
        a = _approved("dup1")
        p1 = planning.plan_demo_execution(a, account=self.demo)
        p2 = planning.plan_demo_execution(a, account=self.demo)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(SignalExecutionPlan.objects.filter(approval=a).count(), 1)

    def test_concurrent_group_cap_blocks(self):
        self.assertEqual(PLAN_MAX_CONCURRENT_GROUPS, 1)
        planning.plan_demo_execution(_approved("c1"), account=self.demo)  # 1 PLANNED
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(_approved("c2"), account=self.demo)
        self.assertEqual(ctx.exception.code, "concurrent_limit_exceeded")

    def test_daily_group_cap_blocks(self):
        # Pre-seed the daily PLANNED-group budget for EURUSD via ORM.
        for i in range(PLAN_MAX_GROUPS_PER_DAY):
            SignalExecutionPlan.objects.create(
                approval=_approved(f"d{i}"), account=self.demo, source=SRC,
                message_id=f"d{i}", symbol="EURUSD", direction="BUY", is_demo=True,
                status=SignalExecutionPlan.Status.PLANNED,
            )
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(_approved("dN"), account=self.demo)
        self.assertEqual(ctx.exception.code, "daily_limit_exceeded")

    # ----- audit ----------------------------------------------------------

    def test_audit_chain_created(self):
        a = _approved("au1")
        plan = planning.plan_demo_execution(a, account=self.demo, actor=self.user)
        self.assertTrue(PlanAuditEvent.objects.filter(
            event=PlanAuditEvent.Event.PLAN_CREATED, plan=plan, approval=a).exists())
        self.assertEqual(PlanAuditEvent.objects.filter(
            event=PlanAuditEvent.Event.LEG_CREATED, plan=plan).count(), 3)

    def test_rejection_audit_persists(self):
        before = PlanAuditEvent.objects.count()
        with self.assertRaises(planning.PlanRejected):
            planning.plan_demo_execution(_approved("au2", symbol="BTCUSD"), account=self.demo)
        self.assertGreater(PlanAuditEvent.objects.count(), before)


class ManagementCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="op2", email="op2@example.invalid", password="x"
        )
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D2", is_demo=True
        )

    def test_command_creates_plan_and_no_jobs(self):
        call_command("plan_demo_execution", "--enable-source", SRC, "--total-lot", "0.03")
        a = _approved("cmd1")
        from io import StringIO
        out = StringIO()
        call_command("plan_demo_execution", "--approval", str(a.id),
                     "--account", str(self.demo.id), stdout=out, stderr=StringIO())
        output = out.getvalue()
        self.assertIn("0 orders placed", output)
        self.assertIn("0 ExecutionJobs created", output)
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(SignalExecutionPlan.objects.count(), 1)
        self.assertEqual(ProposedOrderLeg.objects.count(), 3)


class NoOrderStaticGuardTests(TestCase):
    """Static proof the planner cannot create an order, job, or make a call.

    Uses AST identifier extraction (definitive, ignores docstrings/comments —
    the module docstring legitimately mentions ``ExecutionJob.objects`` in prose).
    """

    @staticmethod
    def _code_names(src):
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for n in node.names:
                    names.add(n.asname or n.name)
            elif isinstance(node, ast.Import):
                for n in node.names:
                    names.add((n.asname or n.name).split(".")[0])
        return names

    def _src(self, module):
        return pathlib.Path(importlib.import_module(module).__file__).read_text()

    def test_planning_module_makes_no_order_or_network_call(self):
        src = self._src("execution.signal_planning")
        names = self._code_names(src)
        for forbidden in ("ExecutionJob", "create_place_order_job", "create_open_trade_job",
                          "order_send", "requests", "httpx", "urllib", "MetaTrader5"):
            self.assertNotIn(forbidden, names, f"planner references {forbidden}")

    def test_e1b_models_are_not_execution_jobs(self):
        from django.apps import apps
        names = {m.__name__ for m in apps.get_app_config("execution").get_models()}
        self.assertIn("SignalExecutionPlan", names)
        self.assertIn("ProposedOrderLeg", names)
        # The plan/leg models are distinct from ExecutionJob.
        self.assertFalse(issubclass(SignalExecutionPlan, ExecutionJob))
        self.assertFalse(issubclass(ProposedOrderLeg, ExecutionJob))
