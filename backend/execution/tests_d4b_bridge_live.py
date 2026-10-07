"""Stream D4b — SIMULATED-BRIDGE tests for the per-runtime live-authorization rails (no real order/terminal).

The standalone bridge (scripts/mt5_signal_bridge.py) is loaded in a temp cwd. These pin the two-layer
fail-closed money-path contract:
  * validate_job_safety: a non-demo job is refused UNLESS the runtime is explicitly authorized-live
    (MT5_ALLOW_LIVE); demo jobs + unauthorized runtimes are byte-identical (refused).
  * evaluate_hosted_startup_config: a hosted bridge may carry MT5_ALLOW_LIVE (D4b), but ONLY with the
    mandatory identity pin + guarded attach; a demo hosted bridge is unchanged.
  * evaluate_binding: a live account trades ONLY with allow_live AND an identity-pin match.
No Django, no DB, no MT5 — pure/simulated seam.
"""
import importlib.util
import os
import tempfile
from unittest import mock

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")
_SYNTH_AGENT = "synthetic-agent-" + "token"
_SYNTH_WORKER = "synthetic-worker-" + "token"


def _load_bridge():
    env = {"GUVFX_AGENT_TOKEN": _SYNTH_AGENT, "GUVFX_WORKER_TOKEN": _SYNTH_WORKER}
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                spec = importlib.util.spec_from_file_location("mt5_bridge_d4b_under_test", _BRIDGE_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        finally:
            os.chdir(prev)
    return mod


class D4bBridgeRailsTests(SimpleTestCase):
    def setUp(self):
        self.b = _load_bridge()

    def _job(self, is_demo):
        return {"job_type": "PLACE_ORDER",
                "payload": {"is_demo": is_demo, "symbol": "EURUSD", "lots": 0.01, "side": "BUY",
                            "sl_price": 1.0, "tp_price": 2.0}}

    # ---- validate_job_safety ----
    def test_demo_job_safe_unchanged(self):
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": ""}, clear=False):
            ok, msg = self.b.validate_job_safety(self._job(True))
        self.assertTrue(ok, msg)

    def test_live_job_refused_without_allow_live(self):
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": ""}, clear=False):
            ok, msg = self.b.validate_job_safety(self._job(False))
        self.assertFalse(ok)
        self.assertIn("Refusing", msg)

    def test_live_job_passes_demo_rail_with_allow_live(self):
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1"}, clear=False):
            ok, msg = self.b.validate_job_safety(self._job(False))
        self.assertTrue(ok, msg)   # passes the demo rail (symbol/lot valid) — runtime is authorized-live

    # ---- evaluate_hosted_startup_config ----
    def _hosted_env(self, **over):
        env = {"MT5_HOSTED_EXECUTION": "1", "MT5_GUARDED_ATTACH": "1", "MT5_REQUIRE_IDENTITY_PIN": "1",
               "GUVFX_WORKER_ID": "w", "GUVFX_WORKER_SECRET": "s"}
        env.update(over)
        return env

    def test_hosted_demo_startup_unchanged(self):
        self.assertEqual(self.b.evaluate_hosted_startup_config(self._hosted_env()), [])   # no MT5_ALLOW_LIVE

    def test_hosted_live_permitted_with_pin_and_guard(self):
        self.assertEqual(self.b.evaluate_hosted_startup_config(self._hosted_env(MT5_ALLOW_LIVE="1")), [])

    def test_hosted_live_requires_pin(self):
        errs = self.b.evaluate_hosted_startup_config(
            self._hosted_env(MT5_ALLOW_LIVE="1", MT5_REQUIRE_IDENTITY_PIN=""))
        self.assertTrue(errs)   # fail-closed: live hosted without the pin is rejected

    def test_hosted_live_requires_guarded_attach(self):
        errs = self.b.evaluate_hosted_startup_config(
            self._hosted_env(MT5_ALLOW_LIVE="1", MT5_GUARDED_ATTACH=""))
        self.assertTrue(errs)

    # ---- evaluate_binding (order-time) ----
    def test_binding_live_allowed_with_allow_live_and_pin_match(self):
        acc = {"login": 900001, "server": "FX-Live", "trade_mode": 2}
        term = {"connected": True, "trade_allowed": True}
        exp = {"is_demo": False, "allow_live": True, "expected_login": "900001", "expected_server": "FX-Live"}
        ok, reason = self.b.evaluate_binding(acc, term, exp)
        self.assertTrue(ok, reason)

    def test_binding_live_refused_without_allow_live(self):
        acc = {"login": 900001, "server": "FX-Live", "trade_mode": 2}
        term = {"connected": True, "trade_allowed": True}
        exp = {"is_demo": False, "allow_live": False, "expected_login": "900001", "expected_server": "FX-Live"}
        ok, reason = self.b.evaluate_binding(acc, term, exp)
        self.assertFalse(ok)
        self.assertIn("live_execution_not_authorised", reason)

    def test_binding_live_refused_on_identity_mismatch_even_with_allow_live(self):
        acc = {"login": 900001, "server": "FX-Live", "trade_mode": 2}
        term = {"connected": True, "trade_allowed": True}
        exp = {"is_demo": False, "allow_live": True, "expected_login": "999999", "expected_server": "FX-Live"}
        ok, reason = self.b.evaluate_binding(acc, term, exp)
        self.assertFalse(ok)
        self.assertIn("login_mismatch", reason)
