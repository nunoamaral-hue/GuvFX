"""WS1 + WS3: credential resolution must never substitute another service's secret, and must fail
cleanly at startup on missing / empty / placeholder / inconsistent configuration.

Regression cover for the 2026-07-22 rotation finding: mt5_validate_worker fell back from its own
MT5_WORKER_TOKEN to the BRIDGE's GUVFX_AGENT_TOKEN, which only worked while the two happened to be equal.
Only synthetic values appear here.
"""
import importlib.util
import os
from unittest import mock

from django.test import SimpleTestCase

from core.credentials import CredentialError, is_placeholder, resolve_secret

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GOOD = "synthetic-secret-value-AAAAAAAAAA"
OTHER = "synthetic-secret-value-BBBBBBBBBB"


class ResolveSecretTests(SimpleTestCase):
    def test_returns_value_from_primary(self):
        self.assertEqual(resolve_secret("A_TOKEN", env={"A_TOKEN": GOOD}), GOOD)

    def test_accepts_declared_alias_for_the_same_secret(self):
        self.assertEqual(
            resolve_secret("A_TOKEN", aliases=("A_TOKEN_LEGACY",), env={"A_TOKEN_LEGACY": GOOD}), GOOD)

    def test_missing_raises_with_actionable_message(self):
        with self.assertRaises(CredentialError) as ctx:
            resolve_secret("A_TOKEN", aliases=("A_LEGACY",), purpose="thing auth", env={})
        msg = str(ctx.exception)
        self.assertIn("A_TOKEN", msg)
        self.assertIn("A_LEGACY", msg)            # names every place it looked
        self.assertIn("another service's credential", msg)

    def test_empty_and_whitespace_are_treated_as_missing(self):
        for bad in ("", "   ", "\t\n"):
            with self.assertRaises(CredentialError):
                resolve_secret("A_TOKEN", env={"A_TOKEN": bad})

    def test_placeholder_text_is_rejected(self):
        for bad in ("changeme", "replace_with_real_token", "<your-token>", "${FROM_VAULT}",
                    "example-token", "<SCRUBBED-ROTATED-TOKEN>"):
            with self.assertRaises(CredentialError, msg=bad):
                resolve_secret("A_TOKEN", env={"A_TOKEN": bad})

    def test_disagreeing_aliases_fail_closed_rather_than_guessing(self):
        with self.assertRaises(CredentialError) as ctx:
            resolve_secret("A_TOKEN", aliases=("A_LEGACY",),
                           env={"A_TOKEN": GOOD, "A_LEGACY": OTHER})
        self.assertIn("disagree", str(ctx.exception))

    def test_agreeing_aliases_are_fine(self):
        self.assertEqual(
            resolve_secret("A_TOKEN", aliases=("A_LEGACY",), env={"A_TOKEN": GOOD, "A_LEGACY": GOOD}),
            GOOD)

    def test_short_value_rejected(self):
        with self.assertRaises(CredentialError):
            resolve_secret("A_TOKEN", min_length=32, env={"A_TOKEN": "short"})

    def test_error_never_contains_the_secret_value(self):
        for env in ({"A_TOKEN": "changeme-" + GOOD}, {"A_TOKEN": GOOD[:3]}):
            try:
                resolve_secret("A_TOKEN", min_length=64, env=env)
            except CredentialError as exc:
                self.assertNotIn(GOOD, str(exc))

    def test_no_api_exists_for_cross_credential_fallback(self):
        """resolve_secret has no parameter that would let one secret stand in for a different one."""
        import inspect
        params = set(inspect.signature(resolve_secret).parameters)
        self.assertEqual(params, {"primary", "aliases", "purpose", "min_length", "env"})

    def test_is_placeholder_helper(self):
        self.assertTrue(is_placeholder("CHANGEME"))
        self.assertFalse(is_placeholder(GOOD))


