"""ADR-0034 / WS3 (M1) — Guarded Attach primitive.

Proves the bridge's never-launch attach:
- ``evaluate_guarded_attach`` (pure): fail-closed decision ordering (no_path → terminal_not_running →
  initialize_failed → not_connected → no_account → ok). Oracle truth-table + AST operator-mutation adequacy
  (every mutant killed) + a non-vacuous-oracle control.
- ``guarded_initialize`` (wrapper): DARK by default (MT5_GUARDED_ATTACH unset ⇒ byte-identical passthrough to
  mt5.initialize — behaviour-preserving for the production bridge); when enabled it probes the process BEFORE
  initialize so a DOWN terminal is NEVER launched, requires broker-connected + an account identity, never
  calls mt5.login(), and releases (shutdown) any attach it opened on failure.
- Structural never-launch invariant: probe precedes initialize, initialize is guarded by `running`, no
  mt5.login in the wrapper, and all 10 bridge initialize sites route through guarded_initialize.
"""
import ast
import copy
import inspect
import os
import re
import textwrap
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_binding import _FakeInfo, _load_bridge

_MOD = _load_bridge()
_eval = _MOD.evaluate_guarded_attach


# (label, path, process_running, init_ok, terminal_connected, account_present, want_ok, want_reason)
CASES = [
    ("ok", "C:\\t\\terminal64.exe", True, True, True, True, True, "ok"),
    ("no_path", None, True, True, True, True, False, "guarded_attach_no_path"),
    ("not_running", "C:\\t\\terminal64.exe", False, True, True, True, False, "guarded_attach_terminal_not_running"),
    ("init_failed", "C:\\t\\terminal64.exe", True, False, True, True, False, "guarded_attach_initialize_failed"),
    ("not_connected", "C:\\t\\terminal64.exe", True, True, False, True, False, "guarded_attach_not_connected"),
    ("no_account", "C:\\t\\terminal64.exe", True, True, True, False, False, "guarded_attach_no_account"),
]


class EvaluateGuardedAttachTests(SimpleTestCase):
    def test_every_case(self):
        for label, path, running, init_ok, conn, acct, want_ok, want_reason in CASES:
            with self.subTest(label):
                ok, reason = _eval(path, running, init_ok, conn, acct)
                self.assertEqual(ok, want_ok, label)
                self.assertEqual(reason, want_reason, label)

    def test_not_running_is_reported_before_init(self):
        # The never-launch reason must win over a (hypothetical) successful init — the decision that keeps
        # a down terminal from being launched.
        ok, reason = _eval("C:\\t\\terminal64.exe", False, True, True, True)
        self.assertFalse(ok)
        self.assertEqual(reason, "guarded_attach_terminal_not_running")


_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
         ast.And: ast.Or, ast.Or: ast.And}


class _Mutant(ast.NodeTransformer):
    def __init__(self, target):
        self.i = -1
        self.target = target

    def _hit(self):
        self.i += 1
        return self.i == self.target

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
            if self._hit():
                node.ops[0] = _SWAP[type(node.ops[0])]()
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, (ast.And, ast.Or)):
            if self._hit():
                node.op = _SWAP[type(node.op)]()
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            if self._hit():
                return node.operand
        return node


def _compile_mutant(tree):
    ns = {"__builtins__": __builtins__}
    exec(compile(tree, "<mutant>", "exec"), ns)
    return ns["evaluate_guarded_attach"]


def _results(fn):
    out = []
    for _, path, running, init_ok, conn, acct, _, _ in CASES:
        try:
            out.append(fn(path, running, init_ok, conn, acct))
        except Exception as exc:  # a mutant that crashes on an oracle case is DISTINGUISHED (killed)
            out.append(("RAISED", type(exc).__name__))
    return out


class MutationAdequacyTests(SimpleTestCase):
    def setUp(self):
        self.tree = ast.parse(textwrap.dedent(inspect.getsource(_eval)))
        c = _Mutant(-1)
        c.visit(copy.deepcopy(self.tree))
        self.total = c.i + 1
        self.baseline = _results(_eval)

    def test_has_operators(self):
        self.assertGreaterEqual(self.total, 5)  # five `not` guards

    def test_every_mutant_killed(self):
        survivors = []
        for t in range(self.total):
            tree_t = copy.deepcopy(self.tree)
            _Mutant(t).visit(tree_t)
            ast.fix_missing_locations(tree_t)
            if _results(_compile_mutant(tree_t)) == self.baseline:
                survivors.append(t)
        self.assertEqual(survivors, [], f"unkilled mutants: {survivors}")

    def test_oracle_not_vacuous(self):
        self.assertNotEqual([(True, "ok") for _ in CASES], self.baseline)


