"""STREAM 9E - tests for the observer's self-contained guarded-attach (observer_attach).

Two jobs:
  1. **Invariant lock** - the never-launch / never-login / fail-closed contract of the read-only attach.
  2. **Parity lock** - ``observer_attach`` is a faithful extraction of the certified helpers in
     ``scripts.mt5_signal_bridge``; this asserts the two behave IDENTICALLY across a matrix so the decoupled copy
     can never silently diverge (the review-fakes guard). If the legacy source changes, this test fails until the
     extraction is reconciled.
"""
import os
from unittest import mock

from django.test import SimpleTestCase

from terminal_provisioning.windows import observer_attach as OA


class _Term:
    def __init__(self, connected):
        self.connected = connected


class _Acc:
    def __init__(self, login=1302575, server="IS6Technologies-Demo", trade_mode=0):
        self.login = login
        self.server = server
        self.trade_mode = trade_mode


class _Mt5:
    """A fake MetaTrader5 module. Records whether initialize/login were called and with what."""
    def __init__(self, *, init_ok=True, term=None, acc=None, raise_on=None):
        self.init_ok = init_ok
        self._term = term
        self._acc = acc
        self._raise_on = raise_on or set()
        self.initialize_kwargs = None
        self.login_called = False
        self.shutdown_called = False

    def initialize(self, **kwargs):
        self.initialize_kwargs = kwargs
        if "initialize" in self._raise_on:
            raise RuntimeError("boom")
        return self.init_ok

    def terminal_info(self):
        if "terminal_info" in self._raise_on:
            raise RuntimeError("boom")
        return self._term

    def account_info(self):
        if "account_info" in self._raise_on:
            raise RuntimeError("boom")
        return self._acc

    def login(self, *a, **k):
        self.login_called = True
        raise AssertionError("guarded attach must never call mt5.login()")

    def shutdown(self):
        self.shutdown_called = True


_PATH = r"C:\GuvFX\accounts\18\terminal\terminal64.exe"


class InvariantTests(SimpleTestCase):
    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_guarded_never_launches_a_down_terminal(self):
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: False)  # not running
        self.assertFalse(ok)
        self.assertIsNone(mt5.initialize_kwargs)   # initialize() never called when the terminal is down

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_guarded_rejects_credential_keys(self):
        mt5 = _Mt5()
        for bad in ({"path": _PATH, "login": 1}, {"path": _PATH, "password": "x"}, {"path": _PATH, "server": "s"}):
            self.assertFalse(OA.guarded_initialize(mt5, bad, probe=lambda p: True))
        self.assertFalse(mt5.login_called)

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_guarded_ok_only_when_connected_with_account(self):
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        self.assertTrue(OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True))
        self.assertFalse(mt5.login_called)

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_guarded_releases_attach_when_not_connected(self):
        mt5 = _Mt5(init_ok=True, term=_Term(False), acc=None)
        self.assertFalse(OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True))
        self.assertTrue(mt5.shutdown_called)   # a partial attach is released, never left dangling

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_guarded_fail_closed_on_raise(self):
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc(), raise_on={"terminal_info"})
        self.assertFalse(OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True))

    def test_evaluate_guarded_attach_reports_most_specific_failure(self):
        self.assertEqual(OA.evaluate_guarded_attach("", True, True, True, True)[1], "guarded_attach_no_path")
        self.assertEqual(OA.evaluate_guarded_attach("p", False, True, True, True)[1],
                         "guarded_attach_terminal_not_running")
        self.assertEqual(OA.evaluate_guarded_attach("p", True, False, True, True)[1],
                         "guarded_attach_initialize_failed")
        self.assertEqual(OA.evaluate_guarded_attach("p", True, True, False, True)[1], "guarded_attach_not_connected")
        self.assertEqual(OA.evaluate_guarded_attach("p", True, True, True, False)[1], "guarded_attach_no_account")
        self.assertEqual(OA.evaluate_guarded_attach("p", True, True, True, True), (True, "ok"))

    def test_no_legacy_bridge_or_http_import(self):
        # The whole point of decoupling: the observer's attach must NOT import the legacy bridge or its heavy
        # deps. Inspect the ACTUAL imports via the AST (the docstring may *mention* the legacy module to explain
        # the decoupling - that is fine; only a real import statement is a violation).
        import ast
        src = open(OA.__file__, "r", encoding="ascii").read()
        modules = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
        self.assertNotIn("scripts", modules)     # not the legacy bridge
        self.assertNotIn("requests", modules)    # nor its heavy transitive deps
        self.assertNotIn("urllib3", modules)


class ParityTests(SimpleTestCase):
    """observer_attach must behave identically to the certified scripts.mt5_signal_bridge helpers."""

    def _legacy(self):
        # scripts/ lives at the repo root (NOT on the backend import path - which is exactly why the observer
        # was decoupled from it). Add it only for this parity comparison; skip if the legacy deps are absent.
        import sys
        import hosted_workspace
        repo = os.path.dirname(os.path.dirname(os.path.dirname(hosted_workspace.__file__)))
        if repo not in sys.path:
            sys.path.insert(0, repo)
        try:
            from scripts import mt5_signal_bridge as legacy
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"legacy bridge unimportable for parity check: {exc}")
        return legacy

    def test_evaluate_guarded_attach_parity(self):
        legacy = self._legacy()
        cases = [
            ("", True, True, True, True), ("p", False, True, True, True), ("p", True, False, True, True),
            ("p", True, True, False, True), ("p", True, True, True, False), ("p", True, True, True, True),
        ]
        for c in cases:
            self.assertEqual(OA.evaluate_guarded_attach(*c), legacy.evaluate_guarded_attach(*c), c)

    def test_guarded_initialize_parity_matrix(self):
        legacy = self._legacy()
        scenarios = [
            {"init_ok": True, "term": _Term(True), "acc": _Acc()},     # connected + account
            {"init_ok": True, "term": _Term(False), "acc": None},      # attached, not connected
            {"init_ok": False, "term": None, "acc": None},             # initialize failed
            {"init_ok": True, "term": _Term(True), "acc": None},       # connected, no account
        ]
        for guarded in ("1", ""):
            with mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": guarded}):
                for running in (True, False):
                    for sc in scenarios:
                        a = OA.guarded_initialize(_Mt5(**sc), {"path": _PATH}, probe=lambda p: running)
                        b = legacy.guarded_initialize(_Mt5(**sc), {"path": _PATH}, probe=lambda p: running)
                        self.assertEqual(a, b, (guarded, running, sc))
