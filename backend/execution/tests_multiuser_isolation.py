"""ADR-0020 multi-user tenant isolation — regressions for the Wayond fan-out isolation gaps found by
the Phase-2 adversarial verification (Beta Product Enablement). Execution stays DARK; these prove the
isolation invariants hold when ``MULTI_ACCOUNT_ROUTING_ENABLED`` is ON.

  1. Fan-out ROUTING: a CONFIGURED source (one with a SignalSourceConfig row) never falls through to an
     UNBOUND legacy catch-all — so one tenant deleting/rebinding its bound assignment can never re-route
     the source onto ANOTHER tenant's unbound account, and a non-routable tagged assignment can never
     suppress delivery via the unbound path. The historical unbound single-Wayond route (UNCONFIGURED
     source) is preserved.
  2. ORDER TARGETING: fan-out IMPLIES terminal-node enforcement — a fanned account without a dedicated
     ACTIVE node fails closed at promotion, so no NULL-node PLACE_ORDER (which the shared legacy worker
     would claim and execute on another tenant's terminal) is ever created.
  3. WIN-CARD: plan/leg resolution is scoped by ACCOUNT, so a shared correlation_id can never leak another
     tenant's leg prices / profit into a card.
"""
import os
import types
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.auto_router import _resolve_target, _resolve_targets
from execution.models import (ProposedOrderLeg, SignalExecutionPlan, SignalSourceConfig, TerminalNode)
from execution.notifications.contracts import resolve_leg_evidence, resolve_signal_linkage
from execution.signal_promotion import PromotionRejected, promote_plan_to_shadow_jobs
from signal_intake.models import PendingSignalApproval
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
AM = StrategyAssignment.ExecutionMode
DEMO = AM.AUTO_DEMO
FANOUT = {"MULTI_ACCOUNT_ROUTING_ENABLED": "1"}


def _user(n):
    return User.objects.create_user(username=n, email=f"{n}@x.invalid", password="x")


def _acct(user, num, node=None):
    return TradingAccount.objects.create(
        user=user, name=num, account_number=num, is_demo=True, terminal_node=node,
        broker_name="DemoBroker")


class FanoutRoutingIsolationTests(TestCase):
    def setUp(self):
        self.ua, self.ub = _user("ta"), _user("tb")
        self.aa, self.ab = _acct(self.ua, "A1"), _acct(self.ub, "B1")
        self.sa = Strategy.objects.create(owner=self.ua, name="SA")
        self.sb = Strategy.objects.create(owner=self.ub, name="SB")

    def _asn(self, strat, acct, *, source="", mode=DEMO, active=True):
        return StrategyAssignment.objects.create(
            strategy=strat, account=acct, execution_mode=mode, signal_source=source,
            is_active=active, stage=StrategyAssignment.STAGE_LIVE)

    @override_settings(**FANOUT)
    def test_configured_source_never_falls_to_unbound_after_bound_deleted(self):
        # ti_signals is CONFIGURED (has a config row). Tenant A is bound to it; Tenant B holds an
        # UNBOUND catch-all. Deleting A's assignment must NOT re-route ti_signals to B's account.
        SignalSourceConfig.objects.create(source="ti_signals", auto_demo_execution_enabled=True)
        a = self._asn(self.sa, self.aa, source="ti_signals")
        self._asn(self.sb, self.ab, source="")                       # tenant B unbound catch-all
        self.assertEqual(_resolve_target(DEMO, "ti_signals"), a)     # bound → A
        a.delete()
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))       # configured → NOT B's unbound
        self.assertEqual(_resolve_targets(DEMO, "ti_signals"), [])   # fan-out path too

    @override_settings(**FANOUT)
    def test_nonroutable_tag_cannot_suppress_and_unbound_never_gets_configured(self):
        SignalSourceConfig.objects.create(source="ti_signals", auto_demo_execution_enabled=True)
        self._asn(self.sb, self.ab, source="")                       # tenant B unbound
        # A CONFIGURED source never uses the unbound path → B's catch-all never receives ti_signals.
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))
        # Tenant A tags a NON-routable (MANUAL) ti_signals assignment — still nothing leaks to B.
        self._asn(self.sa, self.aa, source="ti_signals", mode=AM.MANUAL)
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))

    def test_unconfigured_legacy_source_still_uses_unbound(self):
        # No config row for 'wayond' → the historical unbound single-Wayond route is preserved (no
        # behaviour change for the legacy path).
        b = self._asn(self.sb, self.ab, source="")
        self.assertEqual(_resolve_target(DEMO, "wayond"), b)


