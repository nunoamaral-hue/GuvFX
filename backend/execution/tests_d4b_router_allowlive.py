"""Stream D4b — router/execution widening + per-runtime MT5_ALLOW_LIVE.

Pins the invariants:
  * DEMO EQUIVALENCE (hard merge criterion): a DEMO assignment's router selection is IDENTICAL with the D4
    flag off and on (the current demo estate, incl. 25/35/36, routes exactly as before).
  * LIVE gating: a LIVE account's assignment is routed/admitted ONLY with the flag on AND a valid §3
    authorization (flag off, or no authz ⇒ not routed). Never a global switch — admission is per account.
  * MT5_ALLOW_LIVE is per-runtime: omitted by default, emitted only for a §3-authorized LIVE account.
"""
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from execution.auto_router import _resolve_target, _account_env_admitted
from execution.bridge_config import render_bridge_env, allow_live_for_account
from execution.models import LiveExecutionAuthorization
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
DEMO = StrategyAssignment.ExecutionMode.AUTO_DEMO
ON = {"HOSTED_LIVE_EXECUTION_ENABLED": "1"}


class D4bRouterAllowLiveTests(TestCase):
    def setUp(self):
        self.op = User.objects.create_user(username="d4b", email="d4b@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.op, name="Demo", account_number="D1", is_demo=True, broker_name="DemoBroker", is_active=True)
        self.live = TradingAccount.objects.create(
            user=self.op, name="Live", account_number="L1", is_demo=False, broker_name="LiveBroker", is_active=True)
        self.strat = Strategy.objects.create(owner=self.op, name="S")

    def _asn(self, account, source):
        return StrategyAssignment.objects.create(
            strategy=self.strat, account=account, execution_mode=DEMO,
            signal_source=source, is_active=True, stage=StrategyAssignment.STAGE_LIVE)

    def _authz_live(self):
        return LiveExecutionAuthorization.objects.create(
            trading_account=self.live, user=self.op, broker_identity_snapshot={"login": "L1", "server": ""})

    # ---- DEMO EQUIVALENCE ----
    def test_demo_routing_identical_flag_off_and_on(self):
        a = self._asn(self.demo, "srcD")
        self.assertEqual(_resolve_target(DEMO, "srcD"), a)            # flag OFF (production today)
        with override_settings(**ON):
            self.assertEqual(_resolve_target(DEMO, "srcD"), a)        # flag ON — byte-identical selection

    # ---- LIVE gating via the router ----
    def test_live_not_routed_flag_off(self):
        self._asn(self.live, "srcL")
        self.assertIsNone(_resolve_target(DEMO, "srcL"))             # demo-only pre-filter excludes live

    def test_live_not_routed_flag_on_without_authorization(self):
        self._asn(self.live, "srcL")
        with override_settings(**ON):
            self.assertIsNone(_resolve_target(DEMO, "srcL"))         # admitted only when §3-authorized

    def test_live_routed_flag_on_with_authorization(self):
        a = self._asn(self.live, "srcL")
        self._authz_live()
        with override_settings(**ON):
            self.assertEqual(_resolve_target(DEMO, "srcL"), a)

    def test_live_with_authorization_still_not_routed_flag_off(self):
        self._asn(self.live, "srcL")
        self._authz_live()
        self.assertIsNone(_resolve_target(DEMO, "srcL"))            # DARK: flag off ⇒ never routed

    # ---- centralized admission predicate (shared by router + planning/promotion walls) ----
    def test_account_env_admitted(self):
        self.assertTrue(_account_env_admitted(self.demo))            # DEMO always
        self.assertFalse(_account_env_admitted(self.live))          # flag OFF
        with override_settings(**ON):
            self.assertFalse(_account_env_admitted(self.live))      # flag ON, no authz
            self._authz_live()
            self.assertTrue(_account_env_admitted(self.live))       # flag ON + authz

    # ---- per-runtime MT5_ALLOW_LIVE ----
    def test_allow_live_for_account(self):
        self.assertFalse(allow_live_for_account(self.demo))         # demo never
        self.assertFalse(allow_live_for_account(self.live))         # flag off
        with override_settings(**ON):
            self.assertFalse(allow_live_for_account(self.live))     # no authz
            self._authz_live()
            self.assertTrue(allow_live_for_account(self.live))

    def test_render_bridge_env_allow_live_param(self):
        ep = SimpleNamespace(trading_account_id=1, runtime_path="/x", port=8800, expected_login="L1",
                             expected_server="S", windows_username="u", workspace_uuid="w", is_demo=False)
        self.assertNotIn("MT5_ALLOW_LIVE", render_bridge_env(ep))                    # default: omitted
        self.assertIn("set MT5_ALLOW_LIVE=1", render_bridge_env(ep, allow_live=True))
