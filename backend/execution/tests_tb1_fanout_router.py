"""TB-1 / ADR-0020 — per-source fan-out router behind default-OFF MULTI_ACCOUNT_ROUTING_ENABLED.

Proves the Sponsor's control matrix:
  * flag OFF  → single-tenant path byte-identical (same single destination; 2nd bound account →
    MANUAL exactly as today);
  * flag ON   → one Telegram source fans out to N isolated demo accounts: one plan + jobs per
    account, correct per-account binding, no cross-routing, independent suspension, one destination's
    failure never blocks another, duplicate/retry/reconcile idempotency, unready accounts excluded.

Broker-truth (slot/generation) rejection is enforced downstream at the bridge exact-binding gate
(tests_bridge_binding); the router-level "stale" disposition is the per-account signal-age VOID here.
"""
from decimal import Decimal
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from execution import auto_router
from execution.auto_router import (
    _resolve_targets, effective_mode, MODE_AUTO_DEMO, MODE_MANUAL,
)
from execution.models import ExecutionControl, ExecutionJob, SignalExecutionPlan, SignalSourceConfig
from execution.signal_planning import PlanRejected, plan_demo_execution
from signal_intake.models import (
    AcquiredMessage, ParserProfile, PendingSignalApproval, SignalAuditEvent, SignalProvider,
)
from strategies.models import Strategy, StrategyAssignment
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
AM = StrategyAssignment.ExecutionMode
DEMO = AM.AUTO_DEMO
JT = ExecutionJob.JobType
O = AcquiredMessage.Outcome
SOURCE = "ti_signals"
ON = override_settings(MULTI_ACCOUNT_ROUTING_ENABLED=True)


class _FanoutBase(TestCase):
    """Fully-armed AUTO_DEMO config with a source-bound assignment on each of two demo accounts."""

    def setUp(self):
        self.system = User.objects.create_user(
            username="guvfx-auto-system", email="sys@x.invalid", password="x",
            is_staff=True, is_superuser=True)
        self.parser = ParserProfile.objects.create(
            slug="ti_v1", certification_level=ParserProfile.CertificationLevel.MEDIUM)
        self.provider = SignalProvider.objects.create(
            slug=SOURCE, name="TI Signals", telegram_chat_id="-100123",
            parser_profile=self.parser, status=SignalProvider.Status.ARMED)
        SignalSourceConfig.objects.create(
            source=SOURCE, auto_demo_execution_enabled=True, total_lot_target=Decimal("0.02"))
        self.userA = User.objects.create_user(username="a", email="a@x.invalid", password="x")
        self.userB = User.objects.create_user(username="b", email="b@x.invalid", password="x")
        self.acctA = self._acct(self.userA, "AA1")
        self.acctB = self._acct(self.userB, "BB1")
        self.asnA = self._bind(self.userA, self.acctA)
        self.asnB = self._bind(self.userB, self.acctB)
        ctrl = ExecutionControl.get_solo()
        ctrl.signal_execution_mode = ExecutionControl.SignalExecutionMode.DEMO
        ctrl.auto_execution_enabled = True
        ctrl.kill_switch_engaged = False
        ctrl.save()

    def _acct(self, user, number, *, is_demo=True, broker_server=None):
        return TradingAccount.objects.create(
            user=user, name=number, account_number=number, is_demo=is_demo,
            is_active=True, broker_server=broker_server)

    def _bind(self, user, acct, *, active=True, mode=DEMO):
        strat = Strategy.objects.create(owner=user, name=f"WIM {acct.account_number}")
        return StrategyAssignment.objects.create(
            strategy=strat, account=acct, execution_mode=mode, signal_source=SOURCE,
            is_active=active, stage=StrategyAssignment.STAGE_LIVE)

    def _approval(self, message_id="m1"):
        return PendingSignalApproval.objects.create(
            source=SOURCE, message_id=message_id, provider=self.provider, symbol="EURUSD",
            direction="BUY", entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900"], status=PendingSignalApproval.Status.PENDING_APPROVAL,
            raw_payload={"chat_id": "-100123"})

    def _acq(self, approval, *, fresh=True):
        # get_or_create so re-routing the SAME signal (idempotency / reconcile tests) reuses the one
        # AcquiredMessage instead of colliding on its unique(provider, message_id).
        obj, _ = AcquiredMessage.objects.get_or_create(
            provider=self.provider, message_id=approval.message_id,
            defaults=dict(
                chat_id="-100123", outcome=O.INTAKEN, approval=approval,
                telegram_date=timezone.now() if fresh else timezone.now() - timedelta(hours=2)))
        return obj

    def _route(self, approval):
        auto_router.route_acquired_signal(
            provider=self.provider, acquired=self._acq(approval), approval=approval, outcome=O.INTAKEN)


