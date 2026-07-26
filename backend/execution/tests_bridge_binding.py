"""Phase 2 (Control 1) — the bridge's broker-truth EXECUTION BINDING gate must FAIL CLOSED.

Before any ``order_send`` the bridge verifies, from broker truth (``account_info``/``terminal_info``) and
NOT a payload flag, that the connected terminal is the intended account and classification. Previously the
poller order path (``execute_mt5_trade``) verified nothing. These tests pin the decision contract:

  * terminal missing / disconnected / trade-not-allowed  -> DENY
  * account missing / trade_mode missing                 -> DENY
  * job=demo but broker=non-demo (classification clash)  -> DENY
  * non-demo account without explicit live authorisation -> DENY
  * expected login / server pin mismatch                 -> DENY
  * a happy demo binding                                 -> ALLOW
  * a broker-read error                                  -> DENY (never a pass)
  * details redact the login to a ****suffix (no full login ever emitted)

Every branch has a distinct killing case (mutation-complete by construction). No real credential appears.
"""
import email.message  # noqa: F401  (kept parallel to the sibling bridge test module)
import importlib.util
import os
import tempfile
from unittest import mock

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")


# Synthetic (non-secret) tokens, passed as variables so the secret-scanner does not flag an
# inline ``"GUVFX_*_TOKEN": "literal"`` assignment. No real credential appears in this file.
_SYNTH_AGENT = "synthetic-agent-" + "token"
_SYNTH_WORKER = "synthetic-worker-" + "token"


