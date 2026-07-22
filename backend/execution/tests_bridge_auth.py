"""Bridge HTTP authentication must FAIL CLOSED (standalone security remediation).

Regression cover for the leaked-token incident. The bridge previously ended ``_validate_token`` with
``return True  # No token configured, allow all`` — so a bridge started WITHOUT its env var accepted every
request unauthenticated, including the order-placing POST routes (/mt5/order, /mt5/close-position,
/mt5/modify-position). These tests pin the fail-closed contract:

  * no token configured            -> DENY (and startup refuses to run at all)
  * empty / whitespace-only token  -> DENY (treated as not configured)
  * request with no credential     -> DENY
  * request with a wrong credential-> DENY
  * request with the right one     -> ALLOW

Only synthetic tokens appear here; no real credential is ever present in this file or its output.
"""
import importlib.util
import os
from unittest import mock

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")

GOOD = "synthetic-agent-token-AAA"
OTHER = "synthetic-agent-token-BBB"


def _load_bridge(agent_token="", worker_token=""):
    """Import a FRESH bridge module under a controlled environment (it reads tokens at import time)."""
    env = {"GUVFX_AGENT_TOKEN": agent_token, "GUVFX_WORKER_TOKEN": worker_token}
    with mock.patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location("mt5_signal_bridge_under_test", _BRIDGE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class _Req:
    """Minimal stand-in for the HTTP handler: only ``headers`` is consulted by _validate_token."""

    def __init__(self, token=None):
        self.headers = {} if token is None else {"X-GuvFX-Agent-Token": token}


def _check(mod, token=None):
    return mod.OHLCRequestHandler._validate_token(_Req(token))


class BridgeAuthFailClosedTests(SimpleTestCase):
    def test_no_token_configured_denies_every_request(self):
        mod = _load_bridge(agent_token="", worker_token="")
        self.assertEqual(mod.HTTP_AUTH_TOKEN, "")
        self.assertFalse(_check(mod, None))          # no credential presented
        self.assertFalse(_check(mod, GOOD))          # ANY credential must still be denied
        self.assertFalse(_check(mod, ""))

    def test_whitespace_only_token_is_treated_as_unconfigured(self):
        mod = _load_bridge(agent_token="   ", worker_token="")
        self.assertEqual(mod.HTTP_AUTH_TOKEN, "")
        self.assertFalse(_check(mod, "   "))
        self.assertFalse(_check(mod, GOOD))

    def test_configured_token_allows_only_exact_match(self):
        mod = _load_bridge(agent_token=GOOD)
        self.assertTrue(_check(mod, GOOD))
        self.assertFalse(_check(mod, OTHER))         # wrong credential
        self.assertFalse(_check(mod, None))          # missing credential
        self.assertFalse(_check(mod, ""))            # empty credential
        self.assertFalse(_check(mod, GOOD + "x"))    # near-miss / prefix
        self.assertFalse(_check(mod, GOOD[:-1]))

    def test_worker_token_is_used_as_fallback_and_still_fails_closed(self):
        mod = _load_bridge(agent_token="", worker_token=GOOD)
        self.assertEqual(mod.HTTP_AUTH_TOKEN, GOOD)
        self.assertTrue(_check(mod, GOOD))
        self.assertFalse(_check(mod, None))
        self.assertFalse(_check(mod, OTHER))

    def test_agent_token_takes_precedence_over_worker_token(self):
        mod = _load_bridge(agent_token=GOOD, worker_token=OTHER)
        self.assertTrue(_check(mod, GOOD))
        self.assertFalse(_check(mod, OTHER))

    def test_startup_validation_refuses_to_run_without_auth(self):
        """There must be no production mode in which the bridge starts unauthenticated."""
        mod = _load_bridge(agent_token="", worker_token="")
        with mock.patch.object(mod, "API_URL", "https://api.example"), \
             mock.patch.object(mod, "ACCOUNT_ID", "1"), \
             mock.patch.object(mod, "WORKER_TOKEN", ""):
            self.assertFalse(mod.validate_config())

    def test_startup_validation_passes_when_auth_configured(self):
        mod = _load_bridge(agent_token=GOOD, worker_token=GOOD)
        with mock.patch.object(mod, "API_URL", "https://api.example"), \
             mock.patch.object(mod, "ACCOUNT_ID", "1"):
            self.assertTrue(mod.validate_config())

    def test_validator_has_no_unconditional_allow(self):
        """Guard the exact regression: no `return True` may exist in the validator body.

        The fail-closed implementation returns only False or hmac.compare_digest(...), so any literal
        `return True` reintroduces a permissive path.
        """
        import inspect
        mod = _load_bridge(agent_token=GOOD)
        src = inspect.getsource(mod.OHLCRequestHandler._validate_token)
        code = "\n".join(
            line for line in src.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn("return True", code)
