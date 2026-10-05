"""Stream D3 — LIVE monitoring (DARK). These tests pin the SAFETY INVARIANTS of the monitoring split:

  * The execution fact is UNCHANGED — a LIVE account is never execution-eligible (``evaluate().eligible``
    stays False at condition 11), regardless of the D3 flag. Monitoring NEVER authorises an order.
  * DARK — with the flag OFF everything is byte-identical (matcher demo-only, monitoring not eligible,
    display unchanged).
  * Environment-aware MATCH — the matcher is symmetric (observed env must EQUAL expected env), fixing the
    pre-D3 asymmetry; a LIVE account may match only when explicitly opted in.

DB-free (SimpleTestCase + stubs + override_settings): the readiness providers, the pure matcher, the pure
producer match, and the pure trading-state projection all read plain attributes, so no database is needed.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from execution.readiness import (
    PERSISTENT_WORKSPACE, PersistentWorkspaceProvider, RW_MONITORING_DISABLED, RW_MONITORING_OK,
    RW_REAL_ACCOUNT_NOT_ENABLED, RW_NOT_CONFIRMED, RW_ACTIVE_ACCOUNT_MISMATCH, RW_SUBSYSTEM_DISABLED,
    evaluate_monitoring_readiness)
from hosted_workspace.matching import (
    ExpectedAccount, WorkspaceObservation, evaluate_active_account_match)
from hosted_workspace.producer import RawWorkspaceSnapshot, _account_match
from trading.trading_state import resolve_trading_state, CONNECTED_MONITORING_EXEC_UNAUTHORIZED

ON = {"HOSTED_PERSISTENT_MT5_ENABLED": "1", "HOSTED_LIVE_MONITORING_ENABLED": "1"}


def _ws(**kw):
    base = dict(proj_connected=True, proj_account_match=True, last_decision_at=timezone.now(),
                proj_margin_mode=2, capability_recovery_count=0, proj_trade_allowed=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _acct(is_demo, ws, **kw):
    base = dict(readiness_provider=PERSISTENT_WORKSPACE, mt5_instance_id=None, disconnected_at=None,
                is_active=True, is_demo=is_demo, broker_server=None, hosted_workspace=ws,
                workspace_confirmed_at=timezone.now())
    base.update(kw)
    return SimpleNamespace(**base)


# ───────────────────────── symmetric matcher ─────────────────────────
class D3MatcherSymmetryTests(SimpleTestCase):
    def _obs(self, trade_mode):
        return WorkspaceObservation(process_running=True, ipc_available=True, connected=True,
                                    trade_allowed=True, login="100", server="S", trade_mode=trade_mode)

    def test_demo_expected_demo_observed_ok(self):   # byte-identical pre-D3
        d = evaluate_active_account_match(self._obs(0), ExpectedAccount("100", "S", is_demo=True))
        self.assertTrue(d.ok)

    def test_demo_expected_live_observed_mismatch(self):   # byte-identical pre-D3
        d = evaluate_active_account_match(self._obs(2), ExpectedAccount("100", "S", is_demo=True))
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, "classification_mismatch")

    def test_live_expected_demo_observed_mismatch(self):   # NEW symmetric fix (pre-D3 wrongly passed)
        d = evaluate_active_account_match(self._obs(0), ExpectedAccount("100", "S", is_demo=False, allow_live=True))
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, "classification_mismatch")

    def test_live_expected_live_observed_allowed(self):    # NEW — LIVE identity match
        d = evaluate_active_account_match(self._obs(2), ExpectedAccount("100", "S", is_demo=False, allow_live=True))
        self.assertTrue(d.ok)

    def test_live_expected_live_observed_not_opted_in(self):
        d = evaluate_active_account_match(self._obs(2), ExpectedAccount("100", "S", is_demo=False, allow_live=False))
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, "live_execution_not_authorised")


# ───────────────────────── producer env plumbing ─────────────────────────
class D3ProducerEnvTests(SimpleTestCase):
    def _snap(self, trade_mode, **kw):
        return RawWorkspaceSnapshot(expected_login="100", expected_server="S", observed_login="100",
                                    observed_server="S", observed_trade_mode=trade_mode, **kw)

    def test_default_none_is_demo_only(self):   # every pre-D3 caller: byte-identical demo-only
        self.assertTrue(_account_match(self._snap(0)))                       # demo terminal matches
        self.assertFalse(_account_match(self._snap(2)))                      # live terminal does NOT match

    def test_live_opt_in_matches_live_terminal(self):
        snap = self._snap(2, expected_is_demo=False, expected_allow_live=True)
        self.assertTrue(_account_match(snap))

    def test_live_opt_in_rejects_demo_terminal(self):   # symmetric fail-closed
        snap = self._snap(0, expected_is_demo=False, expected_allow_live=True)
        self.assertFalse(_account_match(snap))


# ───────────────────────── monitoring vs execution ─────────────────────────
class D3MonitoringReadinessTests(SimpleTestCase):
    def test_both_flags_off_not_eligible(self):   # master subsystem off ⇒ disabled before the D3 check
        acct = _acct(is_demo=False, ws=_ws())
        d = PersistentWorkspaceProvider().evaluate_monitoring(acct)
        self.assertFalse(d.eligible)
        self.assertEqual(d.reason_code, RW_SUBSYSTEM_DISABLED)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")   # master on, D3 monitoring flag OFF
    def test_d3_flag_off_not_eligible(self):
        acct = _acct(is_demo=False, ws=_ws())
        d = PersistentWorkspaceProvider().evaluate_monitoring(acct)
        self.assertFalse(d.eligible)
        self.assertEqual(d.reason_code, RW_MONITORING_DISABLED)

    # The strongest invariant: even with EVERY execution flag ON, a LIVE account is monitoring-eligible yet
    # NEVER execution-eligible — it is blocked precisely at the demo-only wall (condition 11). Monitoring and
    # execution are orthogonal; monitoring never authorises an order.
    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_LIVE_MONITORING_ENABLED="1",
                       HOSTED_MT5_EXECUTION_ENABLED="1")
    def test_live_account_monitoring_eligible_but_execution_blocked(self):
        acct = _acct(is_demo=False, ws=_ws())   # LIVE, connected + matched + confirmed + fresh
        mon = evaluate_monitoring_readiness(acct)
        self.assertTrue(mon.eligible)
        self.assertEqual(mon.reason_code, RW_MONITORING_OK)
        exe = PersistentWorkspaceProvider().evaluate(acct)
        self.assertFalse(exe.eligible)
        self.assertEqual(exe.reason_code, RW_REAL_ACCOUNT_NOT_ENABLED)   # blocked for being LIVE, not a flag

    @override_settings(**ON)
    def test_monitoring_fail_closed_unmatched(self):
        acct = _acct(is_demo=False, ws=_ws(proj_account_match=False))
        d = evaluate_monitoring_readiness(acct)
        self.assertFalse(d.eligible)
        self.assertEqual(d.reason_code, RW_ACTIVE_ACCOUNT_MISMATCH)

    @override_settings(**ON)
    def test_monitoring_requires_confirmation(self):
        acct = _acct(is_demo=False, ws=_ws(), workspace_confirmed_at=None)
        d = evaluate_monitoring_readiness(acct)
        self.assertFalse(d.eligible)
        self.assertEqual(d.reason_code, RW_NOT_CONFIRMED)

    @override_settings(**ON)
    def test_demo_account_also_monitoring_eligible(self):   # env-agnostic
        acct = _acct(is_demo=True, ws=_ws())
        self.assertTrue(evaluate_monitoring_readiness(acct).eligible)


# ───────────────────────── display state ─────────────────────────
class D3TradingStateTests(SimpleTestCase):
    def test_flag_off_live_is_legacy_attention(self):
        acct = _acct(is_demo=False, ws=_ws(), is_active=True)
        state = resolve_trading_state(acct)["state"]
        self.assertNotEqual(state, CONNECTED_MONITORING_EXEC_UNAUTHORIZED)   # legacy display unchanged

    @override_settings(**ON)
    def test_flag_on_live_connected_matched_shows_monitoring(self):
        acct = _acct(is_demo=False, ws=_ws())
        self.assertEqual(resolve_trading_state(acct)["state"], CONNECTED_MONITORING_EXEC_UNAUTHORIZED)

    @override_settings(**ON)
    def test_flag_on_demo_unaffected(self):
        acct = _acct(is_demo=True, ws=_ws(), is_active=False)
        # A demo account never enters the D3 branch — its display is the normal (non-monitoring) mapping.
        self.assertNotEqual(resolve_trading_state(acct)["state"], CONNECTED_MONITORING_EXEC_UNAUTHORIZED)