class FanoutNodeEnforcementTests(TestCase):
    def setUp(self):
        self.u = _user("op")
        SignalSourceConfig.objects.create(source="wayond", auto_demo_execution_enabled=True)

    def _plan(self, account, mid="p1"):
        approval = PendingSignalApproval.objects.create(
            source="wayond", message_id=mid, symbol="EURUSD", direction="BUY",
            stop_loss="1.0800", take_profits=["1.0900"], status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=approval, account=account, source="wayond", message_id=mid, symbol="EURUSD",
            direction="BUY", stop_loss="1.0800", is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PLANNED)
        ProposedOrderLeg.objects.create(
            plan=plan, leg_index=1, take_profit="1.0900", stop_loss="1.0800",
            lot_size=Decimal("0.01"), status=ProposedOrderLeg.Status.PLANNED)
        return plan

    @override_settings(**FANOUT)
    def test_fanout_on_unassigned_account_blocked(self):
        acct = _acct(self.u, "N0", node=None)
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_shadow_jobs(self._plan(acct), actor=self.u)
        self.assertEqual(cm.exception.code, "account_node_unassigned")

    @override_settings(**FANOUT)
    def test_fanout_on_draining_node_blocked(self):
        node = TerminalNode.objects.create(hostname="drain", status=TerminalNode.Status.DRAINING)
        acct = _acct(self.u, "ND", node=node)
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_shadow_jobs(self._plan(acct), actor=self.u)
        self.assertEqual(cm.exception.code, "node_not_active")

    @override_settings(**FANOUT)
    def test_fanout_on_active_node_promotes(self):
        node = TerminalNode.objects.create(hostname="n1", status=TerminalNode.Status.ACTIVE)
        acct = _acct(self.u, "N1", node=node)
        jobs = promote_plan_to_shadow_jobs(self._plan(acct), actor=self.u)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].terminal_node_id, node.id)

    @override_settings(**FANOUT)
    def test_fanout_on_demo_path_unassigned_account_blocked(self):
        # The REAL money path (promote_plan_to_demo_jobs, global mode DEMO) is node-gated too — a fanned
        # un-noded account cannot create a NULL-node PLACE_ORDER the shared worker would claim.
        from execution.models import ExecutionControl
        from execution.signal_promotion import promote_plan_to_demo_jobs
        ctrl = ExecutionControl.get_solo()
        ctrl.signal_execution_mode = ExecutionControl.SignalExecutionMode.DEMO
        ctrl.save(update_fields=["signal_execution_mode"])
        acct = _acct(self.u, "ND0", node=None)
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_demo_jobs(self._plan(acct), actor=self.u)
        self.assertEqual(cm.exception.code, "account_node_unassigned")

    def test_fanout_off_unassigned_account_still_promotes(self):
        # Both flags OFF → legacy behaviour preserved (no node requirement) — zero regression.
        env = {k: v for k, v in os.environ.items()
               if k not in ("RISK_REQUIRE_TERMINAL_NODE", "MULTI_ACCOUNT_ROUTING_ENABLED")}
        with mock.patch.dict(os.environ, env, clear=True):
            acct = _acct(self.u, "N0", node=None)
            jobs = promote_plan_to_shadow_jobs(self._plan(acct), actor=self.u)
        self.assertEqual(len(jobs), 1)


