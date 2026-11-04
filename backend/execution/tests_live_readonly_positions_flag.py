"""GFX-LIVE-POSITIONS-READONLY-001 (ADR-0072) — adversarial matrix for the LIVE read-only OPEN-POSITIONS
monitoring gate.

Maps the packet's Phase-4 test matrix. Items also covered by the broader suites are cited rather than
duplicated:
  * (5) wrong login, (6) wrong server, (7) cross-tenant, env-mismatch → ``tests_live_readonly_firewall`` +
        ``tests_per_tenant_transport`` (the backend identity firewall is UNCHANGED by this packet); a focused
        login/server positive+negative control is repeated here so this matrix is self-contained (RULE 11).

MUTATION SENSITIVITY (req 18): the suite fails if a safety guard is removed —
  * delete ``and not _live_readonly_positions_enabled()`` in fetch_positions ⇒ test_live_refused_when_flag_off
    + test_restart_replay_cannot_escalate FAIL (a flag-OFF live read would succeed);
  * weaken any execute demo gate ⇒ tests_bridge_live_readonly.test_read_flag_on_does_not_arm_execution FAIL;
  * drop the truncation disclosure ⇒ test_bounded_response_size_disclosed FAIL.
"""
import inspect
import os
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from execution import snapshot_transport as ST
from execution.bridge_config import (
    render_bridge_env,
    readonly_positions_for_account,
    _live_readonly_positions_monitoring_armed,
)
from execution.tests_bridge_symbols import _load_bridge

RO_FLAG = "MT5_LIVE_READONLY_POSITIONS_ENABLED"
MON_FLAG = "LIVE_READONLY_POSITIONS_MONITORING_ENABLED"
LIVE, DEMO = 2, 0


def _pos(ticket=1):
    p = mock.MagicMock()
    p.ticket, p.symbol, p.type, p.volume = ticket, "XAUUSD", 0, 0.10
    p.price_open, p.price_current, p.profit, p.magic, p.comment = 2000.0, 2010.0, 1.0, 0, "obs"
    p.sl, p.tp, p.time, p.time_update, p.identifier = 1990.0, 2100.0, 1_700_000_000, 1_700_000_050, 7
    return p


def _fake_mt5(trade_mode=LIVE, positions=()):
    a = mock.MagicMock()
    a.trade_mode, a.login, a.server = trade_mode, 55442, "TradersWay-Live"
    m = mock.MagicMock(name="MetaTrader5")
    m.initialize.return_value = True
    m.account_info.return_value = a
    m.positions_get.return_value = positions
    m.last_error.return_value = (0, "ok")
    return m


