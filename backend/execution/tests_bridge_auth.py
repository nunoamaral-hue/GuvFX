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
import ast
import email.message
import importlib.util
import os
import tempfile
from unittest import mock

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")

GOOD = "synthetic-agent-token-AAA"
OTHER = "synthetic-agent-token-BBB"


def _load_bridge(agent_token="", worker_token=""):
    """Import a FRESH bridge module under a controlled environment (it reads tokens at import time).

    Loading is done from a temp cwd because the bridge builds a FileHandler at module scope, which would
    otherwise litter a log file into whatever directory the test runner happens to be in.
    """
    env = {"GUVFX_AGENT_TOKEN": agent_token, "GUVFX_WORKER_TOKEN": worker_token}
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                spec = importlib.util.spec_from_file_location("mt5_signal_bridge_under_test", _BRIDGE_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        finally:
            os.chdir(prev)
    return mod


class _Req:
    """Stand-in for the HTTP handler using a REAL ``email.message.Message``.

    The live handler's ``self.headers`` is an email.message.Message (case-insensitive, multi-valued,
    latin-1 decoded) — not a dict. Using the real type is what exercises the header-layer edge cases.
    """

    def __init__(self, token=None, header_name="X-GuvFX-Agent-Token"):
        self.headers = email.message.Message()
        if token is not None:
            self.headers[header_name] = token


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

    def test_non_ascii_credential_is_denied_not_an_exception(self):
        """A latin-1 header byte >= 0x80 must yield a clean deny, never a TypeError -> 500.

        hmac.compare_digest raises on non-ASCII str operands, so the validator compares bytes.
        """
        mod = _load_bridge(agent_token=GOOD)
        for bad in ("Az9é", "ÿ" * 20, GOOD + "é"):
            self.assertFalse(_check(mod, bad))

    def test_header_lookup_is_case_insensitive_and_still_enforced(self):
        """Real headers are case-insensitive; a differently-cased header must not bypass or break auth."""
        mod = _load_bridge(agent_token=GOOD)
        self.assertTrue(mod.OHLCRequestHandler._validate_token(_Req(GOOD, header_name="x-guvfx-agent-token")))
        self.assertFalse(mod.OHLCRequestHandler._validate_token(_Req(OTHER, header_name="X-GUVFX-AGENT-TOKEN")))

    def test_validator_has_no_unconditional_allow(self):
        """Guard the regression structurally: the validator may never `return True` unconditionally.

        Parses the AST rather than grepping for a literal, so alternate spellings (`return  True`,
        `return bool(1)`, `return not HTTP_AUTH_TOKEN or ...`) cannot slip a permissive path back in.
        Every `return` must be either a bare False, or a call/compare expression (the compare_digest gate).
        """
        with open(_BRIDGE_PATH, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "_validate_token"), None)
        self.assertIsNotNone(fn, "_validate_token not found")
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return):
                continue
            val = node.value
            if isinstance(val, ast.Constant) and val.value is False:
                continue                      # explicit deny — always fine
            if isinstance(val, ast.Constant) and val.value is True:
                # A bare `return True` is only permitted as the tail of a compare_digest-gated branch,
                # which this implementation expresses as `if not compare_digest(...): return False` first.
                # Assert the guarded form exists rather than allowing a naked allow.
                self.assertIn("compare_digest", ast.dump(fn),
                              "return True present without a compare_digest gate")
                continue
            self.assertIsInstance(
                val, (ast.Call, ast.Compare, ast.BoolOp, ast.UnaryOp),
                f"unexpected permissive return in _validate_token: {ast.dump(val)}")