class WinCardAccountScopingTests(TestCase):
    def setUp(self):
        self.ua, self.ub = _user("wa"), _user("wb")
        self.aa, self.ab = _acct(self.ua, "WA"), _acct(self.ub, "WB")

    def _plan(self, account, tp, mid, direction="BUY"):
        approval = PendingSignalApproval.objects.create(
            source="ti_signals", message_id=mid, symbol="XAUUSD", direction=direction,
            stop_loss="4000", take_profits=[tp], status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=approval, account=account, source="ti_signals", message_id=mid, symbol="XAUUSD",
            direction=direction, stop_loss="4000", is_demo=True, correlation_id="SHARED",
            signal_timestamp=timezone.now(), status=SignalExecutionPlan.Status.PLANNED)
        ProposedOrderLeg.objects.create(
            plan=plan, leg_index=1, take_profit=tp, stop_loss="4000", lot_size=Decimal("0.01"),
            status=ProposedOrderLeg.Status.PLANNED)
        return plan

    def test_leg_evidence_scoped_to_the_cards_own_account(self):
        # Two tenants' plans share a correlation_id (a fanned-out signal). Each card must resolve ITS
        # OWN account's plan — never the other tenant's leg prices.
        self._plan(self.aa, tp="4100", mid="a")      # tenant A: TP 4100
        self._plan(self.ab, tp="4200", mid="b")      # tenant B: TP 4200 — SAME correlation_id
        cardA = types.SimpleNamespace(account_id=self.aa.id, close_time=None, comment="")
        cardB = types.SimpleNamespace(account_id=self.ab.id, close_time=None, comment="")
        evA = resolve_leg_evidence("SHARED", cardA)
        evB = resolve_leg_evidence("SHARED", cardB)
        self.assertIn("4100", evA.get("take_profits", []))
        self.assertNotIn("4200", evA.get("take_profits", []))        # A's card NEVER shows B's TP
        self.assertIn("4200", evB.get("take_profits", []))
        self.assertNotIn("4100", evB.get("take_profits", []))

    def test_signal_linkage_scoped_by_account(self):
        # Two tenants' plans share a correlation_id; the linkage must resolve the CARD's OWN account's
        # plan (signal_id + TP), never the other tenant's. Mutation-resistant: reverting the account
        # filter resolves one plan for both accounts, failing one of these.
        self._plan(self.aa, tp="4100", mid="a")
        self._plan(self.ab, tp="4200", mid="b")
        linkA = resolve_signal_linkage("SHARED", account=self.aa)
        self.assertEqual(linkA.get("signal_id"), "a")
        self.assertEqual(linkA.get("take_profit"), "4100")
        linkB = resolve_signal_linkage("SHARED", account=self.ab)
        self.assertEqual(linkB.get("signal_id"), "b")
        self.assertEqual(linkB.get("take_profit"), "4200")


class OpenTradeNodeGateTests(TestCase):
    """The manual open-trade funnel is node-gated under fan-out too (sibling of the promotion gate):
    a fanned un-noded account cannot create a NULL-node OPEN_TRADE the shared worker would claim."""
    def setUp(self):
        self.u = _user("ot")

    def _params(self, acct):
        from execution.services import OpenTradeParams
        return OpenTradeParams(
            account=acct, strategy=None, assignment=None, created_by=self.u, symbol="EURUSD",
            direction="BUY", timeframe="M1", entry_type="MARKET", entry_price=None,
            sl_price=Decimal("1.0"), tp_price=None, risk_per_trade_pct=Decimal("1.0"))

    @override_settings(**FANOUT)
    def test_fanout_on_unassigned_account_refuses_open_trade(self):
        from execution.broker_gate import ExecutionGateRefused
        from execution.models import ExecutionJob
        from execution.services import create_open_trade_job
        acct = _acct(self.u, "OT0", node=None)
        with mock.patch("execution.services.require_entitlement", return_value=None):
            with self.assertRaises(ExecutionGateRefused):
                create_open_trade_job(self._params(acct))
        self.assertEqual(ExecutionJob.objects.filter(job_type="OPEN_TRADE").count(), 0)

    def test_fanout_off_unassigned_account_creates_open_trade(self):
        # Flag OFF → no node requirement → job created (single-tenant unchanged).
        from execution.models import ExecutionJob
        from execution.services import create_open_trade_job
        env = {k: v for k, v in os.environ.items()
               if k not in ("RISK_REQUIRE_TERMINAL_NODE", "MULTI_ACCOUNT_ROUTING_ENABLED")}
        acct = _acct(self.u, "OT1", node=None)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("execution.services.require_entitlement", return_value=None):
            job = create_open_trade_job(self._params(acct))
        self.assertEqual(job.job_type, "OPEN_TRADE")
