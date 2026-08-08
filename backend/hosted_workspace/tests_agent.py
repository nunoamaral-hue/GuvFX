"""ADR-0034 / M3b-2 — Hosted Workspace Agent (read-only observation pipeline).

Mock-MT5 tests over the injected host boundary: oracle + AST mutation adequacy on the orchestration control
flow + missing/duplicate/wrong process + attach failure/raise + account unavailable + wrong login/server +
trade-mode variants + empty/open positions + pending orders + tick present/absent + clock failure +
never-launch/never-read-on-failure invariants + no-state-derivation proof + secret-free + exception safety.
The agent is pure orchestration, so tests need no DB, no mocks of MT5 itself — only a mock host boundary.
"""
import ast
import copy
import inspect
import os
import textwrap

from django.test import SimpleTestCase

from hosted_workspace.manager import WorkspaceObservation
from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason
from hosted_workspace.agent import (
    AttachOutcome,
    HostReadState,
    ProcessProbe,
    WorkspaceSpec,
    build_agent_snapshot,
    observe_workspace,
)


def _spec(**kw):
    base = dict(workspace_id="ws-1", expected_login="12345", expected_server="Demo",
                target_path="C:/w/terminal64.exe", target_pid=4242, freshness_limit_seconds=60.0,
                tick_symbol="EURUSD")
    base.update(kw)
    return WorkspaceSpec(**base)


_UNSET = object()  # sentinel: account is None (unavailable) when login is _UNSET


class _RaisingAttr:
    """A probe/attach-like object whose ONE named attribute raises a non-AttributeError on access (a hostile
    @property), while other attributes are plain values. Models an injected/untrusted host result object."""

    def __init__(self, raise_on, **fixed):
        self.__dict__.update(fixed)
        self.__dict__["_raise_on"] = raise_on

    def __getattr__(self, name):  # only called for attributes not in __dict__
        if name == self.__dict__.get("_raise_on"):
            raise RuntimeError(f"{name} boom")
        raise AttributeError(name)


def _state(login="12345", server="Demo", trade_mode=0, connected=True, trade_allowed=True,
           positions=0, orders=0, tick=True):
    return HostReadState(
        terminal={"connected": connected, "trade_allowed": trade_allowed},
        account=None if login is _UNSET else {"login": login, "server": server, "trade_mode": trade_mode},
        position_count=positions, order_count=orders, tick_present=tick)


class MockHost:
    """Configurable host boundary. Records attach/read/release calls so tests can prove never-launch and
    read-only invariants (no attach on a down terminal; no read on a failed attach)."""

    def __init__(self, *, probe=None, attach=None, state=None,
                 locate_raises=False, attach_raises=False, read_raises=False):
        self._probe = probe if probe is not None else ProcessProbe(running=True, pid=4242)
        self._attach = (attach if attach is not None
                        else AttachOutcome(attempted=True, ok=True, ipc_available=True, reason="ok"))
        self._state = state if state is not None else _state()
        self._locate_raises = locate_raises
        self._attach_raises = attach_raises
        self._read_raises = read_raises
        self.attach_calls = 0
        self.read_calls = 0
        self.released = 0

    def locate(self, spec):
        if self._locate_raises:
            raise RuntimeError("locate boom")
        return self._probe

    def attach(self, spec):
        self.attach_calls += 1
        if self._attach_raises:
            raise RuntimeError("attach boom")
        return self._attach

    def read_state(self, spec):
        self.read_calls += 1
        if self._read_raises:
            raise RuntimeError("read boom")
        return self._state

    def release(self):
        self.released += 1


def _clock(v=1000.0):
    return lambda: v


def _observe(host, spec=None, *, clock=None, previous_state=S.CONNECTED):
    return observe_workspace(host, spec or _spec(), clock=clock or _clock(),
                             previous_state=str(previous_state))


def _facts(obs):
    return (obs.process_running, obs.ipc_available, obs.connected, obs.account_match, obs.trade_allowed,
            obs.fresh)


