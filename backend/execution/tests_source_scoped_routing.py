"""SIGNAL-SOURCE-SCOPED AUTO-ROUTING — tests for auto_router._resolve_target(mode, source).

Proves the source-scoping is back-compatible and fail-closed:
  * Legacy single UNBOUND assignment still resolves for any source (Wayond path unchanged).
  * Two source-bound strategies resolve independently to their own assignment.
  * A bound-but-unclaiming source never hijacks another strategy's assignment.
  * A PAUSED bound assignment resolves to None and does NOT fall back to the unbound one
    (disabling one signal-copy strategy stops it — it never re-routes its signals).
  * Ambiguity (>1 active bound, or >1 active unbound) fails closed to None.
  * effective_mode still returns AUTO_DEMO for the fully-armed single-Wayond config.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution import auto_router
from execution.auto_router import _resolve_target, effective_mode, MODE_AUTO_DEMO, MODE_MANUAL
from execution.models import ExecutionControl, SignalSourceConfig
from signal_intake.models import ParserProfile, PendingSignalApproval, SignalProvider
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
AM = StrategyAssignment.ExecutionMode
DEMO = AM.AUTO_DEMO


class ResolveTargetSourceScopingTests(TestCase):
    def setUp(self):
        self.op = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.op, name="Demo", account_number="D1", is_demo=True,
        )
        self.stra = Strategy.objects.create(owner=self.op, name="Strat A")
        self.strb = Strategy.objects.create(owner=self.op, name="Strat B")

    def _asn(self, strategy, *, source="", active=True, mode=DEMO):
        return StrategyAssignment.objects.create(
            strategy=strategy, account=self.demo, execution_mode=mode,
            signal_source=source, is_active=active, stage=StrategyAssignment.STAGE_LIVE,
        )

    def test_legacy_unbound_resolves_for_any_source(self):
        a = self._asn(self.stra)  # unbound (signal_source="")
        self.assertEqual(_resolve_target(DEMO, "wayond"), a)
        self.assertEqual(_resolve_target(DEMO, ""), a)
        self.assertEqual(_resolve_target(DEMO, "ti_signals"), a)

    def test_two_bound_strategies_resolve_independently(self):
        a = self._asn(self.stra, source="wayond")
        b = self._asn(self.strb, source="ti_signals")
        self.assertEqual(_resolve_target(DEMO, "wayond"), a)
        self.assertEqual(_resolve_target(DEMO, "ti_signals"), b)

    def test_bound_source_does_not_hijack_unrelated_source(self):
        # Only a ti_signals-bound assignment exists; a signal from an unrelated, unclaimed
        # source must NOT resolve to it (and there is no unbound fallback) → None.
        self._asn(self.strb, source="ti_signals")
        self.assertIsNone(_resolve_target(DEMO, "some_other_source"))

    def test_bound_plus_unbound_each_route_correctly(self):
        legacy = self._asn(self.stra)                    # unbound (e.g. current Wayond)
        wim = self._asn(self.strb, source="ti_signals")  # bound (new WIM)
        self.assertEqual(_resolve_target(DEMO, "ti_signals"), wim)
        self.assertEqual(_resolve_target(DEMO, "wayond"), legacy)   # unclaimed → unbound fallback

    def test_paused_bound_stops_and_does_not_reroute(self):
        legacy = self._asn(self.stra)                                  # unbound, active
        self._asn(self.strb, source="ti_signals", active=False)        # WIM paused
        # ti_signals is CLAIMED (a bound assignment exists) but paused → None, NOT the unbound.
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))
        # The legacy unbound path is unaffected.
        self.assertEqual(_resolve_target(DEMO, "wayond"), legacy)

    def test_disabling_bound_via_stage_does_not_reroute(self):
        # Claim detection is unscoped: a bound assignment pulled out of the routable set by
        # stage (LIVE→TEST) keeps the source CLAIMED → None, never the unbound Wayond assignment.
        legacy = self._asn(self.stra)  # unbound Wayond, active
        StrategyAssignment.objects.create(
            strategy=self.strb, account=self.demo, execution_mode=DEMO,
            signal_source="ti_signals", is_active=True, stage=StrategyAssignment.STAGE_TEST,
        )
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))
        self.assertEqual(_resolve_target(DEMO, "wayond"), legacy)

    def test_disabling_bound_via_execution_mode_does_not_reroute(self):
        legacy = self._asn(self.stra)  # unbound Wayond, active
        StrategyAssignment.objects.create(  # bound to ti_signals but downgraded to MANUAL
            strategy=self.strb, account=self.demo, execution_mode=AM.MANUAL,
            signal_source="ti_signals", is_active=True, stage=StrategyAssignment.STAGE_LIVE,
        )
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))
        self.assertEqual(_resolve_target(DEMO, "wayond"), legacy)

    def test_inactive_account_is_never_targeted(self):
        # A deactivated account is unroutable regardless of the assignment flag (reliable stop).
        self.demo.is_active = False
        self.demo.save(update_fields=["is_active"])
        self._asn(self.stra, source="ti_signals")  # active bound, but account is inactive
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))

    def test_ambiguous_bound_fails_closed(self):
        self._asn(self.stra, source="ti_signals")
        self._asn(self.strb, source="ti_signals")
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))

    def test_ambiguous_unbound_fails_closed(self):
        self._asn(self.stra)
        self._asn(self.strb)
        self.assertIsNone(_resolve_target(DEMO, "wayond"))

    def test_second_enabled_source_does_not_borrow_unbound_assignment(self):
        # Fail-OPEN guard: with >1 auto-source enabled, an enabled-but-unbound source must NOT
        # be misrouted onto the legacy unbound (Wayond) assignment.
        a = self._asn(self.stra)  # unbound legacy (Wayond), active
        SignalSourceConfig.objects.create(source="wayond", auto_demo_execution_enabled=True)
        SignalSourceConfig.objects.create(source="ti_signals", auto_demo_execution_enabled=True)
        self.assertIsNone(_resolve_target(DEMO, "ti_signals"))  # no misroute to Wayond
        self.assertIsNone(_resolve_target(DEMO, "wayond"))      # still-unbound wayond also closes
        # Binding Wayond restores it (the correct multi-source config).
        a.signal_source = "wayond"
        a.save(update_fields=["signal_source"])
        self.assertEqual(_resolve_target(DEMO, "wayond"), a)

    def test_single_enabled_source_still_uses_unbound(self):
        a = self._asn(self.stra)  # unbound legacy
        SignalSourceConfig.objects.create(source="wayond", auto_demo_execution_enabled=True)
        self.assertEqual(_resolve_target(DEMO, "wayond"), a)  # single source → unbound serves it

    def test_bound_wayond_not_disarmed_by_stray_nonroutable_binding(self):
        # Once Wayond is bound, a stray non-routable assignment also tagged "wayond" (e.g. a
        # leftover MANUAL) does NOT disarm the real routable arm.
        real = self._asn(self.stra, source="wayond")
        StrategyAssignment.objects.create(
            strategy=self.strb, account=self.demo, execution_mode=AM.MANUAL,
            signal_source="wayond", is_active=True, stage=StrategyAssignment.STAGE_LIVE,
        )
        self.assertEqual(_resolve_target(DEMO, "wayond"), real)

    def test_non_demo_account_never_targeted(self):
        live = TradingAccount.objects.create(
            user=self.op, name="Live", account_number="L1", is_demo=False,
        )
        StrategyAssignment.objects.create(
            strategy=self.stra, account=live, execution_mode=DEMO,
            signal_source="wayond", is_active=True, stage=StrategyAssignment.STAGE_LIVE,
        )
        self.assertIsNone(_resolve_target(DEMO, "wayond"))


class StrategyDisplayNameSourceAwareTests(TestCase):
    """The notification card's strategy name must be resolved by the plan's SOURCE when an account
    hosts more than one AUTO_DEMO strategy (Wayond + WIM), else both would mislabel."""

    def setUp(self):
        self.op = User.objects.create_user(username="dn", email="dn@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.op, name="Demo", account_number="DN1", is_demo=True,
        )
        way = Strategy.objects.create(owner=self.op, name="Wayond Auto Demo")
        wim = Strategy.objects.create(owner=self.op, name="Wayond WIM Strategy")
        for strat, src in ((way, "wayond"), (wim, "ti_signals")):
            StrategyAssignment.objects.create(
                strategy=strat, account=self.demo, execution_mode=DEMO,
                signal_source=src, is_active=True, stage=StrategyAssignment.STAGE_LIVE,
            )

    def _plan(self, source):
        from types import SimpleNamespace
        return SimpleNamespace(account=self.demo, source=source)

    def test_card_labels_by_source(self):
        from execution.notifications.contracts import _resolve_strategy_display_name
        self.assertEqual(_resolve_strategy_display_name(self._plan("wayond")), "Wayond Auto Demo")
        self.assertEqual(_resolve_strategy_display_name(self._plan("ti_signals")), "Wayond WIM Strategy")


class EffectiveModeBackCompatTests(TestCase):
    """The fully-armed single-Wayond config (unbound assignment) still auto-demos."""

    def setUp(self):
        self.op = User.objects.create_user(username="op2", email="op2@x.invalid", password="x")
        call_command("provision_auto_shadow")
        self.demo = TradingAccount.objects.create(
            user=self.op, name="Demo", account_number="D9", is_demo=True,
        )
        self.parser = ParserProfile.objects.create(
            slug="wayond_v1", certification_level=ParserProfile.CertificationLevel.MEDIUM,
        )
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="-100999",
            parser_profile=self.parser, status=SignalProvider.Status.ARMED,
        )
        SignalSourceConfig.objects.create(
            source="wayond", auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"),
        )
        self.strategy = Strategy.objects.create(owner=self.op, name="Wayond Auto Demo")
        # Unbound AUTO_DEMO assignment — exactly today's prod shape.
        StrategyAssignment.objects.create(
            strategy=self.strategy, account=self.demo, execution_mode=DEMO,
            is_active=True, stage=StrategyAssignment.STAGE_LIVE,
        )
        ctrl = ExecutionControl.get_solo()
        ctrl.signal_execution_mode = ExecutionControl.SignalExecutionMode.DEMO
        ctrl.auto_execution_enabled = True
        ctrl.kill_switch_engaged = False
        ctrl.save()

    def _approval(self):
        return PendingSignalApproval.objects.create(
            source="wayond", message_id="m1", provider=self.provider, symbol="EURUSD",
            direction="BUY", entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900"], status=PendingSignalApproval.Status.PENDING_APPROVAL,
        )

    def test_single_unbound_wayond_still_arms_auto_demo(self):
        mode, reason = effective_mode(self._approval())
        self.assertEqual(mode, MODE_AUTO_DEMO)
        self.assertEqual(reason, "armed")

    def test_unknown_source_without_config_stays_manual(self):
        appr = PendingSignalApproval.objects.create(
            source="ti_signals", message_id="m2", provider=self.provider, symbol="XAUUSD",
            direction="BUY", entry="4020.03", stop_loss="4017.61", take_profit="4023.67",
            take_profits=["4023.67"], status=PendingSignalApproval.Status.PENDING_APPROVAL,
        )
        mode, reason = effective_mode(appr)
        self.assertEqual(mode, MODE_MANUAL)
        self.assertEqual(reason, "source_not_enabled")
