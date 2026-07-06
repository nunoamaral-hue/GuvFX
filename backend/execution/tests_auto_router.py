"""AUTO-SHADOW FOUNDATION — auto-router boundary + fail-closed tests.

Proves the packet's acceptance criteria: default config leaves behaviour exactly manual;
each missing gate → MANUAL; edited signals → MANUAL; router exceptions fail closed; a
fully-armed signal produces ONLY ``PLACE_ORDER_SHADOW`` jobs (never ``PLACE_ORDER`` /
``order_send``); the new fields are additive with safe defaults.
"""
import ast
import pathlib
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import auto_router
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
from signal_intake.signals import signal_acquired
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
O = AcquiredMessage.Outcome
EXECUTABLE_JOB_TYPES = {
    ExecutionJob.JobType.PLACE_ORDER,
    ExecutionJob.JobType.OPEN_TRADE,
    ExecutionJob.JobType.PLACE_TEST_ORDER,
}


def _pending_approval(provider, *, edited=False, message_id="m1"):
    return PendingSignalApproval.objects.create(
        source=provider.slug, message_id=message_id, provider=provider,
        symbol="EURUSD", direction="BUY", entry="1.0850", stop_loss="1.0800",
        take_profit="1.0900", take_profits=["1.0900", "1.0950"],
        status=PendingSignalApproval.Status.PENDING_APPROVAL, source_edited=edited,
        raw_payload={"chat_id": "-100123"},
    )


class _ArmedBase(TestCase):
    """A fully-armed auto-shadow config. Individual tests knock out one gate."""

    def setUp(self):
        self.op = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        # The router resolves the system actor by username; a superuser satisfies
        # `review_signals` via has_perm (in prod this is a scoped non-super user).
        self.system = User.objects.create_user(
            username="guvfx-auto-system", email="sys@x.invalid", password="x",
            is_staff=True, is_superuser=True,
        )
        self.demo = TradingAccount.objects.create(
            user=self.op, name="Demo", account_number="D1", is_demo=True,
        )
        self.parser = ParserProfile.objects.create(
            slug="wayond_v1",
            certification_level=ParserProfile.CertificationLevel.MEDIUM,
        )
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="-100123",
            parser_profile=self.parser, status=SignalProvider.Status.ARMED,
        )
        self.source_cfg = SignalSourceConfig.objects.create(
            source="wayond", auto_demo_execution_enabled=True,
            total_lot_target=Decimal("0.03"),
        )
        self.strategy = Strategy.objects.create(owner=self.op, name="Wayond Auto")
        self.assignment = StrategyAssignment.objects.create(
            strategy=self.strategy, account=self.demo, is_active=True,
            stage=StrategyAssignment.STAGE_LIVE,
            execution_mode=StrategyAssignment.ExecutionMode.AUTO_SHADOW,
        )
        ctrl = ExecutionControl.get_solo()
        ctrl.auto_execution_enabled = True
        ctrl.kill_switch_engaged = False
        ctrl.save()

    def _acq(self, approval, *, fresh=True):
        return AcquiredMessage.objects.create(
            provider=self.provider, chat_id="-100123", message_id=approval.message_id,
            outcome=O.INTAKEN, approval=approval,
            telegram_date=timezone.now() if fresh else timezone.now() - timedelta(hours=2),
        )


class ArmedPathTests(_ArmedBase):
    def test_fully_armed_creates_only_shadow_jobs(self):
        approval = _pending_approval(self.provider)
        acq = self._acq(approval)
        auto_router.route_acquired_signal(
            provider=self.provider, acquired=acq, approval=approval, outcome=O.INTAKEN,
        )
        approval.refresh_from_db()
        self.assertEqual(approval.status, PendingSignalApproval.Status.APPROVED)
        jobs = list(ExecutionJob.objects.all())
        self.assertTrue(jobs, "expected shadow jobs to be created")
        # ACCEPTANCE 6/7/8: every job is PLACE_ORDER_SHADOW; NO executable order job.
        for job in jobs:
            self.assertEqual(job.job_type, ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        self.assertFalse(
            ExecutionJob.objects.filter(job_type__in=EXECUTABLE_JOB_TYPES).exists()
        )
        plan = SignalExecutionPlan.objects.get(approval=approval)
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PROMOTED)

    def test_effective_mode_armed(self):
        approval = _pending_approval(self.provider)
        mode, reason = auto_router.effective_mode(approval)
        self.assertEqual(mode, auto_router.MODE_AUTO_SHADOW)
        self.assertEqual(reason, "armed")