class OracleTests(SimpleTestCase):
    def test_happy_path_all_true(self):
        obs = _observe(MockHost())
        self.assertIsInstance(obs, WorkspaceObservation)
        self.assertEqual(_facts(obs), (True, True, True, True, True, True))

    def test_snapshot_fields_populated(self):
        host = MockHost()
        snap = build_agent_snapshot(host, _spec(), clock=_clock())
        self.assertTrue(snap.process_running and snap.attach_succeeded and snap.ipc_available
                        and snap.terminal_connected and snap.trade_allowed)
        self.assertEqual((snap.observed_login, snap.observed_server, snap.observed_trade_mode),
                         ("12345", "Demo", 0))
        self.assertEqual(snap.observed_at, 1000.0)
        self.assertEqual((host.attach_calls, host.read_calls, host.released), (1, 1, 1))

    def test_previous_state_carried_not_derived(self):
        obs = _observe(MockHost(), previous_state=S.EXECUTION_READY)
        self.assertEqual(obs.previous_state, str(S.EXECUTION_READY))


class MissingProcessTests(SimpleTestCase):
    def test_missing_process_never_attaches(self):  # missing process + never launch
        host = MockHost(probe=ProcessProbe(running=False, reason="terminal_not_running"))
        obs = _observe(host)
        self.assertEqual(_facts(obs), (False, False, False, False, False, True))  # fresh obs, no facts
        self.assertEqual((host.attach_calls, host.read_calls, host.released), (0, 0, 0))

    def test_duplicate_process_refuses_attach(self):  # ambiguous target -> never attach
        host = MockHost(probe=ProcessProbe(running=True, duplicate=True, reason="dup"))
        snap = build_agent_snapshot(host, _spec(), clock=_clock())
        self.assertTrue(snap.process_running)    # running was observed True...
        self.assertFalse(snap.attach_succeeded)  # ...but the ambiguous target was never attached
        self.assertEqual((host.attach_calls, host.read_calls), (0, 0))

    def test_wrong_path_not_running_fails_closed(self):  # wrong path -> expected terminal not running
        host = MockHost(probe=ProcessProbe(running=False, reason="terminal_not_running"))
        snap = build_agent_snapshot(host, _spec(target_path="C:/other/terminal64.exe"), clock=_clock())
        self.assertFalse(snap.attach_attempted)
        self.assertEqual(host.attach_calls, 0)

    def test_truthy_nonbool_duplicate_fails_closed(self):  # adversarial LOW: ambiguity gate must fail closed
        # A host that signals ambiguity with a truthy non-bool (duplicate=1) must still refuse to attach —
        # the safety gate clears ONLY on an explicit ``duplicate is False``.
        for amb in (1, "yes", object()):
            host = MockHost(probe=ProcessProbe(running=True, duplicate=amb, reason="dup"))
            snap = build_agent_snapshot(host, _spec(), clock=_clock())
            self.assertFalse(snap.attach_succeeded, amb)
            self.assertEqual(host.attach_calls, 0, amb)


class AttachTests(SimpleTestCase):
    def test_attach_failure_blocks_ipc_and_reads(self):  # attach failure -> ipc_available=false, no read
        host = MockHost(attach=AttachOutcome(attempted=True, ok=False, reason="guarded_attach_not_connected"))
        snap = build_agent_snapshot(host, _spec(), clock=_clock())
        self.assertTrue(snap.process_running and snap.attach_attempted)
        self.assertFalse(snap.attach_succeeded or snap.ipc_available)
        self.assertEqual((host.attach_calls, host.read_calls, host.released), (1, 0, 1))

    def test_attach_ok_but_ipc_unavailable(self):
        host = MockHost(attach=AttachOutcome(attempted=True, ok=True, ipc_available=False, reason="ok"))
        snap = build_agent_snapshot(host, _spec(), clock=_clock())
        self.assertTrue(snap.attach_succeeded)
        self.assertFalse(snap.ipc_available)        # ipc only when attach ok AND ipc reported
        self.assertEqual(host.read_calls, 1)        # still proceeds to read

    def test_attach_raises_fails_closed(self):
        host = MockHost(attach_raises=True)
        obs = _observe(host)
        self.assertEqual(_facts(obs)[:5], (True, False, False, False, False))
        self.assertEqual(host.read_calls, 0)

    def test_no_retry_on_attach_failure(self):
        host = MockHost(attach=AttachOutcome(attempted=True, ok=False, reason="x"))
        build_agent_snapshot(host, _spec(), clock=_clock())
        self.assertEqual(host.attach_calls, 1)      # never retried


