"""STREAM 9E - tests for the observer's self-contained guarded-attach (observer_attach).

Two jobs:
  1. **Invariant lock** - the never-launch / never-login / fail-closed contract of the read-only attach.
  2. **Parity lock** - ``observer_attach`` is a faithful extraction of the certified helpers in
     ``scripts.mt5_signal_bridge``; this asserts the two behave IDENTICALLY across a matrix so the decoupled copy
     can never silently diverge (the review-fakes guard). If the legacy source changes, this test fails until the
     extraction is reconciled.
"""
import os
from types import SimpleNamespace
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
        # inject an empty bare-pid enumerator so the test never depends on the CI host having psutil/wmic
        self.assertTrue(OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                              list_bare_pids=lambda p: set()))
        self.assertFalse(mt5.login_called)

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_guarded_releases_attach_when_not_connected(self):
        mt5 = _Mt5(init_ok=True, term=_Term(False), acc=None)
        self.assertFalse(OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                               list_bare_pids=lambda p: set()))
        self.assertTrue(mt5.shutdown_called)   # a partial attach is released, never left dangling

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_guarded_fail_closed_on_raise(self):
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc(), raise_on={"terminal_info"})
        self.assertFalse(OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                               list_bare_pids=lambda p: set()))

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
        # No terminal ever SPAWNS during these fakes (init just returns a bool), so the observer's attachability
        # gate is inert here and the return value stays identical to the legacy helper. Inject an always-empty
        # bare-pid enumerator so the parity check never depends on the CI host's real process list.
        no_spawn = lambda p: set()
        for guarded in ("1", ""):
            with mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": guarded}):
                for running in (True, False):
                    for sc in scenarios:
                        a = OA.guarded_initialize(_Mt5(**sc), {"path": _PATH}, probe=lambda p: running,
                                                  list_bare_pids=no_spawn)
                        b = legacy.guarded_initialize(_Mt5(**sc), {"path": _PATH}, probe=lambda p: running)
                        self.assertEqual(a, b, (guarded, running, sc))


class AttachabilityGateTests(SimpleTestCase):
    """The never-CREATE guarantee (account-37 forensic). ``mt5.initialize(path=)`` is dual-mode: when the target
    is running but NOT attachable (first-run/busy, or it exits mid-call on a restart/LiveUpdate) it LAUNCHES a
    fresh, bare (non-/portable) terminal itself. The guard must detect that and neutralise EXACTLY what it spawned,
    failing closed - never leaving an extra terminal behind. All process I/O is injected (no psutil/no real procs)."""

    def _seq(self, *snapshots):
        """Fake list_bare_pids(path) returning each snapshot set on successive calls (before, then after)."""
        state = {"i": 0}

        def _f(path):
            i = min(state["i"], len(snapshots) - 1)
            state["i"] = state["i"] + 1
            return set(snapshots[i])
        return _f

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_would_launch_is_neutralised_and_fails_closed(self):
        # before={}, after={4242} => initialize LAUNCHED a bare terminal. Even a 'connected' readback must not be
        # trusted: kill exactly the spawned pid, release the attach, and return False (a hold). Never login.
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        killed = []
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                   list_bare_pids=self._seq(set(), {4242}),
                                   terminate=lambda pid: (killed.append(pid) or True))
        self.assertFalse(ok)
        self.assertEqual(killed, [4242])
        self.assertTrue(mt5.shutdown_called)
        self.assertFalse(mt5.login_called)

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_clean_attach_when_no_new_terminal_spawned(self):
        # before==after => initialize ATTACHED (no launch). Connected + account => True; nothing killed.
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        killed = []
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                   list_bare_pids=self._seq(set(), set()),
                                   terminate=lambda pid: (killed.append(pid) or True))
        self.assertTrue(ok)
        self.assertEqual(killed, [])

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_preexisting_bare_terminal_is_not_killed(self):
        # A bare terminal (555) present in BOTH snapshots is not in our diff and is never killed - the guard only
        # neutralises what IT spawned (a lingering stray is handled by duplicate_terminal + frozen visibility).
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        killed = []
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                   list_bare_pids=self._seq({555}, {555}),
                                   terminate=lambda pid: (killed.append(pid) or True))
        self.assertTrue(ok)
        self.assertEqual(killed, [])

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_multiple_spawned_bare_terminals_all_neutralised(self):
        mt5 = _Mt5(init_ok=False)   # attach failed AND it launched two bare ones
        killed = []
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                   list_bare_pids=self._seq(set(), {11, 22}),
                                   terminate=lambda pid: (killed.append(pid) or True))
        self.assertFalse(ok)
        self.assertEqual(sorted(killed), [11, 22])

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_would_launch_still_fails_closed_when_terminate_fails(self):
        # Even if the kill fails, the result is still a hold (False); downstream duplicate_terminal is the backstop.
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                   list_bare_pids=self._seq(set(), {9}), terminate=lambda pid: False)
        self.assertFalse(ok)

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_down_terminal_never_enumerates_or_kills(self):
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        enum, killed = [], []
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: False,
                                   list_bare_pids=lambda p: (enum.append(p) or set()),
                                   terminate=lambda pid: (killed.append(pid) or True))
        self.assertFalse(ok)
        self.assertIsNone(mt5.initialize_kwargs)   # never attached
        self.assertEqual(enum, [])                 # no enumeration when the terminal is down
        self.assertEqual(killed, [])

    def test_dark_mode_gate_is_inert(self):
        # Guard unset AND force=False => pure passthrough; the attachability gate must not enumerate or kill.
        with mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": ""}):
            mt5 = _Mt5(init_ok=True)
            enum, killed = [], []
            ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                       list_bare_pids=lambda p: (enum.append(p) or set()),
                                       terminate=lambda pid: (killed.append(pid) or True))
            self.assertTrue(ok)
            self.assertEqual(enum, [])
            self.assertEqual(killed, [])

    def test_force_enables_guard_without_env(self):
        # force=True enforces the never-launch guard even when the env is unset (the observer's path), with NO
        # dependence on / mutation of MT5_GUARDED_ATTACH.
        with mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": ""}):
            mt5 = _Mt5(init_ok=True, term=_Term(False), acc=None)   # attached, not connected
            ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True,
                                       list_bare_pids=lambda p: set(), force=True)
            self.assertFalse(ok)                 # guarded path ran (not-connected -> fail closed)
            self.assertTrue(mt5.shutdown_called)
            self.assertEqual(os.environ.get("MT5_GUARDED_ATTACH"), "")   # no global env mutation

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_enum_unavailable_fails_closed_without_attaching(self):
        # If the bare-terminal set cannot be enumerated at all (host has neither psutil nor wmic), we cannot
        # guarantee never-launch -> fail closed WITHOUT ever calling initialize (never risk a launch).
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True, list_bare_pids=lambda p: None)
        self.assertFalse(ok)
        self.assertIsNone(mt5.initialize_kwargs)   # never attached (fail closed before initialize)

    @mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "1"})
    def test_postcheck_unavailable_fails_closed_and_releases(self):
        # Enumeration works before but becomes indeterminate after initialize -> cannot verify no-launch -> release
        # any attach and fail closed.
        mt5 = _Mt5(init_ok=True, term=_Term(True), acc=_Acc())
        seq = iter([set(), None])   # before=ran(empty), after=indeterminate
        ok = OA.guarded_initialize(mt5, {"path": _PATH}, probe=lambda p: True, list_bare_pids=lambda p: next(seq))
        self.assertFalse(ok)
        self.assertTrue(mt5.shutdown_called)