# ─────────────────────────── flag default (safety) ───────────────────────────
class FlagDefaultTests(TestCase):
    def test_defaults_off_with_no_setting_or_env(self):
        import os
        from execution.auto_router import _multi_account_routing_enabled
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MULTI_ACCOUNT_ROUTING_ENABLED", None)
            with override_settings():  # no MULTI_ACCOUNT_ROUTING_ENABLED setting
                # ensure the setting attr is truly absent
                from django.conf import settings as _s
                if hasattr(_s, "MULTI_ACCOUNT_ROUTING_ENABLED"):
                    delattr(_s._wrapped, "MULTI_ACCOUNT_ROUTING_ENABLED")
                self.assertFalse(_multi_account_routing_enabled())

    def test_env_truthy_and_falsy_values(self):
        import os
        from execution.auto_router import _multi_account_routing_enabled
        for on in ("1", "true", "TRUE", "Yes", "on"):
            with mock.patch.dict(os.environ, {"MULTI_ACCOUNT_ROUTING_ENABLED": on}):
                self.assertTrue(_multi_account_routing_enabled(), on)
        for off in ("0", "false", "no", "", "  "):
            with mock.patch.dict(os.environ, {"MULTI_ACCOUNT_ROUTING_ENABLED": off}):
                self.assertFalse(_multi_account_routing_enabled(), repr(off))

    @override_settings(MULTI_ACCOUNT_ROUTING_ENABLED=True)
    def test_setting_true_wins(self):
        from execution.auto_router import _multi_account_routing_enabled
        self.assertTrue(_multi_account_routing_enabled())


# ─────────────────────────── _resolve_targets (unit) ───────────────────────────
class ResolveTargetsTests(_FanoutBase):
    def test_fans_out_to_all_bound(self):
        got = {a.id for a in _resolve_targets(DEMO, SOURCE)}
        self.assertEqual(got, {self.asnA.id, self.asnB.id})

    def test_excludes_paused_assignment(self):
        self.asnB.is_active = False
        self.asnB.save(update_fields=["is_active"])
        self.assertEqual([a.id for a in _resolve_targets(DEMO, SOURCE)], [self.asnA.id])

    def test_excludes_inactive_account(self):
        self.acctB.is_active = False
        self.acctB.save(update_fields=["is_active"])
        self.assertEqual([a.id for a in _resolve_targets(DEMO, SOURCE)], [self.asnA.id])

    def test_excludes_non_demo_account(self):
        self.acctB.is_demo = False
        self.acctB.save(update_fields=["is_demo"])
        self.assertEqual([a.id for a in _resolve_targets(DEMO, SOURCE)], [self.asnA.id])

    def test_empty_when_all_paused(self):
        for a in (self.asnA, self.asnB):
            a.is_active = False
            a.save(update_fields=["is_active"])
        self.assertEqual(_resolve_targets(DEMO, SOURCE), [])

    def test_unbound_stays_single_tenant(self):
        # An UNBOUND legacy assignment is not fanned out — the unbound path returns at most one.
        StrategyAssignment.objects.filter(id__in=[self.asnA.id, self.asnB.id]).update(signal_source="")
        # two unbound → ambiguous → single-tenant fallback yields []
        self.assertEqual(_resolve_targets(DEMO, "wayond"), [])
        self.asnB.delete()
        # one unbound → the single legacy target
        self.assertEqual([a.id for a in _resolve_targets(DEMO, "wayond")], [self.asnA.id])


# ─────────────────────────── effective_mode flag gating ───────────────────────────
class EffectiveModeFlagTests(_FanoutBase):
    def test_two_bound_flag_off_is_manual(self):
        # Today's behaviour: a 2nd account on the source makes the source ambiguous → MANUAL.
        mode, reason = effective_mode(self._approval())
        self.assertEqual(mode, MODE_MANUAL)
        self.assertEqual(reason, "no_unique_auto_assignment")

    @ON
    def test_two_bound_flag_on_is_armed(self):
        mode, reason = effective_mode(self._approval())
        self.assertEqual(mode, MODE_AUTO_DEMO)
        self.assertEqual(reason, "armed")

    @ON
    def test_zero_routable_flag_on_is_manual(self):
        for a in (self.asnA, self.asnB):
            a.is_active = False
            a.save(update_fields=["is_active"])
        mode, reason = effective_mode(self._approval())
        self.assertEqual(mode, MODE_MANUAL)
        self.assertEqual(reason, "no_routable_assignment")