class ReadStateTests(SimpleTestCase):
    def test_read_raises_fails_closed_but_attached(self):
        host = MockHost(read_raises=True)
        snap = build_agent_snapshot(host, _spec(), clock=_clock())
        self.assertTrue(snap.attach_succeeded and snap.ipc_available)
        self.assertFalse(snap.terminal_connected or snap.trade_allowed)
        self.assertIsNone(snap.observed_login)
        self.assertEqual(snap.connection_reason, "read_error")
        self.assertEqual(host.released, 1)

    def test_account_unavailable(self):  # account unavailable -> account_match false
        host = MockHost(state=_state(login=_UNSET))
        obs = _observe(host)
        self.assertFalse(obs.account_match)

    def test_disconnected_terminal(self):
        host = MockHost(state=_state(connected=False))
        obs = _observe(host)
        self.assertFalse(obs.connected)

    def test_trade_not_allowed(self):
        host = MockHost(state=_state(trade_allowed=False))
        self.assertFalse(_observe(host).trade_allowed)


class AccountIdentityTests(SimpleTestCase):
    def test_wrong_login(self):
        self.assertFalse(_observe(MockHost(state=_state(login="99999"))).account_match)

    def test_wrong_server(self):
        self.assertFalse(_observe(MockHost(state=_state(server="Other"))).account_match)

    def test_correct_identity_matches(self):
        self.assertTrue(_observe(MockHost(state=_state(login="12345", server="Demo"))).account_match)

    def test_trade_mode_variants(self):
        self.assertTrue(_observe(MockHost(state=_state(trade_mode=0))).account_match)     # DEMO -> ok
        self.assertFalse(_observe(MockHost(state=_state(trade_mode=2))).account_match)    # REAL -> deny
        self.assertFalse(_observe(MockHost(state=_state(trade_mode=None))).account_match)  # unknown -> deny
        self.assertFalse(_observe(MockHost(state=_state(trade_mode=False))).account_match)  # bool -> deny

    def test_int_login_normalised_to_string(self):  # mt5 login is an int
        snap = build_agent_snapshot(MockHost(state=_state(login=12345)), _spec(expected_login="12345"),
                                    clock=_clock())
        self.assertEqual(snap.observed_login, "12345")


class ReadOnlySurfaceTests(SimpleTestCase):
    """Positions / orders / tick are read but do NOT affect the observation (read-only, no gating)."""

    def _obs_for(self, **state_kw):
        return _facts(_observe(MockHost(state=_state(**state_kw))))

    def test_empty_positions(self):
        self.assertEqual(self._obs_for(positions=0), (True, True, True, True, True, True))

    def test_open_positions_do_not_change_observation(self):
        self.assertEqual(self._obs_for(positions=3), (True, True, True, True, True, True))

    def test_pending_orders_do_not_change_observation(self):
        self.assertEqual(self._obs_for(orders=5), (True, True, True, True, True, True))

    def test_tick_present_and_absent_equivalent(self):
        self.assertEqual(self._obs_for(tick=True), self._obs_for(tick=False))


class FreshnessAndClockTests(SimpleTestCase):
    def test_observed_at_from_injected_clock(self):
        snap = build_agent_snapshot(MockHost(), _spec(), clock=_clock(1234.5))
        self.assertEqual(snap.observed_at, 1234.5)

    def test_clock_failure_fails_closed_not_fresh(self):
        def boom():
            raise RuntimeError("no clock")
        obs = observe_workspace(MockHost(), _spec(), clock=boom, previous_state=str(S.CONNECTED))
        self.assertFalse(obs.fresh)
        self.assertIsNone(obs.observed_at)

    def test_non_finite_clock_fails_closed(self):
        obs = observe_workspace(MockHost(), _spec(), clock=lambda: float("nan"),
                                previous_state=str(S.CONNECTED))
        self.assertFalse(obs.fresh)
        self.assertIsNone(obs.observed_at)


