"""
T1 R1 (HIGH-3) — downstream (execution-plan) identity completeness, DARK/default-OFF.

The source-aware chat identity must reach SignalExecutionPlan so two genuinely distinct
approved messages from DIFFERENT chats with the same source+message_id do not collide on
the plan's unique (source, chat_id, message_id, account) key. The pathway is gated by
SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED (default OFF): OFF is byte-identical to the
pre-T1 behaviour (plan chat read from raw_payload, always blank); ON carries the
approval's chat-scoped identity. Activation is a separate cutover authorization; this
proves both states and that NO execution authority or order behaviour changes either way.
"""

import os
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

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
CHAT_A = "1001"
CHAT_B = "2002"
_ON = {"SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED": "1"}


def _approved(message_id, *, chat_id="", source=SRC):
    return PendingSignalApproval.objects.create(
        source=source, chat_id=chat_id, message_id=message_id,
        symbol="EURUSD", direction="BUY", entry="1.0850", stop_loss="1.0800",
        take_profit="1.0900", take_profits=["1.0900", "1.0950", "1.1000"],
        status=PendingSignalApproval.Status.APPROVED, raw_payload={},
    )


class DownstreamChatIdentityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="op", email="op@example.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
            broker_name="DemoBroker")
        SignalSourceConfig.objects.create(
            source=SRC, auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"))

    # ---- default OFF: byte-identical to pre-T1 (plan chat stays blank) --------
    def test_flag_off_plan_chat_is_blank_and_no_order(self):
        before = ExecutionJob.objects.count()
        plan = planning.plan_demo_execution(
            _approved("m1", chat_id=CHAT_A), account=self.demo, actor=self.user)
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)
        self.assertEqual(plan.chat_id, "")                 # unchanged legacy behaviour
        self.assertEqual(ExecutionJob.objects.count(), before)  # no order side-effect

    def test_flag_off_distinct_chat_same_message_id_collide(self):
        """Demonstrates the defect the flag fixes: with the chat NOT carried downstream,
        two genuinely distinct approvals collide on (source, '', message_id, account)."""
        planning.plan_demo_execution(_approved("5", chat_id=CHAT_A), account=self.demo)
        with self.assertRaises(planning.PlanRejected) as cm:
            planning.plan_demo_execution(_approved("5", chat_id=CHAT_B), account=self.demo)
        self.assertEqual(cm.exception.code, "plan_integrity_error")  # fail-closed, no plan

    # ---- ON: chat carried end-to-end, distinct channels stay distinct ---------
    def test_flag_on_plan_carries_approval_chat(self):
        with mock.patch.dict(os.environ, _ON):
            plan = planning.plan_demo_execution(
                _approved("m2", chat_id=CHAT_A), account=self.demo)
        self.assertEqual(plan.chat_id, CHAT_A)

    def test_flag_on_distinct_chat_same_message_id_make_two_plans(self):
        with mock.patch.dict(os.environ, _ON):
            p_a = planning.plan_demo_execution(_approved("5", chat_id=CHAT_A), account=self.demo)
            p_b = planning.plan_demo_execution(_approved("5", chat_id=CHAT_B), account=self.demo)
        self.assertNotEqual(p_a.id, p_b.id)
        self.assertEqual(p_a.chat_id, CHAT_A)
        self.assertEqual(p_b.chat_id, CHAT_B)
        self.assertEqual(
            SignalExecutionPlan.objects.filter(source=SRC, message_id="5").count(), 2)

    def test_flag_on_single_channel_is_idempotent(self):
        """ON must not create extra plans: one approval → one plan (approval+account key)."""
        appr = _approved("m3", chat_id=CHAT_A)
        with mock.patch.dict(os.environ, _ON):
            p1 = planning.plan_demo_execution(appr, account=self.demo)
            p2 = planning.plan_demo_execution(appr, account=self.demo)  # replay
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(SignalExecutionPlan.objects.filter(approval=appr).count(), 1)

    def test_flag_on_no_new_orders(self):
        before = ExecutionJob.objects.count()
        with mock.patch.dict(os.environ, _ON):
            planning.plan_demo_execution(_approved("m4", chat_id=CHAT_A), account=self.demo)
        self.assertEqual(ExecutionJob.objects.count(), before)  # still no order authority