class _RecordingMt5:
    """Minimal MT5 double that records initialize/login/shutdown so the never-launch + no-login invariants
    are directly observable."""

    def __init__(self, *, init_result=True, connected=True, term_none=False, acc_none=False,
                 term_raises=False, acc_raises=False,
                 login=123456, server="Broker-Demo", trade_mode=0):
        self.init_calls = 0
        self.login_calls = 0
        self.shutdown_calls = 0
        self._init_result = init_result
        self._connected = connected
        self._term_none = term_none
        self._term_raises = term_raises
        self._acc_raises = acc_raises
        self._acc = None if acc_none else _FakeInfo(login=login, server=server, trade_mode=trade_mode)

    def initialize(self, **kw):
        self.init_calls += 1
        return self._init_result

    def login(self, *a, **k):  # must never be called
        self.login_calls += 1
        return True

    def terminal_info(self):
        if self._term_raises:
            raise RuntimeError("IPC pipe broken")
        return None if self._term_none else _FakeInfo(connected=self._connected)

    def account_info(self):
        if self._acc_raises:
            raise RuntimeError("IPC pipe broken")
        return self._acc

    def shutdown(self):
        self.shutdown_calls += 1

    def last_error(self):
        return (-1, "err")


_PATH = {"path": "C:\\GuvFX\\accounts\\1\\terminal\\terminal64.exe"}


def _guarded(**env):
    e = {"MT5_GUARDED_ATTACH": "1"}
    e.update(env)
    return mock.patch.dict(os.environ, e, clear=False)


def _legacy():
    return mock.patch.dict(os.environ, {"MT5_GUARDED_ATTACH": ""}, clear=False)


class GuardedInitializeWrapperTests(SimpleTestCase):
    def test_legacy_passthrough_true(self):
        m = _RecordingMt5(init_result=True)
        with _legacy():
            self.assertTrue(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: False))
        self.assertEqual(m.init_calls, 1)   # called exactly once (passthrough)
        self.assertEqual(m.login_calls, 0)
        self.assertEqual(m.shutdown_calls, 0)

    def test_legacy_passthrough_false(self):
        m = _RecordingMt5(init_result=False)
        with _legacy():
            self.assertFalse(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: True))
        self.assertEqual(m.init_calls, 1)

    def test_guarded_success(self):
        m = _RecordingMt5(init_result=True, connected=True)
        with _guarded():
            self.assertTrue(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: True))
        self.assertEqual(m.init_calls, 1)
        self.assertEqual(m.login_calls, 0)

    def test_guarded_never_launches_when_not_running(self):
        # THE core safety invariant: probe says not running ⇒ initialize is NEVER called (no launch), and
        # therefore no cached-credential auto-login can occur.
        m = _RecordingMt5(init_result=True)
        with _guarded():
            self.assertFalse(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: False))
        self.assertEqual(m.init_calls, 0)
        self.assertEqual(m.login_calls, 0)

    def test_guarded_no_path_never_launches(self):
        m = _RecordingMt5()
        with _guarded():
            self.assertFalse(_MOD.guarded_initialize(m, {}, probe=lambda p: True))
        self.assertEqual(m.init_calls, 0)

    def test_guarded_init_failed(self):
        m = _RecordingMt5(init_result=False)
        with _guarded():
            self.assertFalse(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: True))
        self.assertEqual(m.init_calls, 1)
        self.assertEqual(m.shutdown_calls, 0)  # nothing attached ⇒ nothing to release

    def test_guarded_not_connected_releases_attach(self):
        m = _RecordingMt5(init_result=True, connected=False)
        with _guarded():
            self.assertFalse(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: True))
        self.assertEqual(m.init_calls, 1)
        self.assertEqual(m.shutdown_calls, 1)  # released the attach we opened
        self.assertEqual(m.login_calls, 0)

    def test_guarded_no_account_releases_attach(self):
        m = _RecordingMt5(init_result=True, connected=True, acc_none=True)
        with _guarded():
            self.assertFalse(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: True))
        self.assertEqual(m.shutdown_calls, 1)

    def test_guarded_never_calls_login_on_any_path(self):
        for kw in (dict(init_result=True, connected=True),
                   dict(init_result=True, connected=False),
                   dict(init_result=False)):
            for running in (True, False):
                m = _RecordingMt5(**kw)
                with _guarded():
                    _MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: running)
                self.assertEqual(m.login_calls, 0)

    def test_guarded_rejects_credentials_attach_only(self):
        # initialize(login,password,server) AUTHENTICATES — the guarded path must refuse credential-bearing
        # init_kwargs and NEVER call initialize (no attach, no auth).
        for extra in ({"login": 999}, {"password": "x"}, {"server": "S"},
                      {"login": 999, "password": "x", "server": "S"}):
            m = _RecordingMt5(init_result=True)
            kwargs = dict(_PATH)
            kwargs.update(extra)
            with _guarded():
                self.assertFalse(_MOD.guarded_initialize(m, kwargs, probe=lambda p: True), extra)
            self.assertEqual(m.init_calls, 0, extra)
            self.assertEqual(m.login_calls, 0, extra)

    def test_legacy_passthrough_still_allows_credentials(self):
        # Flag OFF: the legacy /mt5/login-and-validate path is unchanged — it still initialises w/ creds.
        m = _RecordingMt5(init_result=True)
        kwargs = dict(_PATH)
        kwargs.update({"login": 999, "password": "x", "server": "S"})
        with _legacy():
            self.assertTrue(_MOD.guarded_initialize(m, kwargs, probe=lambda p: False))
        self.assertEqual(m.init_calls, 1)

    def test_guarded_fail_closed_when_terminal_info_raises(self):
        # A raising IPC call after a successful attach must fail closed (no raise) AND release the attach.
        m = _RecordingMt5(init_result=True, term_raises=True)
        with _guarded():
            self.assertFalse(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: True))
        self.assertEqual(m.init_calls, 1)
        self.assertEqual(m.shutdown_calls, 1)
        self.assertEqual(m.login_calls, 0)

    def test_guarded_fail_closed_when_account_info_raises(self):
        m = _RecordingMt5(init_result=True, connected=True, acc_raises=True)
        with _guarded():
            self.assertFalse(_MOD.guarded_initialize(m, dict(_PATH), probe=lambda p: True))
        self.assertEqual(m.shutdown_calls, 1)