class ExceptionSafetyTests(SimpleTestCase):
    def test_locate_raises_never_propagates(self):
        obs = _observe(MockHost(locate_raises=True))
        self.assertEqual(_facts(obs)[:5], (False, False, False, False, False))

    def test_every_stage_raise_returns_observation(self):
        for kw in ({"locate_raises": True}, {"attach_raises": True}, {"read_raises": True}):
            obs = _observe(MockHost(**kw))
            self.assertIsInstance(obs, WorkspaceObservation)

    def test_no_default_positive_from_empty_reads(self):
        host = MockHost(state=HostReadState(terminal=None, account=None))
        obs = _observe(host)
        self.assertEqual((obs.connected, obs.trade_allowed, obs.account_match), (False, False, False))

    def test_untrusted_account_get_raising_fails_closed_and_releases(self):  # adversarial MEDIUM regression
        # An injected/untrusted host whose account mapping .get raises during extraction must NOT propagate an
        # exception to the consumer AND must still release the attach exactly once (no dangling IPC handle).
        class _RaisingMapping(dict):
            def get(self, *a, **k):
                raise RuntimeError("get boom")
        host = MockHost(state=HostReadState(terminal={"connected": True, "trade_allowed": True},
                                            account=_RaisingMapping()))
        obs = _observe(host)
        self.assertIsInstance(obs, WorkspaceObservation)
        self.assertEqual((obs.ipc_available, obs.account_match, obs.connected), (True, False, False))
        self.assertEqual(host.released, 1)  # released exactly once via finally

    def test_untrusted_identity_str_raising_degrades_to_none(self):  # adversarial MEDIUM regression
        class _BoomStr:
            def __str__(self):
                raise RuntimeError("str boom")
        host = MockHost(state=_state(login=_BoomStr()))
        snap = build_agent_snapshot(host, _spec(), clock=_clock())
        self.assertIsNone(snap.observed_login)  # str() failure -> explicit unknown, never an exception
        self.assertEqual(host.released, 1)

    def test_raising_attach_result_attr_fails_closed_and_releases(self):  # re-verify MEDIUM regression
        # An untrusted attach RESULT whose attribute access raises AFTER host.attach opened a handle must
        # fail closed (no exception to consumer) AND release exactly once (no dangling IPC attach).
        host = MockHost(attach=_RaisingAttr("ipc_available", attempted=True, ok=True, reason="ok"))
        obs = _observe(host)
        self.assertIsInstance(obs, WorkspaceObservation)
        self.assertEqual(_facts(obs)[:5], (True, False, False, False, False))
        self.assertEqual(host.released, 1)  # finally released the (possibly-opened) attach

    def test_raising_probe_attr_fails_closed_never_attaches(self):  # re-verify MEDIUM regression
        host = MockHost(probe=_RaisingAttr("running", duplicate=False, reason="", pid=1))
        obs = _observe(host)
        self.assertEqual(_facts(obs)[:5], (False, False, False, False, False))
        self.assertEqual((host.attach_calls, host.released), (0, 0))  # never attached -> nothing to release


class SecretFreeTests(SimpleTestCase):
    def test_observed_identity_not_emitted(self):
        host = MockHost(state=_state(login="SUPERSECRET123", server="SRVSECRET"))
        obs = observe_workspace(host, _spec(expected_login="SUPERSECRET123", expected_server="SRVSECRET"),
                                clock=_clock(), previous_state=str(S.CONNECTED))
        self.assertNotIn("SUPERSECRET123", str(obs))
        self.assertNotIn("SRVSECRET", str(obs))