class BridgePositionsFlagGate(SimpleTestCase):
    """The bridge read gate — the load-bearing safety boundary."""

    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        import sys
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def test_live_served_when_flag_on(self):  # req (2)
        m = _fake_mt5(LIVE, (_pos(),))
        self._install(m)
        with mock.patch.dict(os.environ, {RO_FLAG: "1"}):
            r = self.bridge.fetch_positions("")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["trade_mode"], LIVE)
        m.order_send.assert_not_called()
        m.order_check.assert_not_called()  # req (9)(10): a read never reaches an order/order_check call

    def test_live_refused_when_flag_off(self):  # req (1) + guard (18)
        m = _fake_mt5(LIVE, (_pos(),))
        self._install(m)
        with mock.patch.dict(os.environ, {RO_FLAG: ""}):
            r = self.bridge.fetch_positions("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "account_not_demo")
        self.assertNotIn("positions", r)
        m.order_send.assert_not_called()

    def test_unknown_trade_mode_fails_closed(self):  # None => treated non-demo => refused when flag off
        m = _fake_mt5(LIVE, (_pos(),))
        m.account_info.return_value.trade_mode = None
        self._install(m)
        with mock.patch.dict(os.environ, {RO_FLAG: ""}):
            r = self.bridge.fetch_positions("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "account_not_demo")

    def test_restart_replay_cannot_escalate(self):  # req (17): authority is env-fresh, never cached/persisted
        m = _fake_mt5(LIVE, (_pos(),))
        self._install(m)
        with mock.patch.dict(os.environ, {RO_FLAG: ""}):
            self.assertFalse(self.bridge.fetch_positions("")["ok"])      # OFF: refused
        with mock.patch.dict(os.environ, {RO_FLAG: "1"}):
            self.assertTrue(self.bridge.fetch_positions("")["ok"])       # ON: served
        with mock.patch.dict(os.environ, {RO_FLAG: ""}):
            self.assertFalse(self.bridge.fetch_positions("")["ok"])      # OFF again: refused (no cached ON)

    def test_provenance_allowlist(self):  # req (16): response top-level fields are exactly the allowlist
        m = _fake_mt5(LIVE, (_pos(),))
        self._install(m)
        with mock.patch.dict(os.environ, {RO_FLAG: "1"}):
            r = self.bridge.fetch_positions("")
        self.assertEqual(
            set(r.keys()),
            {"ok", "positions", "count", "total_count", "truncated", "observed_at",
             "account_login", "account_server", "trade_mode"},
        )
        self.assertIsInstance(r["observed_at"], str)

    def test_bounded_response_size_disclosed(self):  # req (15) + guard (18)
        m = _fake_mt5(LIVE, (_pos(1), _pos(2), _pos(3)))
        self._install(m)
        self.addCleanup(setattr, self.bridge, "_READONLY_POSITIONS_MAX", self.bridge._READONLY_POSITIONS_MAX)
        self.bridge._READONLY_POSITIONS_MAX = 2
        with mock.patch.dict(os.environ, {RO_FLAG: "1"}):
            r = self.bridge.fetch_positions("")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 2)          # capped list
        self.assertEqual(r["total_count"], 3)    # true number disclosed
        self.assertTrue(r["truncated"])          # never a silent drop

    def test_read_failure_is_unavailable_not_empty(self):  # req (14)
        m = _fake_mt5(LIVE)
        m.positions_get.return_value = None
        self._install(m)
        with mock.patch.dict(os.environ, {RO_FLAG: "1"}):
            r = self.bridge.fetch_positions("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "positions_get_failed")


class BridgeMethodEnforcement(SimpleTestCase):
    """req (8): HTTP method enforcement is STRUCTURAL and precedes terminal interaction."""

    def setUp(self):
        self.bridge = _load_bridge()

    def test_positions_is_get_only(self):
        get_src = inspect.getsource(self.bridge.OHLCRequestHandler.do_GET)
        post_src = inspect.getsource(self.bridge.OHLCRequestHandler.do_POST)
        self.assertIn("/mt5/positions", get_src)          # read is a GET route
        self.assertNotIn("/mt5/positions", post_src)      # never a POST route
        # token auth precedes any MT5 touch on both verbs (fail-closed)
        self.assertIn("_validate_token", get_src)
        self.assertIn("_validate_token", post_src)
        # PUT/PATCH/DELETE are not implemented => BaseHTTPRequestHandler answers 501, never a terminal call
        for verb in ("do_PUT", "do_PATCH", "do_DELETE"):
            self.assertFalse(hasattr(self.bridge.OHLCRequestHandler, verb), verb)

    def test_positions_get_passes_only_symbol(self):  # req (9)(10): the GET cannot carry an order/SL-TP payload
        get_src = inspect.getsource(self.bridge.OHLCRequestHandler.do_GET)
        self.assertIn("fetch_positions(symbol)", get_src)
        # fetch_positions itself only accepts a symbol — no order/mutation parameter can enter it.
        self.assertEqual(list(inspect.signature(self.bridge.fetch_positions).parameters), ["symbol"])


class RendererAndDecision(SimpleTestCase):
    """The config renderer flag + the fail-closed, execution-independent per-tenant decision."""

    EP = SimpleNamespace(trading_account_id=1, runtime_path="/x", port=8800, expected_login="L1",
                         expected_server="S", windows_username="u", workspace_uuid="w", is_demo=False)

    def test_renderer_default_omits_flag(self):
        self.assertNotIn("MT5_LIVE_READONLY_POSITIONS_ENABLED", render_bridge_env(self.EP))

    def test_renderer_emits_flag_independent_of_execution(self):  # req (11)
        ro = render_bridge_env(self.EP, readonly_positions=True)
        self.assertIn("set MT5_LIVE_READONLY_POSITIONS_ENABLED=1", ro)
        self.assertNotIn("MT5_ALLOW_LIVE", ro)      # read authority NEVER implies execution authority

    def test_renderer_both_flags_orthogonal(self):
        both = render_bridge_env(self.EP, allow_live=True, live_expiry_epoch=1_893_456_000,
                                 readonly_positions=True)
        self.assertIn("set MT5_ALLOW_LIVE=1", both)
        self.assertIn("set MT5_LIVE_READONLY_POSITIONS_ENABLED=1", both)

    def test_decision_fail_closed(self):
        obj = SimpleNamespace()
        with mock.patch("trading.account_policy.is_live_environment", return_value=True):
            self.assertFalse(readonly_positions_for_account(obj))                  # armed flag OFF => deny
            with override_settings(LIVE_READONLY_POSITIONS_MONITORING_ENABLED="1"):
                self.assertTrue(readonly_positions_for_account(obj))               # LIVE + armed => allow
        with mock.patch("trading.account_policy.is_live_environment", return_value=False):
            with override_settings(LIVE_READONLY_POSITIONS_MONITORING_ENABLED="1"):
                self.assertFalse(readonly_positions_for_account(obj))              # DEMO never, even armed

    def test_monitoring_authority_independent_of_execution_flag(self):  # req (11)
        with override_settings(HOSTED_LIVE_EXECUTION_ENABLED="1"):
            self.assertFalse(_live_readonly_positions_monitoring_armed())          # execution flag ≠ read authority
        with override_settings(LIVE_READONLY_POSITIONS_MONITORING_ENABLED="1"):
            self.assertTrue(_live_readonly_positions_monitoring_armed())


class FirewallIdentityControls(SimpleTestCase):
    """req (5)(6)(7): the UNCHANGED backend read firewall binds the read to this tenant. Positive + negative
    controls here; DEMO/LIVE environment mismatch is covered by tests_live_readonly_firewall."""

    ACCT = SimpleNamespace(account_number="L1", broker_server=SimpleNamespace(server_name="SrvA"))

    def test_matching_identity_passes(self):  # positive control (RULE 11)
        self.assertTrue(ST.verify_snapshot_identity(self.ACCT, "L1", "SrvA", require_server=True).ok)

    def test_wrong_login_refused(self):  # req (5) + (7) cross-tenant
        r = ST.verify_snapshot_identity(self.ACCT, "L2", "SrvA", require_server=True)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, ST.ID_LOGIN_MISMATCH)

    def test_wrong_server_refused(self):  # req (6)
        r = ST.verify_snapshot_identity(self.ACCT, "L1", "SrvB", require_server=True)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, ST.ID_SERVER_MISMATCH)

    def test_missing_observation_refused(self):
        r = ST.verify_snapshot_identity(self.ACCT, "", "SrvA", require_server=True)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, ST.ID_OBSERVED_MISSING)
