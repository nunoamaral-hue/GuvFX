"""
EXEC-E2a tests — plan → suppressed, un-claimable shadow jobs.

Central claims: promotion creates ONLY PLACE_ORDER_SHADOW jobs (never an
executable PLACE_ORDER), each leg links to exactly one shadow job, re-promotion
is idempotent, the shadow payload is complete + SHADOW-flagged, the next_job
endpoint guard refuses shadow jobs to ordinary workers (and serves them only to
a shadow_worker), normal claim is unchanged, the gates reject correctly, and the
promotion code makes no MT5/agent/network/order call.
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
from rest_framework.test import APIClient

from execution import signal_promotion as promo
from execution.models import (
    ExecutionControl,
    ExecutionJob,
    ProposedOrderLeg,
    PromotionAuditEvent,
    SignalExecutionPlan,
    SignalSourceConfig,
    WorkerIdentity,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()
SRC = PendingSignalApproval.Source.WAYOND_TELEGRAM
NEXT_URL = "/api/execution/jobs/next/"


def _planned_plan(user, account, *, mid="p1", tps=("1.0900", "1.0950", "1.1000"),
                  lots=("0.01", "0.01", "0.01"), sl="1.0800", symbol="EURUSD",
                  direction="BUY", signal_ts=None):
    approval = PendingSignalApproval.objects.create(
        source=SRC, message_id=mid, symbol=symbol, direction=direction,
        stop_loss=sl, take_profits=list(tps),
        status=PendingSignalApproval.Status.APPROVED,
    )
    plan = SignalExecutionPlan.objects.create(
        approval=approval, account=account, source=SRC, message_id=mid,
        symbol=symbol, direction=direction, stop_loss=sl, is_demo=account.is_demo,
        signal_timestamp=signal_ts or timezone.now(),
        status=SignalExecutionPlan.Status.PLANNED,
    )
    for i, (tp, lot) in enumerate(zip(tps, lots), start=1):
        ProposedOrderLeg.objects.create(
            plan=plan, leg_index=i, take_profit=tp, stop_loss=sl,
            lot_size=Decimal(lot), status=ProposedOrderLeg.Status.PLANNED,
        )
    return plan


class PromotionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True
        )
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)

    def _executable_count(self):
        return ExecutionJob.objects.exclude(
            job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW
        ).count()

    # ----- core no-order guarantees --------------------------------------

    def test_three_legs_make_three_shadow_jobs_no_executable(self):
        before = self._executable_count()
        plan = _planned_plan(self.user, self.demo, mid="m1")
        jobs = promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(j.job_type == ExecutionJob.JobType.PLACE_ORDER_SHADOW for j in jobs))
        self.assertEqual(
            ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW).count(), 3
        )
        # No executable job (PLACE_ORDER/OPEN_TRADE/PLACE_TEST_ORDER) was created.
        self.assertEqual(self._executable_count(), before)
        self.assertEqual(self._executable_count(), 0)

    def test_each_leg_links_to_exactly_one_shadow_job(self):
        plan = _planned_plan(self.user, self.demo, mid="m2")
        promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        plan.refresh_from_db()
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PROMOTED)
        legs = list(plan.legs.order_by("leg_index"))
        self.assertEqual(len(legs), 3)
        job_ids = set()
        for leg in legs:
            self.assertIsNotNone(leg.execution_job_id)
            self.assertEqual(leg.status, ProposedOrderLeg.Status.PROMOTED)
            job_ids.add(leg.execution_job_id)
        self.assertEqual(len(job_ids), 3)  # distinct jobs

    def test_shadow_payload_is_complete_and_flagged(self):
        plan = _planned_plan(self.user, self.demo, mid="m3", tps=("1.0900", "1.0950"),
                             lots=("0.02", "0.01"))
        jobs = promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        p = jobs[0].payload
        self.assertEqual(p["execution_mode"], "SHADOW")
        self.assertEqual(p["symbol"], "EURUSD")
        self.assertEqual(p["side"], "BUY")
        self.assertEqual(p["lots"], "0.02")
        self.assertEqual(p["sl_price"], "1.0800")
        self.assertEqual(p["tp_price"], "1.0900")
        self.assertTrue(p["is_demo"])
        self.assertIsNone(p["entry_price"])  # market
        self.assertIn("WAY", p["comment"])

    def test_repromotion_is_idempotent(self):
        plan = _planned_plan(self.user, self.demo, mid="m4")
        j1 = promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        plan.refresh_from_db()
        j2 = promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual({j.id for j in j1}, {j.id for j in j2})
        self.assertEqual(
            ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW).count(), 3
        )

    # ----- gate rejections ------------------------------------------------

    def test_kill_switch_blocks_promotion(self):
        from execution import signal_proposals as bridge
        bridge.engage_kill_switch(actor=self.user)
        plan = _planned_plan(self.user, self.demo, mid="m5")
        with self.assertRaises(promo.PromotionRejected) as ctx:
            promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(ctx.exception.code, "kill_switch_engaged")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_disabled_source_blocks_promotion(self):
        SignalSourceConfig.objects.filter(source=SRC).update(auto_demo_execution_enabled=False)
        plan = _planned_plan(self.user, self.demo, mid="m6")
        with self.assertRaises(promo.PromotionRejected) as ctx:
            promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(ctx.exception.code, "source_not_enabled")

    def test_stale_plan_blocks_promotion(self):
        plan = _planned_plan(self.user, self.demo, mid="m7",
                             signal_ts=timezone.now() - timedelta(seconds=600))
        with self.assertRaises(promo.PromotionRejected) as ctx:
            promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(ctx.exception.code, "stale_signal")

    def test_non_planned_plan_blocks_promotion(self):
        plan = _planned_plan(self.user, self.demo, mid="m8")
        SignalExecutionPlan.objects.filter(id=plan.id).update(status=SignalExecutionPlan.Status.HELD)
        plan.refresh_from_db()
        with self.assertRaises(promo.PromotionRejected) as ctx:
            promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(ctx.exception.code, "plan_not_planned")

    def test_live_account_blocks_promotion(self):
        live = TradingAccount.objects.create(
            user=self.user, name="Live", account_number="L1", is_demo=False
        )
        plan = _planned_plan(self.user, live, mid="m9")
        with self.assertRaises(promo.PromotionRejected) as ctx:
            promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(ctx.exception.code, "account_not_demo")

    def test_lot_over_cap_blocks_promotion(self):
        plan = _planned_plan(self.user, self.demo, mid="m10", tps=("1.0900",), lots=("0.05",))
        with self.assertRaises(promo.PromotionRejected) as ctx:
            promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(ctx.exception.code, "lot_out_of_range")

    def test_rejection_audit_persists(self):
        SignalSourceConfig.objects.filter(source=SRC).update(auto_demo_execution_enabled=False)
        plan = _planned_plan(self.user, self.demo, mid="m11")
        with self.assertRaises(promo.PromotionRejected):
            promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertTrue(PromotionAuditEvent.objects.filter(
            event=PromotionAuditEvent.Event.PROMOTION_REJECTED, plan=plan).exists())

    def test_audit_chain_on_success(self):
        plan = _planned_plan(self.user, self.demo, mid="m12")
        promo.promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertTrue(PromotionAuditEvent.objects.filter(
            event=PromotionAuditEvent.Event.PROMOTION_CREATED, plan=plan).exists())
        self.assertEqual(PromotionAuditEvent.objects.filter(
            event=PromotionAuditEvent.Event.JOB_CREATED, plan=plan).count(), 3)


class EndpointGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op2", email="op2@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D2", is_demo=True
        )
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)
        plan = _planned_plan(self.user, self.demo, mid="g1")
        promo.promote_plan_to_shadow_jobs(plan, actor=self.user)  # 3 PENDING shadow jobs, no terminal_node

    def _worker(self, wid, secret, perms=None):
        WorkerIdentity.objects.create(
            worker_id=wid, worker_secret_hash=WorkerIdentity.hash_secret(secret),
            worker_permissions=perms or {}, status=WorkerIdentity.Status.ACTIVE,
        )
        c = APIClient()
        return c, dict(HTTP_X_WORKER_ID=wid, HTTP_X_WORKER_SECRET=secret)

    def test_ordinary_worker_cannot_claim_shadow(self):
        c, h = self._worker("w1", "s1", perms={})  # no shadow_worker
        resp = c.get(NEXT_URL + "?job_type=PLACE_ORDER_SHADOW", **h)
        self.assertEqual(resp.status_code, 204)  # no_jobs — guard excluded them
        # nothing was claimed (no RUNNING shadow job)
        self.assertEqual(
            ExecutionJob.objects.filter(
                job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW,
                status=ExecutionJob.Status.RUNNING).count(), 0)

    def test_staff_user_cannot_claim_shadow(self):
        staff = User.objects.create_user(
            username="st", email="st@x.invalid", password="x", is_staff=True)
        c = APIClient(); c.force_authenticate(user=staff)
        resp = c.get(NEXT_URL + "?job_type=PLACE_ORDER_SHADOW")
        self.assertEqual(resp.status_code, 204)

    def test_shadow_worker_can_claim_shadow(self):
        c, h = self._worker("w2", "s2", perms={"shadow_worker": True})
        resp = c.get(NEXT_URL + "?job_type=PLACE_ORDER_SHADOW", **h)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["job_type"], "PLACE_ORDER_SHADOW")

    def test_normal_claim_path_unchanged_for_non_shadow(self):
        # An ordinary worker can still claim a normal job (guard only excludes shadow).
        ExecutionJob.objects.create(
            account=self.demo, job_type=ExecutionJob.JobType.SYNC_POSITIONS,
            status=ExecutionJob.Status.PENDING, payload={})
        c, h = self._worker("w3", "s3", perms={})
        resp = c.get(NEXT_URL + "?job_type=SYNC_POSITIONS", **h)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["job_type"], "SYNC_POSITIONS")

    def test_ordinary_worker_default_claim_excludes_shadow(self):
        # Even the default claim (no job_type → SYNC) never yields a shadow job.
        c, h = self._worker("w4", "s4", perms={})
        resp = c.get(NEXT_URL, **h)
        self.assertIn(resp.status_code, (200, 204))
        if resp.status_code == 200:
            self.assertNotEqual(resp.data.get("job_type"), "PLACE_ORDER_SHADOW")


class CommandAndGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op3", email="op3@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D3", is_demo=True
        )
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)

    def test_command_creates_shadow_jobs_no_executable(self):
        from io import StringIO
        plan = _planned_plan(self.user, self.demo, mid="c1")
        out = StringIO()
        call_command("promote_plan_to_shadow", "--plan", str(plan.id), stdout=out, stderr=StringIO())
        output = out.getvalue()
        self.assertIn("0 executable jobs created", output)
        self.assertIn("0 orders placed", output)
        self.assertEqual(
            ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW).count(), 3)
        self.assertEqual(
            ExecutionJob.objects.exclude(job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW).count(), 0)

    def test_promotion_module_makes_no_order_mt5_or_network_call(self):
        src = pathlib.Path(importlib.import_module("execution.signal_promotion").__file__).read_text()
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
        for forbidden in ("order_send", "order_check", "MetaTrader5", "mt5", "requests",
                          "httpx", "urllib", "create_place_order_job",
                          "create_open_trade_job", "agent_order"):
            self.assertNotIn(forbidden, names, f"promotion references {forbidden}")

    def test_promotion_creates_no_open_trade_or_test_order(self):
        # Static: the module never references OPEN_TRADE / PLACE_TEST_ORDER. (E3 adds a
        # DEMO-gated PLACE_ORDER path — promote_plan_to_demo_jobs — whose shadow-only-under-
        # SHADOW-mode + PLACE_ORDER-only-under-DEMO-mode safety is proven behaviourally in
        # execution.tests_e3_demo_promotion; a static scan can't distinguish the gated path.)
        src = pathlib.Path(importlib.import_module("execution.signal_promotion").__file__).read_text()
        self.assertIn("PLACE_ORDER_SHADOW", src)
        stripped = src.replace("PLACE_ORDER_SHADOW", "")
        for jt in ("JobType.OPEN_TRADE", "JobType.PLACE_TEST_ORDER"):
            self.assertNotIn(jt, stripped, f"promotion references executable {jt}")
