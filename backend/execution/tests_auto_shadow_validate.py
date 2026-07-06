"""AUTO-SHADOW-VALIDATE — end-to-end validation of the auto-shadow foundation.

Drives a REAL Wayond-format message all the way through ``acquire_message`` (which fires
``signal_acquired`` → the auto-router) and proves:
  * default config leaves the signal MANUAL (PENDING, no job);
  * a fully-armed test config auto-advances into ``PLACE_ORDER_SHADOW`` jobs ONLY;
  * no ``PLACE_ORDER`` / ``order_send`` path is reachable;
  * missing SL, missing TP, and edited signals all block;
  * the runtime risk controls still run on the auto path.

All in a repo/test context — no deploy, no arming in prod, no real order.
"""
import ast
import pathlib
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution import auto_router, risk_controls
from execution.models import (
    ExecutionControl,
    ExecutionJob,
    SignalExecutionPlan,
    SignalSourceConfig,
)
from signal_intake import acquisition
from signal_intake.models import (
    AcquiredMessage,
    ParserProfile,
    PendingSignalApproval,
    SignalProvider,
)
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
O = AcquiredMessage.Outcome
EXECUTABLE = {
    ExecutionJob.JobType.PLACE_ORDER,
    ExecutionJob.JobType.OPEN_TRADE,
    ExecutionJob.JobType.PLACE_TEST_ORDER,
}
# A real Wayond ENTRY_SIGNAL body (from the certified corpus), symbol swapped to an
# allowed EURUSD so the demo plan/promote path is exercised end-to-end.
WAYOND_TEXT = (
    "EURUSD | Potential upward movement\n\nEURUSD | BUY 1.0850\n\n"
    "❌ Stop Loss 1.0800 (50 pips)\n\n✅TP1 1.0900\n✅TP2 1.0950\n✅TP3 1.1000"
)


def _pending(provider, *, sl="1.0800", tps=("1.0900", "1.0950"), edited=False, mid="p1"):
    return PendingSignalApproval.objects.create(
        source=provider.slug, message_id=mid, provider=provider, symbol="EURUSD",
        direction="BUY", entry="1.0850", stop_loss=sl,
        take_profit=(tps[0] if tps else ""), take_profits=list(tps),
        status=PendingSignalApproval.Status.PENDING_APPROVAL,
        source_edited=edited, raw_payload={"chat_id": "-100123"},
    )


class AutoShadowValidateBase(TestCase):
    def setUp(self):
        self.op = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        # Provision the system reviewer identity via the command (also validates it).
        call_command("provision_auto_shadow")
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
        self.strategy = Strategy.objects.create(owner=self.op, name="Wayond Auto")

    def _arm(self):
        SignalSourceConfig.objects.create(
            source="wayond", auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"),
        )
        StrategyAssignment.objects.create(
            strategy=self.strategy, account=self.demo, is_active=True,
            stage=StrategyAssignment.STAGE_LIVE,
            execution_mode=StrategyAssignment.ExecutionMode.AUTO_SHADOW,
        )
        ctrl = ExecutionControl.get_solo()
        ctrl.auto_execution_enabled = True
        ctrl.save()

    def _feed(self, text=WAYOND_TEXT, mid="w1"):
        return acquisition.acquire_message(self.provider, {
            "message_id": mid, "chat_id": "-100123", "date": timezone.now(), "text": text,
        })

    def _route(self, approval):
        auto_router.route_acquired_signal(
            provider=self.provider, acquired=None, approval=approval, outcome=O.INTAKEN,
        )


class DefaultManualTests(AutoShadowValidateBase):
    def test_default_signal_stays_manual_end_to_end(self):
        acq = self._feed()  # default config: auto OFF
        self.assertEqual(acq.outcome, O.INTAKEN)
        approval = PendingSignalApproval.objects.get(pk=acq.approval_id)
        self.assertEqual(approval.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(SignalExecutionPlan.objects.count(), 0)


class AutoShadowE2ETests(AutoShadowValidateBase):
    def test_fully_armed_creates_shadow_jobs_only_end_to_end(self):
        self._arm()
        acq = self._feed()  # acquire_message → signal_acquired → auto-router
        self.assertEqual(acq.outcome, O.INTAKEN)
        approval = PendingSignalApproval.objects.get(pk=acq.approval_id)
        self.assertEqual(approval.status, PendingSignalApproval.Status.APPROVED)
        jobs = list(ExecutionJob.objects.all())
        self.assertTrue(jobs, "expected shadow jobs")
        # ACCEPTANCE 2/3/4 — only PLACE_ORDER_SHADOW; zero executable order jobs.
        for job in jobs:
            self.assertEqual(job.job_type, ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        self.assertFalse(ExecutionJob.objects.filter(job_type__in=EXECUTABLE).exists())
        plan = SignalExecutionPlan.objects.get(approval=approval)
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PROMOTED)
        # execution_mode on every shadow job's payload is SHADOW.
        for job in jobs:
            self.assertEqual(job.payload.get("execution_mode"), "SHADOW")


class BlockedCaseTests(AutoShadowValidateBase):
    def test_missing_sl_blocks(self):
        self._arm()
        self._route(_pending(self.provider, sl="", tps=("1.0900",)))
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_missing_tp_blocks(self):
        self._arm()
        self._route(_pending(self.provider, tps=()))
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_edited_signal_blocks(self):
        self._arm()
        self._route(_pending(self.provider, edited=True))
        self.assertEqual(ExecutionJob.objects.count(), 0)


class RiskControlTests(AutoShadowValidateBase):
    def test_risk_controls_run_and_block_promotion(self):
        self._arm()
        # Force the runtime risk gate to trip (open-positions cap 0). A valid, armed signal
        # with SL+TP must produce NO shadow job → proves evaluate_promotion_risk runs.
        with mock.patch.object(risk_controls, "MAX_OPEN_POSITIONS_PER_ACCOUNT", 0):
            self._route(_pending(self.provider))
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_risk_controls_allow_when_within_caps(self):
        # Control: same signal WITHOUT the forced cap → shadow jobs are created.
        self._arm()
        self._route(_pending(self.provider))
        self.assertTrue(ExecutionJob.objects.filter(
            job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW).exists())
        self.assertFalse(ExecutionJob.objects.filter(job_type__in=EXECUTABLE).exists())


class BoundaryAndCommandTests(AutoShadowValidateBase):
    def test_no_order_send_in_router_code(self):
        tree = ast.parse(pathlib.Path(auto_router.__file__).read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for token in ("order_send", "order_check", "agent_order", "PLACE_ORDER",
                      "OPEN_TRADE", "PLACE_TEST_ORDER"):
            self.assertNotIn(token, names)
        self.assertIn("promote_plan_to_shadow_jobs", names)

    def test_provision_command_identity(self):
        u = User.objects.get(username="guvfx-auto-system")
        self.assertTrue(u.has_perm("signal_intake.review_signals"))
        self.assertFalse(u.has_usable_password())  # service identity, cannot log in
        self.assertFalse(u.is_staff)
        # Idempotent.
        call_command("provision_auto_shadow")
        self.assertEqual(User.objects.filter(username="guvfx-auto-system").count(), 1)
        # Revoke (rollback) removes the permission.
        call_command("provision_auto_shadow", "--revoke")
        self.assertFalse(
            User.objects.get(username="guvfx-auto-system").has_perm("signal_intake.review_signals")
        )