class ProbePathScopingTests(SimpleTestCase):
    """_terminal_process_running matches strictly by INSTALL DIRECTORY (never image-name alone), so a foreign
    terminal64.exe never green-lights launching a down target. Forward-slash paths keep this portable on the
    POSIX CI host (os.path parses them on every platform)."""

    TARGET = "/opt/mt5/acct1/terminal/terminal64.exe"

    def _dir(self, exe):
        return os.path.dirname(os.path.abspath(exe)).lower()

    def test_no_path_is_false(self):
        self.assertFalse(_MOD._terminal_process_running(None))
        self.assertFalse(_MOD._terminal_process_running(""))

    def test_foreign_directory_is_false(self):
        foreign = self._dir("/opt/mt5/other/terminal64.exe")
        with mock.patch.object(_MOD, "_running_terminal_dirs", return_value={foreign}):
            self.assertFalse(_MOD._terminal_process_running(self.TARGET))

    def test_matching_directory_is_true(self):
        with mock.patch.object(_MOD, "_running_terminal_dirs", return_value={self._dir(self.TARGET)}):
            self.assertTrue(_MOD._terminal_process_running(self.TARGET))

    def test_empty_process_set_fail_closed(self):
        with mock.patch.object(_MOD, "_running_terminal_dirs", return_value=set()):
            self.assertFalse(_MOD._terminal_process_running(self.TARGET))


class GuardedAttachStructureTests(SimpleTestCase):
    """The never-launch invariant is asserted structurally so it cannot be silently removed."""

    def setUp(self):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self.src = open(os.path.join(repo, "scripts", "mt5_signal_bridge.py"), encoding="utf-8").read()
        m = re.search(r"\ndef guarded_initialize\(", self.src)
        self.assertIsNotNone(m)
        rest = self.src[m.end():]
        nxt = re.search(r"\ndef ", rest)
        self.body = rest[: nxt.start()] if nxt else rest
        # The never-launch invariant concerns the GUARDED path only; the legacy passthrough (unchanged) is
        # allowed to launch. Scope structural checks to the code AFTER the passthrough return so the
        # docstring + the passthrough's own mt5.initialize() do not confound the ordering assertions.
        self.guarded = self.body[self.body.find("legacy passthrough"):]

    def test_probe_precedes_initialize(self):
        probe = self.guarded.find("probe(path)")
        # The real attach is `bool(mt5.initialize(**init_kwargs))`; match that exact call form so the
        # credential-rejection comment (which mentions mt5.initialize) does not confound the ordering.
        init = self.guarded.find("bool(mt5.initialize(")
        self.assertGreater(probe, -1, "no probe(path) in guarded path")
        self.assertGreater(init, -1, "no attach call in guarded path")
        self.assertLess(probe, init, "probe(path) must precede the attach in the guarded path")

    def test_initialize_is_guarded_by_running(self):
        # The attach call sits under `if running:` (now inside a try:) — a down terminal never reaches it.
        guarded_call = re.search(r"if running:\s*\n\s*try:\s*\n\s*init_ok = bool\(mt5\.initialize\(", self.body)
        self.assertIsNotNone(guarded_call, "attach initialize must be guarded by `if running:`")

    def test_guarded_rejects_credential_keys(self):
        # Attach-only invariant, locked structurally: the guarded path refuses login/password/server.
        self.assertRegex(self.guarded, r'any\(k in init_kwargs for k in \("login", "password", "server"\)\)')

    def test_no_login_in_wrapper(self):
        # No credential replay: the guarded path never authenticates. (Scoped past the docstring, which
        # only *documents* the invariant.)
        self.assertNotIn("mt5.login(", self.guarded)

    def test_all_call_sites_route_through_guarded_initialize(self):
        self.assertEqual(self.src.count("if not guarded_initialize(mt5, init_kwargs):"), 10)
        # The only remaining raw mt5.initialize(**init_kwargs) calls are the two inside the wrapper.
        self.assertEqual(self.src.count("bool(mt5.initialize(**init_kwargs))"), 2)
        self.assertNotIn("if not mt5.initialize(**init_kwargs):", self.src)