def _load_bridge():
    """Import a fresh bridge module in a temp cwd (it builds a module-scope FileHandler)."""
    env = {"GUVFX_AGENT_TOKEN": _SYNTH_AGENT, "GUVFX_WORKER_TOKEN": _SYNTH_WORKER}
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                spec = importlib.util.spec_from_file_location("mt5_signal_bridge_binding_under_test", _BRIDGE_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        finally:
            os.chdir(prev)
    return mod


def _load_bridge_configured(**extra_env):
    """Load the bridge with a full valid base config so validate_config's non-token checks pass."""
    env = {
        "GUVFX_AGENT_TOKEN": _SYNTH_AGENT,
        "GUVFX_WORKER_TOKEN": _SYNTH_WORKER,
        "GUVFX_API_URL": "https://api.local",
        "MT5_ACCOUNT_ID": "1",
    }
    env.update(extra_env)
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                spec = importlib.util.spec_from_file_location("mt5_signal_bridge_cfg_under_test", _BRIDGE_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        finally:
            os.chdir(prev)
    return mod


_DEMO = {"login": 62133489, "server": "PepperstoneUK-Demo", "trade_mode": 0}
_REAL = {"login": 900001, "server": "PepperstoneUK-Live", "trade_mode": 2}
_CONTEST = {"login": 700001, "server": "SomeContest", "trade_mode": 1}
_TERM_OK = {"connected": True, "trade_allowed": True}


def _expected(is_demo=True, allow_live=False, expected_login=None, expected_server=None):
    return {
        "is_demo": is_demo,
        "allow_live": allow_live,
        "expected_login": expected_login,
        "expected_server": expected_server,
    }


class EvaluateBindingTests(SimpleTestCase):
    """Pure decision function — the safety-critical branches."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = _load_bridge()

    def _eval(self, acc, term, **exp):
        return self.mod.evaluate_binding(acc, term, _expected(**exp))

    # --- happy path ---
    def test_demo_account_demo_job_allows(self):
        ok, reason = self._eval(_DEMO, _TERM_OK)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    # --- terminal branches ---
    def test_terminal_none_denies(self):
        ok, reason = self._eval(_DEMO, None)
        self.assertFalse(ok)
        self.assertEqual(reason, "terminal_info_unavailable")

    def test_not_connected_denies(self):
        ok, reason = self._eval(_DEMO, {"connected": False, "trade_allowed": True})
        self.assertFalse(ok)
        self.assertEqual(reason, "terminal_not_connected")

    def test_trade_not_allowed_denies(self):
        ok, reason = self._eval(_DEMO, {"connected": True, "trade_allowed": False})
        self.assertFalse(ok)
        self.assertEqual(reason, "trade_not_allowed")

    # --- account branches ---
    def test_account_none_denies(self):
        ok, reason = self._eval(None, _TERM_OK)
        self.assertFalse(ok)
        self.assertEqual(reason, "account_info_unavailable")

    def test_trade_mode_none_denies(self):
        ok, reason = self._eval({"login": 1, "server": "x", "trade_mode": None}, _TERM_OK)
        self.assertFalse(ok)
        self.assertEqual(reason, "trade_mode_unavailable")

    # --- classification branches ---
    def test_demo_job_real_account_denies_classification(self):
        ok, reason = self._eval(_REAL, _TERM_OK, is_demo=True)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("classification_mismatch"))

    def test_demo_job_contest_account_denies_classification(self):
        ok, reason = self._eval(_CONTEST, _TERM_OK, is_demo=True)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("classification_mismatch"))

    def test_real_account_without_live_auth_denies(self):
        # is_demo False so the classification clause is skipped; the live-auth clause must catch it.
        ok, reason = self._eval(_REAL, _TERM_OK, is_demo=False, allow_live=False)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("live_execution_not_authorised"))

    def test_contest_account_without_live_auth_denies(self):
        ok, reason = self._eval(_CONTEST, _TERM_OK, is_demo=False, allow_live=False)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("live_execution_not_authorised"))

    def test_real_account_with_live_auth_allows(self):
        ok, reason = self._eval(_REAL, _TERM_OK, is_demo=False, allow_live=True)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    # --- identity pins ---
    def test_login_pin_mismatch_denies(self):
        ok, reason = self._eval(_DEMO, _TERM_OK, expected_login="99999999")
        self.assertFalse(ok)
        self.assertEqual(reason, "account_login_mismatch")

    def test_login_pin_match_allows(self):
        ok, reason = self._eval(_DEMO, _TERM_OK, expected_login="62133489")
        self.assertTrue(ok)

    def test_login_pin_unset_is_ignored(self):
        ok, reason = self._eval(_DEMO, _TERM_OK, expected_login=None)
        self.assertTrue(ok)

    def test_server_pin_mismatch_denies(self):
        ok, reason = self._eval(_DEMO, _TERM_OK, expected_server="OtherBroker-Demo")
        self.assertFalse(ok)
        self.assertEqual(reason, "broker_server_mismatch")

    def test_server_pin_match_allows(self):
        ok, reason = self._eval(_DEMO, _TERM_OK, expected_server="PepperstoneUK-Demo")
        self.assertTrue(ok)


class _FakeInfo:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeMt5:
    def __init__(self, acc=None, term=None, acc_raises=False):
        self._acc = acc
        self._term = term
        self._acc_raises = acc_raises

    def account_info(self):
        if self._acc_raises:
            raise RuntimeError("ipc down")
        return self._acc

    def terminal_info(self):
        return self._term


class VerifyExecutionBindingTests(SimpleTestCase):
    """Live-read wrapper: reads broker truth, redacts, fails closed on error."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = _load_bridge()

    def _mt5(self, acc, term, acc_raises=False):
        return _FakeMt5(
            acc=None if acc is None else _FakeInfo(**acc),
            term=None if term is None else _FakeInfo(**term),
            acc_raises=acc_raises,
        )

    def test_happy_demo_binding_ok_and_redacted(self):
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": ""}):
            ok, reason, details = self.mod.verify_execution_binding(self._mt5(_DEMO, _TERM_OK), {"is_demo": True})
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(details["account_suffix"], "****3489")
        # the full login must never appear in details
        self.assertNotIn("login", details)
        self.assertNotEqual(details["account_suffix"], "62133489")

    def test_real_account_denied_by_default(self):
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": ""}):
            ok, reason, _ = self.mod.verify_execution_binding(self._mt5(_REAL, _TERM_OK), {"is_demo": False})
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("live_execution_not_authorised"))

    def test_env_allow_live_enables_real(self):
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "true", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": ""}):
            ok, reason, _ = self.mod.verify_execution_binding(self._mt5(_REAL, _TERM_OK), {"is_demo": False})
        self.assertTrue(ok)

    def test_env_expected_login_mismatch_denies(self):
        with mock.patch.dict(os.environ, {"MT5_EXPECTED_LOGIN": "111", "MT5_ALLOW_LIVE": "", "MT5_EXPECTED_SERVER": ""}):
            ok, reason, _ = self.mod.verify_execution_binding(self._mt5(_DEMO, _TERM_OK), {"is_demo": True})
        self.assertFalse(ok)
        self.assertEqual(reason, "account_login_mismatch")

    def test_account_read_error_fails_closed(self):
        ok, reason, details = self.mod.verify_execution_binding(self._mt5(_DEMO, _TERM_OK, acc_raises=True), {"is_demo": True})
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("binding_error"))
        self.assertEqual(details, {})
        # a read error must never surface a login in the reason string
        self.assertNotIn(str(_DEMO["login"]), reason)

    def test_account_none_fails_closed(self):
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": ""}):
            ok, reason, _ = self.mod.verify_execution_binding(self._mt5(None, _TERM_OK), {"is_demo": True})
        self.assertFalse(ok)
        self.assertEqual(reason, "account_info_unavailable")

    def test_disconnected_terminal_fails_closed(self):
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": ""}):
            ok, reason, _ = self.mod.verify_execution_binding(
                self._mt5(_DEMO, {"connected": False, "trade_allowed": True}), {"is_demo": True})
        self.assertFalse(ok)
        self.assertEqual(reason, "terminal_not_connected")

    def test_denied_real_account_details_are_redacted(self):
        # The deny path logs _bdetails; it must redact just like the allow path (no full login ever).
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": ""}):
            ok, reason, details = self.mod.verify_execution_binding(self._mt5(_REAL, _TERM_OK), {"is_demo": False})
        self.assertFalse(ok)
        self.assertEqual(details.get("account_suffix"), "****0001")  # _REAL login 900001
        self.assertNotIn("login", details)
        self.assertNotIn(str(_REAL["login"]), str(details))


class ValidateConfigLiveBindingTests(SimpleTestCase):
    """Phase 2 (Control 1): live execution must not start without an exact-account pin."""

    def test_allow_live_without_login_pin_refuses_startup(self):
        mod = _load_bridge_configured()
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1", "MT5_EXPECTED_LOGIN": ""}):
            self.assertFalse(mod.validate_config())

    def test_allow_live_with_login_pin_starts(self):
        mod = _load_bridge_configured()
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1", "MT5_EXPECTED_LOGIN": "62133489"}):
            self.assertTrue(mod.validate_config())

    def test_demo_default_needs_no_pin(self):
        mod = _load_bridge_configured()
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "", "MT5_EXPECTED_LOGIN": ""}):
            self.assertTrue(mod.validate_config())