class NoStateDerivationTests(SimpleTestCase):
    def test_agent_never_derives_lifecycle_state(self):
        src = open(os.path.join(os.path.dirname(__file__), "agent.py"), encoding="utf-8").read()
        self.assertNotIn("WorkspaceLifecycleState", src)
        self.assertNotIn("evaluate_workspace_transition", src)
        self.assertNotIn("derive_workspace_decision", src)

    def test_agent_never_launches_or_logs_in(self):
        # AST-based (docstring/comment prose can never confound it): the agent makes no login/initialize
        # call, spawns no process, and does not import MetaTrader5.
        src = open(os.path.join(os.path.dirname(__file__), "agent.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        called_attrs, called_names = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    called_attrs.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
            if isinstance(node, ast.Import):
                for n in node.names:
                    self.assertNotIn("MetaTrader5", n.name)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("MetaTrader5", node.module or "")
        for forbidden in ("login", "initialize", "shutdown", "Popen", "system"):
            self.assertNotIn(forbidden, called_attrs, forbidden)
        for forbidden in ("Popen", "exec", "eval"):
            self.assertNotIn(forbidden, called_names, forbidden)


# --- AST mutation adequacy on the orchestration control flow ----------------------------------------------
_SWAP = {ast.And: ast.Or, ast.Or: ast.And, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot,
         ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_CMP = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)


class _Mutant(ast.NodeTransformer):
    def __init__(self, target):
        self.i, self.target = -1, target

    def _hit(self):
        self.i += 1
        return self.i == self.target

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], _CMP) and self._hit():
            node.ops[0] = _SWAP[type(node.ops[0])]()
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, (ast.And, ast.Or)) and self._hit():
            node.op = _SWAP[type(node.op)]()
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._hit():
            return node.operand
        return node


def _factory_happy():
    return MockHost()


def _factory_missing():
    return MockHost(probe=ProcessProbe(running=False, reason="terminal_not_running"))


def _factory_duplicate():
    return MockHost(probe=ProcessProbe(running=True, duplicate=True, reason="dup"))


def _factory_attach_fail():
    return MockHost(attach=AttachOutcome(attempted=True, ok=False, reason="x"))


def _factory_ipc_false():
    return MockHost(attach=AttachOutcome(attempted=True, ok=True, ipc_available=False, reason="ok"))


def _factory_read_raise():
    return MockHost(read_raises=True)


def _factory_attach_raise():
    return MockHost(attach_raises=True)


# (label, host_factory) — the mutation oracle. Chosen so every And/Or/Not/Is-comparison mutant in
# build_agent_snapshot changes at least one observable (snapshot fact / reason string / host call-count).
_AGENT_CASES = [_factory_happy, _factory_missing, _factory_duplicate, _factory_attach_fail, _factory_ipc_false,
                _factory_read_raise, _factory_attach_raise]


def _snap_signature(fn, factory):
    host = factory()
    snap = fn(host, _spec(), clock=_clock())
    return (snap.process_running, snap.attach_attempted, snap.attach_succeeded, snap.ipc_available,
            snap.terminal_connected, snap.trade_allowed, snap.observed_login, snap.observed_server,
            snap.observed_trade_mode, snap.workspace_id, snap.process_reason, snap.attach_reason,
            snap.connection_reason, host.attach_calls, host.read_calls, host.released)


class AgentMutationTests(SimpleTestCase):
    def setUp(self):
        import hosted_workspace.agent as mod
        self.mod = mod
        self.tree = ast.parse(textwrap.dedent(inspect.getsource(build_agent_snapshot)))
        c = _Mutant(-1)
        c.visit(copy.deepcopy(self.tree))
        self.total = c.i + 1
        self.baseline = [_snap_signature(build_agent_snapshot, f) for f in _AGENT_CASES]

    def test_has_operators(self):
        self.assertGreaterEqual(self.total, 4)

    def test_every_mutant_killed(self):
        survivors = []
        for t in range(self.total):
            tree = copy.deepcopy(self.tree)
            _Mutant(t).visit(tree)
            ast.fix_missing_locations(tree)
            ns = dict(self.mod.__dict__)
            exec(compile(tree, "<m>", "exec"), ns)
            fn = ns["build_agent_snapshot"]
            try:
                result = [_snap_signature(fn, f) for f in _AGENT_CASES]
            except Exception:
                result = "RAISED"
            if result == self.baseline:
                survivors.append(t)
        self.assertEqual(survivors, [], f"unkilled mutants: {survivors}")