class ValidateWorkerCredentialTests(SimpleTestCase):
    """The exact regression: the validate worker must NOT accept the bridge's agent token."""

    @staticmethod
    def _load():
        path = os.path.join(_REPO, "mt5_worker", "mt5_validate_worker.py")
        spec = importlib.util.spec_from_file_location("mt5_validate_worker_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {}, clear=False):
            spec.loader.exec_module(mod)
        return mod

    def test_agent_token_is_no_longer_accepted_as_a_worker_credential(self):
        mod = self._load()
        with self.assertRaises(mod.WorkerCredentialError) as ctx:
            mod._resolve_worker_token({"GUVFX_AGENT_TOKEN": GOOD})
        self.assertIn("will NOT fall back", str(ctx.exception))

    def test_own_credential_is_accepted(self):
        mod = self._load()
        self.assertEqual(mod._resolve_worker_token({"MT5_WORKER_TOKEN": GOOD}), GOOD)
        self.assertEqual(mod._resolve_worker_token({"GUVFX_WORKER_TOKEN": GOOD}), GOOD)

    def test_disagreeing_worker_aliases_fail_closed(self):
        mod = self._load()
        with self.assertRaises(mod.WorkerCredentialError):
            mod._resolve_worker_token({"MT5_WORKER_TOKEN": GOOD, "GUVFX_WORKER_TOKEN": OTHER})

    def test_missing_credential_raises(self):
        mod = self._load()
        with self.assertRaises(mod.WorkerCredentialError):
            mod._resolve_worker_token({})

    def test_startup_validation_exits_nonzero_without_credential(self):
        mod = self._load()
        with mock.patch.dict(os.environ, {"MT5_WORKER_TOKEN": "", "GUVFX_WORKER_TOKEN": ""}, clear=False):
            with self.assertRaises(SystemExit) as ctx:
                mod.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_heartbeat_stays_best_effort_without_credential(self):
        """A missing credential must not kill the validation loop at runtime."""
        mod = self._load()
        with mock.patch.dict(os.environ, {"MT5_WORKER_TOKEN": "", "GUVFX_WORKER_TOKEN": ""}, clear=False):
            mod.emit_heartbeat()   # must not raise


class BridgeNoCrossCredentialFallbackTests(SimpleTestCase):
    """The bridge's inbound auth must use GUVFX_AGENT_TOKEN only."""

    @staticmethod
    def _load(agent="", worker=""):
        path = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")
        import tempfile
        prev = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with mock.patch.dict(os.environ,
                                     {"GUVFX_AGENT_TOKEN": agent, "GUVFX_WORKER_TOKEN": worker},
                                     clear=False):
                    spec = importlib.util.spec_from_file_location("bridge_ws1_under_test", path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
            finally:
                os.chdir(prev)
        return mod

    def test_worker_token_no_longer_authenticates_inbound_requests(self):
        mod = self._load(agent="", worker=GOOD)
        self.assertEqual(mod.HTTP_AUTH_TOKEN, "")          # no fallback to the worker credential
        import email.message

        class _Req:
            def __init__(self, tok):
                self.headers = email.message.Message()
                self.headers["X-GuvFX-Agent-Token"] = tok
        self.assertFalse(mod.OHLCRequestHandler._validate_token(_Req(GOOD)))

    def test_agent_token_authenticates(self):
        mod = self._load(agent=GOOD, worker=OTHER)
        self.assertEqual(mod.HTTP_AUTH_TOKEN, GOOD)

    def test_startup_refuses_without_agent_token(self):
        mod = self._load(agent="", worker=GOOD)
        with mock.patch.object(mod, "API_URL", "https://api.example"), \
             mock.patch.object(mod, "ACCOUNT_ID", "1"):
            self.assertFalse(mod.validate_config())

    def test_startup_rejects_placeholder_secret(self):
        mod = self._load(agent="changeme", worker=GOOD)
        with mock.patch.object(mod, "API_URL", "https://api.example"), \
             mock.patch.object(mod, "ACCOUNT_ID", "1"):
            self.assertFalse(mod.validate_config())
