"""ADR-0034 Execution Engine (G6) — Hosted Workspace bridge startup safety assertions.

A HOSTED WORKSPACE execution bridge (``MT5_HOSTED_EXECUTION`` set) must REFUSE to start unless its complete
safety configuration is present, and must never silently downgrade to legacy / shared / credential-login /
unguarded / un-pinned execution. ``evaluate_hosted_startup_config`` is pure (env-in, error-list-out), so it
is proven without MT5 or process I/O. Legacy bridges (flag unset) are entirely unaffected.
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
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ, {"GUVFX_AGENT_TOKEN": _SYNTH_AGENT,
                                              "GUVFX_WORKER_TOKEN": _SYNTH_WORKER}, clear=False):
                spec = importlib.util.spec_from_file_location("mt5_bridge_hosted_startup_under_test",
                                                              _BRIDGE_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        finally:
            os.chdir(prev)
    return mod


_BR = _load_bridge()

_VALID_HOSTED = {
    "MT5_HOSTED_EXECUTION": "1",
    "MT5_GUARDED_ATTACH": "1",
    "MT5_REQUIRE_IDENTITY_PIN": "1",
    # MT5_ALLOW_LIVE unset; no MT5_LOGIN/PASSWORD/SERVER
}


class HostedStartupConfigTests(SimpleTestCase):
    def test_legacy_bridge_unaffected(self):
        # Flag unset ⇒ no hosted assertions at all (legacy/production behaviour byte-for-byte unchanged).
        self.assertEqual(_BR.evaluate_hosted_startup_config({}), [])
        self.assertEqual(_BR.evaluate_hosted_startup_config({"MT5_ALLOW_LIVE": "1", "MT5_LOGIN": "x"}), [])

    def test_valid_hosted_config_passes(self):
        self.assertEqual(_BR.evaluate_hosted_startup_config(dict(_VALID_HOSTED)), [])

    def test_missing_guarded_attach_rejected(self):
        env = dict(_VALID_HOSTED); env.pop("MT5_GUARDED_ATTACH")
        errs = _BR.evaluate_hosted_startup_config(env)
        self.assertTrue(any("MT5_GUARDED_ATTACH" in e for e in errs))

    def test_missing_identity_pin_rejected(self):
        env = dict(_VALID_HOSTED); env.pop("MT5_REQUIRE_IDENTITY_PIN")
        errs = _BR.evaluate_hosted_startup_config(env)
        self.assertTrue(any("MT5_REQUIRE_IDENTITY_PIN" in e for e in errs))

    def test_live_mode_rejected_demo_only(self):
        env = dict(_VALID_HOSTED); env["MT5_ALLOW_LIVE"] = "1"
        errs = _BR.evaluate_hosted_startup_config(env)
        self.assertTrue(any("DEMO-ONLY" in e or "MT5_ALLOW_LIVE" in e for e in errs))

    def test_credential_login_path_rejected(self):
        for cred in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
            env = dict(_VALID_HOSTED); env[cred] = "something"
            errs = _BR.evaluate_hosted_startup_config(env)
            self.assertTrue(any(cred in e for e in errs), cred)

    def test_completely_unsafe_hosted_config_aggregates_errors(self):
        env = {"MT5_HOSTED_EXECUTION": "1", "MT5_ALLOW_LIVE": "1", "MT5_LOGIN": "x", "MT5_PASSWORD": "y",
               "MT5_SERVER": "z"}  # hosted on, but guarded off + pin off + live + full creds
        errs = _BR.evaluate_hosted_startup_config(env)
        self.assertGreaterEqual(len(errs), 5)  # guarded + pin + live + 3 creds

    def test_validate_config_wires_the_hosted_assertion(self):
        # Structural: validate_config must call the hosted assertion so a hosted bridge cannot start unsafe
        # (no hidden downgrade branch that skips it).
        import inspect
        src = inspect.getsource(_BR.validate_config)
        self.assertIn("evaluate_hosted_startup_config", src)

    def test_no_secret_in_error_text(self):
        env = dict(_VALID_HOSTED); env["MT5_PASSWORD"] = "SUPERSECRETPW"
        errs = _BR.evaluate_hosted_startup_config(env)
        self.assertTrue(errs)
        self.assertNotIn("SUPERSECRETPW", " ".join(errs))  # reports the KEY, never the value