class GateFailClosedTests(_ArmedBase):
    def _assert_manual(self, approval, reason):
        mode, got = auto_router.effective_mode(approval)
        self.assertEqual(mode, auto_router.MODE_MANUAL)
        self.assertEqual(got, reason)
        self.assertFalse(auto_router.should_auto_execute(approval)[0])

    def test_edited_signal_is_manual(self):
        self._assert_manual(_pending_approval(self.provider, edited=True), "edited_signal")

    def test_auto_execution_disabled_is_manual(self):
        ctrl = ExecutionControl.get_solo()
        ctrl.auto_execution_enabled = False
        ctrl.save()
        self._assert_manual(_pending_approval(self.provider), "auto_execution_disabled")

    def test_kill_switch_is_manual(self):
        ctrl = ExecutionControl.get_solo()
        ctrl.kill_switch_engaged = True
        ctrl.save()
        self._assert_manual(_pending_approval(self.provider), "kill_switch")

    def test_provider_not_armed_is_manual(self):
        self.provider.status = SignalProvider.Status.ONBOARDING
        self.provider.save()
        self._assert_manual(_pending_approval(self.provider), "provider_not_armed")

    def test_source_not_enabled_is_manual(self):
        self.source_cfg.auto_demo_execution_enabled = False
        self.source_cfg.save()
        self._assert_manual(_pending_approval(self.provider), "source_not_enabled")

    def test_low_confidence_is_manual(self):
        self.parser.certification_level = ParserProfile.CertificationLevel.LOW
        self.parser.save()
        self._assert_manual(_pending_approval(self.provider), "parser_confidence_below_medium")

    def test_no_auto_assignment_is_manual(self):
        self.assignment.execution_mode = StrategyAssignment.ExecutionMode.MANUAL
        self.assignment.save()
        self._assert_manual(_pending_approval(self.provider), "no_unique_auto_shadow_assignment")

    def test_ambiguous_assignment_is_manual(self):
        acct2 = TradingAccount.objects.create(
            user=self.op, name="Demo2", account_number="D2", is_demo=True,
        )
        StrategyAssignment.objects.create(
            strategy=Strategy.objects.create(owner=self.op, name="Other"),
            account=acct2, is_active=True, stage=StrategyAssignment.STAGE_LIVE,
            execution_mode=StrategyAssignment.ExecutionMode.AUTO_SHADOW,
        )
        self._assert_manual(_pending_approval(self.provider), "no_unique_auto_shadow_assignment")

    def test_none_provider_is_manual(self):
        approval = _pending_approval(self.provider)
        approval.provider = None
        approval.save()
        self._assert_manual(approval, "provider_not_armed")


class RouteAndExceptionTests(_ArmedBase):
    def test_non_intaken_outcome_is_noop(self):
        approval = _pending_approval(self.provider)
        auto_router.route_acquired_signal(
            provider=self.provider, acquired=None, approval=approval,
            outcome=O.DROPPED_NOT_ARMED,
        )
        approval.refresh_from_db()
        self.assertEqual(approval.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_none_approval_is_noop(self):
        auto_router.route_acquired_signal(
            provider=self.provider, acquired=None, approval=None, outcome=O.INTAKEN,
        )
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_should_auto_execute_swallows_exception(self):
        # A broken approval object (no attributes) must fail closed, never raise.
        go, reason = auto_router.should_auto_execute(object())
        self.assertFalse(go)
        self.assertTrue(reason.startswith("error:"))


class DefaultBehaviourTests(TestCase):
    """ACCEPTANCE 1/2/10 — defaults leave behaviour exactly manual."""

    def test_model_defaults_are_safe(self):
        self.assertFalse(ExecutionControl.get_solo().auto_execution_enabled)
        parser = ParserProfile.objects.create(slug="p1")
        self.assertEqual(parser.certification_level, ParserProfile.CertificationLevel.LOW)
        op = User.objects.create_user(username="u", email="u@x.invalid", password="x")
        acct = TradingAccount.objects.create(
            user=op, name="A", account_number="A1", is_demo=True,
        )
        assignment = StrategyAssignment.objects.create(
            strategy=Strategy.objects.create(owner=op, name="S"), account=acct,
        )
        self.assertEqual(assignment.execution_mode, StrategyAssignment.ExecutionMode.MANUAL)

    def test_intaken_signal_with_defaults_stays_pending_no_job(self):
        op = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        parser = ParserProfile.objects.create(slug="wayond_v1")
        provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="-100123",
            parser_profile=parser, status=SignalProvider.Status.ARMED,
        )
        approval = _pending_approval(provider)
        acq = AcquiredMessage.objects.create(
            provider=provider, chat_id="-100123", message_id="m1",
            outcome=O.INTAKEN, approval=approval, telegram_date=timezone.now(),
        )
        # Route with default execution config (auto OFF) → no-op.
        auto_router.route_acquired_signal(
            provider=provider, acquired=acq, approval=approval, outcome=O.INTAKEN,
        )
        approval.refresh_from_db()
        self.assertEqual(approval.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(SignalExecutionPlan.objects.count(), 0)


class SignalWiringTests(TestCase):
    """acquire_message fires signal_acquired for a newly-acquired message."""

    def test_acquire_message_fires_signal(self):
        parser = ParserProfile.objects.create(slug="wayond_v1")
        provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="-100123",
            parser_profile=parser, status=SignalProvider.Status.ONBOARDING,  # un-armed
        )
        seen = {}

        def _probe(sender=None, **kwargs):
            seen.update(kwargs)

        signal_acquired.connect(_probe, dispatch_uid="test_probe")
        try:
            acq = acquisition.acquire_message(
                provider, {"message_id": "z1", "chat_id": "-100123",
                           "date": timezone.now(), "text": "hi"},
            )
        finally:
            signal_acquired.disconnect(dispatch_uid="test_probe")
        # Un-armed → DROPPED, but the signal still fired with the outcome + row.
        self.assertEqual(acq.outcome, O.DROPPED_NOT_ARMED)
        self.assertEqual(seen.get("outcome"), O.DROPPED_NOT_ARMED)
        self.assertEqual(seen.get("acquired"), acq)


class BoundarySourceTests(TestCase):
    """ACCEPTANCE 8/9 — the router source has no order_send/executable-order path."""

    def test_router_source_has_no_forbidden_tokens(self):
        # Walk the AST for CODE identifiers only (Name/Attribute) so the docstring — which
        # legitimately *describes* the excluded calls — does not trip the check.
        tree = ast.parse(pathlib.Path(auto_router.__file__).read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for token in ("order_send", "order_check", "agent_order", "create_place_order",
                      "create_open_trade", "PLACE_ORDER", "OPEN_TRADE", "PLACE_TEST_ORDER",
                      "TRADE_ACTION"):
            self.assertNotIn(token, names, f"auto_router must not reference {token!r} in code")
        # It reaches execution ONLY through the shadow-only promotion bridge.
        self.assertIn("promote_plan_to_shadow_jobs", names)
