"""ADR-0049.1 — HOST-SIDE TIME-BOUND for a LIVE bridge runtime (MT5_LIVE_EXPIRY).

Two layers pinned here:
  1. ``bridge_config.render_bridge_env`` — a LIVE render ALWAYS carries ``MT5_LIVE_EXPIRY`` alongside
     ``MT5_ALLOW_LIVE=1`` and REFUSES ``allow_live`` without a positive-int expiry; the OFF render omits both.
  2. ``scripts/mt5_signal_bridge.py`` — the runtime admits an OPENING LIVE order ONLY while both the live
     authorization (MT5_ALLOW_LIVE=1) AND a valid, non-expired, non-absurd host time-bound hold, read FRESH per
     order (so a restart cannot reset/extend it and a lapsed window denies the next order). CLOSE/MODIFY/
     PARTIAL-CLOSE stay demo-only and never consult the live admission.
"""
import importlib.util
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from execution.bridge_config import render_bridge_env

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")
_SYNTH_AGENT = "synthetic-agent-" + "token"
_SYNTH_WORKER = "synthetic-worker-" + "token"


def _load_bridge(**extra_env):
    env = {"GUVFX_AGENT_TOKEN": _SYNTH_AGENT, "GUVFX_WORKER_TOKEN": _SYNTH_WORKER}
    env.update(extra_env)
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                spec = importlib.util.spec_from_file_location("mt5_bridge_live_expiry_under_test", _BRIDGE_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        finally:
            os.chdir(prev)
    return mod


def _ep():
    return SimpleNamespace(
        trading_account_id=48, runtime_path=r"C:\GuvFX\accounts\48\terminal\terminal64.exe", port=8808,
        expected_login="38313831", expected_server="IS6Technologies-Live", windows_username="guvfx_u_48",
        workspace_uuid="w-uuid", is_demo=False)


class RenderBridgeEnvExpiryTests(SimpleTestCase):
    def test_allow_live_without_expiry_refused(self):
        with self.assertRaises(ValueError):
            render_bridge_env(_ep(), allow_live=True)                      # a LIVE boundary must be time-bounded

    def test_allow_live_emits_both_keys(self):
        t = render_bridge_env(_ep(), allow_live=True, live_expiry_epoch=1_893_456_000)
        self.assertIn("set MT5_ALLOW_LIVE=1", t)
        self.assertIn("set MT5_LIVE_EXPIRY=1893456000", t)

    def test_off_render_omits_both_keys(self):
        t = render_bridge_env(_ep(), allow_live=False)
        self.assertNotIn("MT5_ALLOW_LIVE", t)
        self.assertNotIn("MT5_LIVE_EXPIRY", t)
        # the expiry arg is ignored entirely when not live
        self.assertNotIn("MT5_LIVE_EXPIRY", render_bridge_env(_ep(), allow_live=False, live_expiry_epoch=1_893_456_000))

    def test_expiry_must_be_positive_int(self):
        for bad in (0, -5, "1893456000", 1.5, True, None):
            with self.assertRaises(ValueError):
                render_bridge_env(_ep(), allow_live=True, live_expiry_epoch=bad)


class BridgeLiveExpiryHelperTests(SimpleTestCase):
    def test_expiry_ok_window(self):
        mod = _load_bridge()
        now = 1_000_000
        cases = {
            str(now + 100): True,      # in the future, within ceiling
            str(now + 7200): True,     # exactly at the ceiling (<=)
            str(now + 7201): False,    # beyond the ceiling -> absurd/tamper -> deny
            str(now): False,           # exactly now -> strict > -> deny
            str(now - 1): False,       # past -> deny
            "": False, "notanumber": False, "12x3": False,   # absent/malformed -> deny
        }
        for raw, expected in cases.items():
            with mock.patch.dict(os.environ, {"MT5_LIVE_EXPIRY": raw}, clear=False):
                self.assertEqual(mod._bridge_live_expiry_ok(now), expected, raw)
        # absent variable entirely -> deny
        env = dict(os.environ); env.pop("MT5_LIVE_EXPIRY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(mod._bridge_live_expiry_ok(now))

    def test_admitted_requires_both_allow_live_and_valid_expiry(self):
        mod = _load_bridge()
        now = 1_000_000
        future, past = str(now + 100), str(now - 100)
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1", "MT5_LIVE_EXPIRY": future}, clear=False):
            self.assertTrue(mod._bridge_live_admitted(now))
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1", "MT5_LIVE_EXPIRY": past}, clear=False):
            self.assertFalse(mod._bridge_live_admitted(now))               # expired ⇒ not admitted
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "0", "MT5_LIVE_EXPIRY": future}, clear=False):
            self.assertFalse(mod._bridge_live_admitted(now))               # no live authorization ⇒ not admitted

    def test_read_fresh_each_call_not_cached(self):
        # The expiry is read from the env on EVERY call (per-order enforcement + restart cannot extend it): the
        # SAME loaded module flips its verdict when the env value changes between calls.
        mod = _load_bridge()
        now = 1_000_000
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1", "MT5_LIVE_EXPIRY": str(now + 100)}, clear=False):
            self.assertTrue(mod._bridge_live_admitted(now))
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1", "MT5_LIVE_EXPIRY": str(now - 100)}, clear=False):
            self.assertFalse(mod._bridge_live_admitted(now))