# ─────────────────────────── end-to-end fan-out (flag ON) ───────────────────────────
@ON
class FanoutRouteTests(_FanoutBase):
    def _plans(self, approval):
        return SignalExecutionPlan.objects.filter(approval=approval)

    def test_two_accounts_get_isolated_plans_and_jobs(self):
        appr = self._approval()
        self._route(appr)
        plans = {p.account_id: p for p in self._plans(appr)}
        self.assertEqual(set(plans), {self.acctA.id, self.acctB.id})   # one plan per account
        # correct binding + no cross-routing: each plan's jobs belong to that plan's account only
        for acct in (self.acctA, self.acctB):
            jobs = list(ExecutionJob.objects.filter(account=acct, job_type=JT.PLACE_ORDER))
            self.assertTrue(jobs, f"expected PLACE_ORDER jobs for account {acct.id}")
            self.assertTrue(all(j.account_id == acct.id for j in jobs))
        self.assertFalse(ExecutionJob.objects.filter(job_type=JT.PLACE_ORDER_SHADOW).exists())

    def test_duplicate_route_is_idempotent(self):
        appr = self._approval()
        self._route(appr)
        p1 = {p.id for p in self._plans(appr)}
        j1 = ExecutionJob.objects.count()
        self._route(appr)   # router retry / duplicate delivery
        self.assertEqual({p.id for p in self._plans(appr)}, p1)       # no new plans
        self.assertEqual(ExecutionJob.objects.count(), j1)           # no new jobs

    def test_independent_suspension(self):
        self.asnB.is_active = False   # suspend customer B only
        self.asnB.save(update_fields=["is_active"])
        appr = self._approval()
        self._route(appr)
        self.assertEqual([p.account_id for p in self._plans(appr)], [self.acctA.id])  # only A

    def test_partial_failure_does_not_block_sibling(self):
        # Customer B's account is demo-flagged but points at a LIVE broker server → plan rejects
        # "account_live" for B only. A must still be planned + promoted; B records a deferral.
        live = BrokerServer.objects.create(
            broker_display_name="X", server_name="live-1", environment=BrokerServer.LIVE)
        self.acctB.broker_server = live
        self.acctB.save(update_fields=["broker_server"])
        appr = self._approval()
        self._route(appr)
        self.assertEqual([p.account_id for p in self._plans(appr)], [self.acctA.id])   # A only
        self.assertTrue(ExecutionJob.objects.filter(account=self.acctA, job_type=JT.PLACE_ORDER).exists())
        self.assertFalse(ExecutionJob.objects.filter(account=self.acctB).exists())     # B none
        self.assertTrue(SignalAuditEvent.objects.filter(   # B's failure is durably recorded
            approval=appr, event=SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED).exists())

    def test_unexpected_exception_on_one_destination_isolated(self):
        appr = self._approval()
        real = plan_demo_execution

        def _side_effect(approval, *, account, **kw):
            if account.id == self.acctB.id:
                raise RuntimeError("boom-B")
            return real(approval, account=account, **kw)

        with mock.patch("execution.auto_router.plan_demo_execution", side_effect=_side_effect):
            self._route(appr)
        self.assertEqual([p.account_id for p in self._plans(appr)], [self.acctA.id])   # A survived
        self.assertTrue(SignalAuditEvent.objects.filter(
            approval=appr, event=SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED).exists())

    def test_reconciliation_restart_completes_without_duplication(self):
        # First pass: B fails (transient), only A is planned.
        appr = self._approval()
        real = plan_demo_execution
        state = {"fail_b": True}

        def _side_effect(approval, *, account, **kw):
            if account.id == self.acctB.id and state["fail_b"]:
                raise RuntimeError("transient-B")
            return real(approval, account=account, **kw)

        with mock.patch("execution.auto_router.plan_demo_execution", side_effect=_side_effect):
            self._route(appr)
        self.assertEqual([p.account_id for p in self._plans(appr)], [self.acctA.id])
        a_plan_id = self._plans(appr).get(account=self.acctA).id
        jobs_a = ExecutionJob.objects.filter(account=self.acctA).count()
        # Reconciliation restart: B now succeeds. A must NOT be re-planned/duplicated.
        state["fail_b"] = False
        self._route(appr)
        self.assertEqual(set(p.account_id for p in self._plans(appr)), {self.acctA.id, self.acctB.id})
        self.assertEqual(self._plans(appr).get(account=self.acctA).id, a_plan_id)   # same A plan
        self.assertEqual(ExecutionJob.objects.filter(account=self.acctA).count(), jobs_a)  # no dup A jobs

    def test_stale_signal_voids_per_account_no_jobs(self):
        appr = self._approval()
        auto_router.route_acquired_signal(
            provider=self.provider, acquired=self._acq(appr, fresh=False), approval=appr, outcome=O.INTAKEN)
        plans = self._plans(appr)
        self.assertEqual(set(p.account_id for p in plans), {self.acctA.id, self.acctB.id})
        self.assertTrue(all(p.status == SignalExecutionPlan.Status.VOIDED for p in plans))
        self.assertFalse(ExecutionJob.objects.exists())   # stale → no orders on any destination


