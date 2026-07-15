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
from unittest import mock

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
# A second, distinct provider-slug source (mirrors prod's "wayond" vs "ti_signals")
# used by the per-source daily-cap isolation tests.
SRC_B = "ti_signals"


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
        # Demo account has no synced broker instruments -> default baseline; BTCUSD is not in it,
        # so the broker-symbol registry rejects it fail-closed.
        with self.assertRaises(planning.PlanRejected) as ctx:
            planning.plan_demo_execution(
                _approved("r2", symbol="BTCUSD"), account=self.demo
            )
        self.assertEqual(ctx.exception.code, "SYMBOL_NOT_AVAILABLE_ON_BROKER")

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
        # deployed default raised to 10 for overlapping signals; pin to 1 to verify the gate.
        planning.plan_demo_execution(_approved("c1"), account=self.demo)  # 1 PLANNED
        with mock.patch.object(planning, "PLAN_MAX_CONCURRENT_GROUPS", 1):
            with self.assertRaises(planning.PlanRejected) as ctx:
                planning.plan_demo_execution(_approved("c2"), account=self.demo)
        self.assertEqual(ctx.exception.code, "concurrent_limit_exceeded")

    def test_daily_group_cap_blocks(self):
        # Deployed default raised to 24/source; pin to 3 to verify the gate cheaply.
        # Pre-seed the daily group budget for EURUSD (same source) via ORM.
        for i in range(3):
            SignalExecutionPlan.objects.create(
                approval=_approved(f"d{i}"), account=self.demo, source=SRC,
                message_id=f"d{i}", symbol="EURUSD", direction="BUY", is_demo=True,
                status=SignalExecutionPlan.Status.PLANNED,
            )
        SignalSourceConfig.objects.update(daily_group_cap=3)
        with mock.patch.object(planning, "PLAN_MAX_GROUPS_PER_DAY", 3):
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


