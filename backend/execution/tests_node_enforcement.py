"""
GFX-PKT-E3-NODE-ASSIGNMENT-ENFORCEMENT tests.

Flag OFF (default): behaviour unchanged — accounts without a terminal node still
promote (the prod/legacy state). Flag ON: promotion is blocked for an unassigned
account or a non-ACTIVE node, with a persisted PROMOTION_REJECTED audit; an
ACTIVE-node account promotes. The audit command reports PASS/FAIL per account and
--strict exits non-zero on failures. All SHADOW — no order.
"""

import os
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution.signal_promotion import PromotionRejected, promote_plan_to_shadow_jobs
from execution.models import (
    ExecutionJob,
    ProposedOrderLeg,
    PromotionAuditEvent,
    SignalExecutionPlan,
    SignalSourceConfig,
    TerminalNode,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()
SRC = PendingSignalApproval.Source.WAYOND_TELEGRAM
FLAG = {"RISK_REQUIRE_TERMINAL_NODE": "1"}


class NodeEnforcementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)

    def _account(self, node=None):
        return TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
            terminal_node=node,
        )

    def _plan(self, account, mid="p1"):
        approval = PendingSignalApproval.objects.create(
            source=SRC, message_id=mid, symbol="EURUSD", direction="BUY",
            stop_loss="1.0800", take_profits=["1.0900"],
            status=PendingSignalApproval.Status.APPROVED,
        )
        plan = SignalExecutionPlan.objects.create(
            approval=approval, account=account, source=SRC, message_id=mid,
            symbol="EURUSD", direction="BUY", stop_loss="1.0800", is_demo=True,
            signal_timestamp=timezone.now(), status=SignalExecutionPlan.Status.PLANNED,
        )
        ProposedOrderLeg.objects.create(
            plan=plan, leg_index=1, take_profit="1.0900", stop_loss="1.0800",
            lot_size=Decimal("0.01"), status=ProposedOrderLeg.Status.PLANNED,
        )
        return plan

    def _node(self, status=TerminalNode.Status.ACTIVE):
        return TerminalNode.objects.create(hostname=f"node-{status}", status=status)

    # --- flag OFF (default): behaviour unchanged --------------------------
    def test_flag_off_unassigned_account_still_promotes(self):
        env = {k: v for k, v in os.environ.items() if k != "RISK_REQUIRE_TERMINAL_NODE"}
        with mock.patch.dict(os.environ, env, clear=True):
            plan = self._plan(self._account(node=None))
            jobs = promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_type, ExecutionJob.JobType.PLACE_ORDER_SHADOW)

    # --- flag ON: enforcement ---------------------------------------------
    def test_flag_on_unassigned_account_blocked(self):
        with mock.patch.dict(os.environ, FLAG):
            plan = self._plan(self._account(node=None))
            with self.assertRaises(PromotionRejected) as cm:
                promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(cm.exception.code, "account_node_unassigned")
        self.assertTrue(  # every block decision is audited
            PromotionAuditEvent.objects.filter(
                plan=plan, event=PromotionAuditEvent.Event.PROMOTION_REJECTED
            ).exists()
        )

    def test_flag_on_draining_node_blocked(self):
        with mock.patch.dict(os.environ, FLAG):
            plan = self._plan(self._account(node=self._node(TerminalNode.Status.DRAINING)))
            with self.assertRaises(PromotionRejected) as cm:
                promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(cm.exception.code, "node_not_active")

    def test_flag_on_active_node_promotes(self):
        with mock.patch.dict(os.environ, FLAG):
            plan = self._plan(self._account(node=self._node()))
            jobs = promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(len(jobs), 1)
        # the job carries the node snapshot
        self.assertIsNotNone(jobs[0].terminal_node_id)

    # --- audit command ------------------------------------------------------
    def test_audit_command_reports_and_strict_fails(self):
        self._account(node=None)
        from io import StringIO
        out = StringIO()
        call_command("audit_node_assignments", stdout=out)
        text = out.getvalue()
        self.assertIn("FAIL account_node_unassigned", text)
        self.assertIn("fail=1", text)
        with self.assertRaises(SystemExit):
            call_command("audit_node_assignments", "--strict", stdout=StringIO())

    def test_audit_command_passes_active_node(self):
        self._account(node=self._node())
        from io import StringIO
        out = StringIO()
        call_command("audit_node_assignments", "--strict", stdout=out)  # no SystemExit
        self.assertIn("PASS", out.getvalue())
        self.assertIn("fail=0", out.getvalue())