class BridgeBindingLiveExpiryIntegrationTests(SimpleTestCase):
    """The expiry feeds the OPENING-order admission through ``verify_execution_binding`` → ``evaluate_binding``:
    a REAL (non-demo) account is refused unless MT5_ALLOW_LIVE AND a non-expired MT5_LIVE_EXPIRY both hold."""

    def _mt5(self):
        acc = SimpleNamespace(login=900001, server="PepperstoneUK-Live", trade_mode=2)   # 2 == REAL
        term = SimpleNamespace(connected=True, trade_allowed=True)
        return SimpleNamespace(account_info=lambda: acc, terminal_info=lambda: term)

    def _payload(self):
        return {"is_demo": False, "require_identity_pin": True,
                "expected_login": "900001", "expected_server": "PepperstoneUK-Live"}

    def test_real_account_refused_when_expiry_past_even_with_allow_live(self):
        mod = _load_bridge()
        past = str(int(time.time()) - 100)
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1", "MT5_LIVE_EXPIRY": past}, clear=False):
            ok, reason, _d = mod.verify_execution_binding(self._mt5(), self._payload())
        self.assertFalse(ok)
        self.assertIn("live_execution_not_authorised", reason)

    def test_real_account_refused_when_expiry_missing_even_with_allow_live(self):
        mod = _load_bridge()
        env = dict(os.environ); env.pop("MT5_LIVE_EXPIRY", None); env["MT5_ALLOW_LIVE"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            ok, reason, _d = mod.verify_execution_binding(self._mt5(), self._payload())
        self.assertFalse(ok)
        self.assertIn("live_execution_not_authorised", reason)

    def test_real_account_admitted_when_allow_live_and_valid_expiry(self):
        mod = _load_bridge()
        future = str(int(time.time()) + 1800)      # 30 min ahead, inside the 2h ceiling
        with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "1", "MT5_LIVE_EXPIRY": future}, clear=False):
            ok, reason, _d = mod.verify_execution_binding(self._mt5(), self._payload())
        self.assertTrue(ok, reason)

    def test_startup_config_requires_expiry_when_live(self):
        mod = _load_bridge()
        base = {"MT5_HOSTED_EXECUTION": "1", "MT5_GUARDED_ATTACH": "1", "MT5_REQUIRE_IDENTITY_PIN": "1",
                "MT5_ALLOW_LIVE": "1", "GUVFX_WORKER_ID": "w", "GUVFX_WORKER_SECRET": "s"}
        errs = mod.evaluate_hosted_startup_config({**base, "MT5_LIVE_EXPIRY": ""})     # live but no time-bound
        self.assertTrue(any("MT5_LIVE_EXPIRY" in e for e in errs))
        errs_ok = mod.evaluate_hosted_startup_config({**base, "MT5_LIVE_EXPIRY": "9999999999"})
        self.assertFalse(any("MT5_LIVE_EXPIRY" in e for e in errs_ok))
