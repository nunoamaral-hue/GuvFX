"""LIVE READ-ONLY MONITORING — backend read-firewall DEMO/LIVE environment verification (invariant 5).

``verify_snapshot_identity(require_environment=True)`` must refuse a read whose observed terminal environment
(trade_mode) disagrees with the account's expected environment, while:
  * staying byte-identical when the bridge did not report trade_mode (observed_trade_mode is None — an older
    bridge not yet redeployed; this is what makes the backend safe to deploy BEFORE any bridge redeploy);
  * staying byte-identical for every existing caller (require_environment defaults False);
  * never weakening the login(EXACT) / server(casefold) checks that already bind the tenant.

RULE 11: includes positive controls (a matching environment PASSES) so the refusals are not vacuous.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from execution import snapshot_transport as ST
from trading.models import TradingAccount

User = get_user_model()

LIVE_TM = 2   # REAL
CONTEST_TM = 1
DEMO_TM = 0


class EnvironmentFirewallTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="m", email="m@x.invalid", password="x")
        # Free-text broker_name (no BrokerServer) => account_environment classifies purely by is_demo.
        self.live = TradingAccount.objects.create(
            user=self.u, name="Live", account_number="55442", is_demo=False, broker_name="TradersWay")
        self.demo = TradingAccount.objects.create(
            user=self.u, name="Demo", account_number="500123", is_demo=True, broker_name="DemoBroker")

    # ── expected_is_demo tri-state ──
    def test_expected_is_demo_classifies(self):
        self.assertIs(ST.expected_is_demo(self.demo), True)
        self.assertIs(ST.expected_is_demo(self.live), False)

    # ── LIVE account ──
    def test_live_account_live_terminal_passes(self):
        # Positive control: observed LIVE matches expected LIVE.
        r = ST.verify_snapshot_identity(self.live, "55442", "TradersWay-Live",
                                        observed_trade_mode=LIVE_TM, require_environment=True)
        self.assertTrue(r.ok, r.reason_code)
        self.assertEqual(r.reason_code, ST.ID_OK)

    def test_live_account_demo_terminal_refused(self):
        # A LIVE-classified account observed on a DEMO terminal must be refused (drift / wrong env).
        r = ST.verify_snapshot_identity(self.live, "55442", "TradersWay-Live",
                                        observed_trade_mode=DEMO_TM, require_environment=True)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, ST.ID_ENVIRONMENT_MISMATCH)

    # ── DEMO account ──
    def test_demo_account_demo_terminal_passes(self):
        r = ST.verify_snapshot_identity(self.demo, "500123", "DemoBroker-Demo",
                                        observed_trade_mode=DEMO_TM, require_environment=True)
        self.assertTrue(r.ok, r.reason_code)

    def test_demo_account_live_terminal_refused(self):
        # The symmetric guard: a DEMO-classified account must never read a LIVE/contest terminal.
        r = ST.verify_snapshot_identity(self.demo, "500123", "DemoBroker-Demo",
                                        observed_trade_mode=LIVE_TM, require_environment=True)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, ST.ID_ENVIRONMENT_MISMATCH)

    def test_contest_counts_as_non_demo(self):
        r = ST.verify_snapshot_identity(self.demo, "500123", "DemoBroker-Demo",
                                        observed_trade_mode=CONTEST_TM, require_environment=True)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, ST.ID_ENVIRONMENT_MISMATCH)

    # ── backward-compatibility / fail-open-on-unknown ──
    def test_missing_observed_trade_mode_skips_check(self):
        # Pre-redeploy bridge returns no trade_mode -> environment check SKIPPED (backend-safe to deploy first).
        r = ST.verify_snapshot_identity(self.live, "55442", "TradersWay-Live",
                                        observed_trade_mode=None, require_environment=True)
        self.assertTrue(r.ok, r.reason_code)

    def test_require_environment_off_is_byte_identical(self):
        # Every existing caller (require_environment defaults False) is unaffected even on a mismatch.
        r = ST.verify_snapshot_identity(self.live, "55442", "TradersWay-Live", observed_trade_mode=DEMO_TM)
        self.assertTrue(r.ok, r.reason_code)

    # ── environment check never masks the load-bearing tenant checks ──
    def test_login_mismatch_still_refused_with_environment_on(self):
        r = ST.verify_snapshot_identity(self.live, "99999", "TradersWay-Live",
                                        observed_trade_mode=LIVE_TM, require_environment=True)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, ST.ID_LOGIN_MISMATCH)   # login EXACT gate runs BEFORE the env branch