class DailyCapPerSourceTests(TestCase):
    """The daily group cap (``PLAN_MAX_GROUPS_PER_DAY``) is per-SOURCE.

    Each provider (e.g. wayond, ti_signals) gets an independent daily budget; the
    counter counts acted-on groups (PLANNED/PROMOTED/CLOSED) across the whole day —
    not just the momentary PLANNED backlog — resets at the calendar rollover, and
    never disturbs the concurrency/exposure caps or any open position. No order is
    ever placed by planning.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="dc", email="dc@example.invalid", password="x"
        )
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="DC1", is_demo=True
        )
        for src in (SRC, SRC_B):
            SignalSourceConfig.objects.create(
                source=src, auto_demo_execution_enabled=True,
                total_lot_target=Decimal("0.03"),
            )

    def _approval(self, mid, *, source, symbol="EURUSD"):
        return PendingSignalApproval.objects.create(
            source=source, message_id=mid, symbol=symbol, direction="BUY",
            entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900", "1.0950", "1.1000"],
            status=PendingSignalApproval.Status.APPROVED,
        )

    def _seed(self, n, *, source, symbol="EURUSD",
              status=SignalExecutionPlan.Status.PLANNED, days_ago=0):
        """Create n plans (each with its own approval) for (source, symbol) in
        ``status``, optionally backdated ``days_ago`` calendar days."""
        for i in range(n):
            mid = f"{source}-{status}-{i}-{days_ago}"
            plan = SignalExecutionPlan.objects.create(
                approval=self._approval(mid, source=source, symbol=symbol),
                account=self.demo, source=source, message_id=mid,
                symbol=symbol, direction="BUY", is_demo=True, status=status,
            )
            if days_ago:
                SignalExecutionPlan.objects.filter(pk=plan.pk).update(
                    created_at=timezone.now() - timedelta(days=days_ago)
                )

    # 1 — per-source isolation: one source at its cap never blocks another.
    def test_daily_cap_is_per_source_isolated(self):
        SignalSourceConfig.objects.update(daily_group_cap=3)
        with mock.patch.object(planning, "PLAN_MAX_GROUPS_PER_DAY", 3):
            self._seed(3, source=SRC)  # source A at its cap
            with self.assertRaises(planning.PlanRejected) as ctx:
                planning.plan_demo_execution(
                    self._approval("a-over", source=SRC), account=self.demo)
            self.assertEqual(ctx.exception.code, "daily_limit_exceeded")
            # Source B is untouched by A's exhausted budget → still plans.
            plan_b = planning.plan_demo_execution(
                self._approval("b-ok", source=SRC_B), account=self.demo)
            self.assertEqual(plan_b.status, SignalExecutionPlan.Status.PLANNED)
            self.assertEqual(plan_b.source, SRC_B)

    # 2 — acceptance up to the limit: the cap-th group of the day still plans.
    def test_daily_cap_accepts_up_to_limit(self):
        SignalSourceConfig.objects.update(daily_group_cap=4)
        with mock.patch.object(planning, "PLAN_MAX_GROUPS_PER_DAY", 4):
            self._seed(3, source=SRC)  # 3 of 4 used
            plan = planning.plan_demo_execution(
                self._approval("fourth", source=SRC), account=self.demo)
            self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)

    # 3 — fail-closed past the cap, counting ACTED-ON groups (PROMOTED/CLOSED),
    #     and excluding non-acted plans (VOIDED/HELD).
    def test_daily_cap_rejects_past_limit_counting_acted_on_groups(self):
        SignalSourceConfig.objects.update(daily_group_cap=3)
        with mock.patch.object(planning, "PLAN_MAX_GROUPS_PER_DAY", 3):
            # Budget consumed by groups that already promoted/closed today (NOT
            # currently PLANNED). The old PLANNED-only counter would see 0 and wrongly
            # admit; the per-source acted-on counter blocks.
            self._seed(2, source=SRC, status=SignalExecutionPlan.Status.PROMOTED)
            self._seed(1, source=SRC, status=SignalExecutionPlan.Status.CLOSED)
            # VOIDED/HELD do NOT consume budget (no order was acted on).
            self._seed(3, source=SRC, status=SignalExecutionPlan.Status.VOIDED)
            self._seed(3, source=SRC, status=SignalExecutionPlan.Status.HELD)
            with self.assertRaises(planning.PlanRejected) as ctx:
                planning.plan_demo_execution(
                    self._approval("over", source=SRC), account=self.demo)
            self.assertEqual(ctx.exception.code, "daily_limit_exceeded")

    # 4 — the concurrency cap is still enforced independently of the daily cap.
    def test_concurrent_group_cap_enforced_independently(self):
        planning.plan_demo_execution(
            self._approval("cc1", source=SRC), account=self.demo)  # 1 PLANNED
        SignalSourceConfig.objects.update(daily_group_cap=10000)
        with mock.patch.object(planning, "PLAN_MAX_GROUPS_PER_DAY", 10_000), \
                mock.patch.object(planning, "PLAN_MAX_CONCURRENT_GROUPS", 1):
            with self.assertRaises(planning.PlanRejected) as ctx:
                planning.plan_demo_execution(
                    self._approval("cc2", source=SRC), account=self.demo)
        self.assertEqual(ctx.exception.code, "concurrent_limit_exceeded")

    # 5 — the deployed risk caps are the documented values. Exposure is 2.40 (raised by the
    #     source-scoped 0.40/leg sizing packet, ADR-0012, to admit one 1.20-lot TI signal +
    #     overlap); the concurrency / open-position caps are unchanged.
    def test_other_risk_caps_unchanged(self):
        from execution import risk_controls
        from execution import models as ex_models
        self.assertEqual(risk_controls.MAX_ACCOUNT_EXPOSURE_LOT, Decimal("2.40"))
        self.assertEqual(risk_controls.MAX_SYMBOL_EXPOSURE_LOT, Decimal("2.40"))
        self.assertEqual(risk_controls.MAX_OPEN_POSITIONS_PER_ACCOUNT, 20)
        self.assertEqual(ex_models.PLAN_MAX_CONCURRENT_GROUPS, 10)
        self.assertEqual(ex_models.SIGNAL_MAX_CONCURRENT_POSITIONS, 20)

    # 6 — the daily budget resets at the calendar rollover.
    def test_daily_cap_resets_on_calendar_rollover(self):
        SignalSourceConfig.objects.update(daily_group_cap=3)
        with mock.patch.object(planning, "PLAN_MAX_GROUPS_PER_DAY", 3):
            self._seed(3, source=SRC, days_ago=1)  # yesterday's budget (backdated)
            plan = planning.plan_demo_execution(
                self._approval("today", source=SRC), account=self.demo)
            self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)

    # 7 — both sources stay live (each reaches its own cap), and planning never
    #     creates or alters an open position (no Trade touched).
    def test_both_sources_live_and_no_open_position_altered(self):
        from trading.models import Trade
        before = Trade.objects.count()
        SignalSourceConfig.objects.update(daily_group_cap=2)
        with mock.patch.object(planning, "PLAN_MAX_GROUPS_PER_DAY", 2):
            for src in (SRC, SRC_B):
                self._seed(2, source=src)  # each source at its own cap
                with self.assertRaises(planning.PlanRejected) as ctx:
                    planning.plan_demo_execution(
                        self._approval(f"{src}-over", source=src), account=self.demo)
                self.assertEqual(ctx.exception.code, "daily_limit_exceeded")
        # Planning (and its rejections) created/mutated NO Trade — open positions untouched.
        self.assertEqual(Trade.objects.count(), before)


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