# ─────────────────────────── idempotency invariant (DB + planner) ───────────────────────────
class IdempotencyInvariantTests(_FanoutBase):
    """Fan-out idempotency is enforced at the PLAN layer: the uniq_plan_approval_account DB constraint
    makes a duplicate plan for a (approval, account) impossible even under true concurrency, and the
    planner short-circuits to the existing plan. (The residual job-layer concurrency window is
    pre-existing single-tenant behaviour, unchanged by TB-1 — see ADR-0020 Consequences.)"""

    def test_planner_returns_existing_for_same_approval_account(self):
        appr = self._approval()
        appr.status = PendingSignalApproval.Status.APPROVED
        appr.save(update_fields=["status"])
        p1 = plan_demo_execution(appr, account=self.acctA, actor=self.system)
        p2 = plan_demo_execution(appr, account=self.acctA, actor=self.system)   # concurrent/retry
        self.assertEqual(p1.id, p2.id)                                          # same plan, not two

    def test_db_constraint_blocks_second_plan_per_account(self):
        appr = self._approval()
        common = dict(
            approval=appr, account=self.acctA, source=SOURCE, chat_id="-100123",
            message_id=appr.message_id, symbol="EURUSD", direction="BUY", is_demo=True)
        SignalExecutionPlan.objects.create(**common)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SignalExecutionPlan.objects.create(**common)   # (approval, account) unique

    def test_same_approval_different_accounts_allowed(self):
        appr = self._approval()
        common = dict(
            approval=appr, source=SOURCE, chat_id="-100123", message_id=appr.message_id,
            symbol="EURUSD", direction="BUY", is_demo=True)
        SignalExecutionPlan.objects.create(account=self.acctA, **common)
        SignalExecutionPlan.objects.create(account=self.acctB, **common)   # different account: OK
        self.assertEqual(SignalExecutionPlan.objects.filter(approval=appr).count(), 2)


# ─────────────────────────── flag OFF: single-tenant unchanged ───────────────────────────
class FlagOffSingleTenantTests(_FanoutBase):
    def test_single_bound_account_routes_normally(self):
        # Exactly one bound account (Nuno shape): OFF path plans + promotes that one account.
        self.asnB.delete()
        self.acctB.delete()
        appr = self._approval()
        self._route(appr)
        plans = SignalExecutionPlan.objects.filter(approval=appr)
        self.assertEqual([p.account_id for p in plans], [self.acctA.id])
        self.assertTrue(ExecutionJob.objects.filter(account=self.acctA, job_type=JT.PLACE_ORDER).exists())

    def test_two_bound_accounts_flag_off_stays_manual_no_orders(self):
        # The exact "2nd account breaks the single-tenant router" case, with the flag OFF: nobody
        # trades (fail-safe), and NO plan/job is created — behaviour identical to before ADR-0020.
        appr = self._approval()
        self._route(appr)
        self.assertFalse(SignalExecutionPlan.objects.filter(approval=appr).exists())
        self.assertFalse(ExecutionJob.objects.exists())
        self.assertTrue(SignalAuditEvent.objects.filter(   # deferral reason recorded, as today
            approval=appr, event=SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED).exists())