class BareTerminalClassifierControlTests(SimpleTestCase):
    """RULE 11 positive+negative control for the DESTRUCTIVE classifier ``_bare_terminal_pids``: prove the enumerator
    can FIND a bare terminal that IS there (positive) and correctly EXCLUDE a /portable one and an empty-cmdline one
    (negative), on the psutil-absent host path (the wmic fallback that makes the never-CREATE gate robust). Skipped
    where psutil is importable (that branch is covered by the injection tests); this exercises the deployed-like
    no-psutil path where wmic is the measurement tool."""

    def setUp(self):
        try:
            import psutil  # noqa: F401
            self.skipTest("psutil present -> wmic fallback not exercised here; covered by injection tests")
        except Exception:
            pass

    def test_wmic_positive_and_negative_controls(self):
        path = r"C:\GuvFX\accounts\18\terminal\terminal64.exe"
        stdout = (
            "\r\n"
            'CommandLine="C:\\GuvFX\\accounts\\18\\terminal\\terminal64.exe"\r\n'
            "ProcessId=111\r\n"
            "\r\n"
            'CommandLine="C:\\GuvFX\\accounts\\18\\terminal\\terminal64.exe" /portable\r\n'
            "ProcessId=222\r\n"
            "\r\n"
            "CommandLine=\r\n"                     # empty cmdline -> must be EXCLUDED (fail closed for kill)
            "ProcessId=333\r\n"
            "\r\n"
        )
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=stdout)):
            pids = OA._bare_terminal_pids(path)
        # positive: the bare terminal (111) is found; negatives: /portable (222) and empty-cmdline (333) excluded.
        self.assertEqual(pids, {111})

    def test_wmic_unavailable_returns_none_fail_closed(self):
        def _boom(*a, **k):
            raise FileNotFoundError("wmic not found")
        with mock.patch("subprocess.run", _boom):
            self.assertIsNone(OA._bare_terminal_pids(r"C:\GuvFX\accounts\18\terminal\terminal64.exe"))


class ObserverForcesGuardTests(SimpleTestCase):
    """The observer harness enforces the never-launch guard via force=True (NOT via a global env mutation), so every
    account (existing + new) is protected the moment the centrally-staged run_observer.py deploys - independent of
    the per-account scheduled task's env - and the guard can never leak into any other code sharing the process."""

    def test_observe_passes_force_and_does_not_mutate_env(self):
        from terminal_provisioning.windows import run_observer as RO
        captured = {}

        class _Bridge:
            @staticmethod
            def _terminal_process_running(p):
                return True

            @staticmethod
            def guarded_initialize(mt5, kw, **k):
                captured.update(k)
                return False   # hold; we only assert HOW it was called

        with mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": "0"}):
            RO.observe(18, mt5=object(), bridge=_Bridge)
            self.assertTrue(captured.get("force"))                       # observer enforced the never-launch guard
            self.assertEqual(os.environ.get("MT5_GUARDED_ATTACH"), "0")  # and did NOT mutate the global env
