"""ADR-0034 / M3a — Workspace Manager decision engine.

Oracle + AST mutation adequacy (the EXECUTION_READY gate AND the derivation engine) + illegal-transition +
stale-observation + execution-ready negative cases + unknown-observation + graph-fidelity proofs. The engine
is pure/deterministic/side-effect-free, so tests need no DB and no mocks.
"""
import ast
import copy
import inspect
import textwrap

from django.test import SimpleTestCase

from hosted_workspace.state_machine import (
    WORKSPACE_TRANSITIONS,
    WorkspaceLifecycleState as S,
    WorkspaceReason,
    evaluate_workspace_transition,
)
from hosted_workspace.manager import (
    WorkspaceObservation,
    _all_execution_conditions,
    derive_workspace_decision,
)


def _obs(**kw):
    base = dict(process_running=True, ipc_available=True, connected=True, account_match=True,
                trade_allowed=True, fresh=True, previous_state=str(S.CONNECTED),
                previous_reason=str(WorkspaceReason.NONE), observed_at=None)
    base.update(kw)
    return WorkspaceObservation(**base)


class OracleTests(SimpleTestCase):
    def test_execution_ready_from_connected(self):
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        self.assertEqual(d.next_state, str(S.EXECUTION_READY))
        self.assertTrue(d.execution_ready)
        self.assertTrue(d.transition_required)
        self.assertEqual(d.telemetry_event, str(__import__("hosted_workspace.telemetry",
                         fromlist=["WorkspaceEvent"]).WorkspaceEvent.EXECUTION_READY))

    def test_stay_execution_ready_no_transition(self):
        d = derive_workspace_decision(_obs(previous_state=str(S.EXECUTION_READY)))
        self.assertEqual(d.next_state, str(S.EXECUTION_READY))
        self.assertTrue(d.execution_ready)
        self.assertFalse(d.transition_required)
        self.assertIsNone(d.telemetry_event)

    def test_mismatch_suspends(self):
        d = derive_workspace_decision(_obs(previous_state=str(S.EXECUTION_READY), account_match=False))
        self.assertEqual(d.next_state, str(S.SUSPENDED))
        self.assertEqual(d.reason, str(WorkspaceReason.ACCOUNT_MISMATCH))
        self.assertFalse(d.execution_ready)

    def test_disconnect_and_recovery(self):
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED), connected=False))
        self.assertEqual(d.next_state, str(S.DISCONNECTED))
        self.assertTrue(d.recovery_required)
        self.assertFalse(d.execution_ready)
        d2 = derive_workspace_decision(_obs(previous_state=str(S.DISCONNECTED), process_running=False))
        self.assertEqual(d2.next_state, str(S.RECOVERING))
        self.assertTrue(d2.recovery_required)

    def test_provisioning_to_waiting(self):
        d = derive_workspace_decision(_obs(previous_state=str(S.PROVISIONING), process_running=False,
                                           connected=False))
        self.assertEqual(d.next_state, str(S.WAITING_FOR_LOGIN))
        self.assertTrue(d.transition_required)


class ExecutionReadyNegativeTests(SimpleTestCase):
    def test_each_missing_condition_blocks_execution_ready(self):
        for field in ("process_running", "ipc_available", "connected", "account_match", "fresh",
                      "trade_allowed"):
            d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED), **{field: False}))
            self.assertFalse(d.execution_ready, f"{field}=False must block EXECUTION_READY")
            self.assertNotEqual(d.next_state, str(S.EXECUTION_READY), field)


class StaleObservationTests(SimpleTestCase):
    def test_stale_holds_connected_not_ready(self):
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED), fresh=False))
        self.assertEqual(d.next_state, str(S.CONNECTED))
        self.assertEqual(d.reason, str(WorkspaceReason.STALE_OBSERVATION))
        self.assertFalse(d.execution_ready)


class IllegalTransitionTests(SimpleTestCase):
    def test_illegal_target_fails_closed(self):
        # All exec conditions true but previous is WAITING_FOR_LOGIN: WAITING_FOR_LOGIN -> EXECUTION_READY is
        # ILLEGAL, so the decision fails closed (hold previous, ERROR, not execution-ready).
        d = derive_workspace_decision(_obs(previous_state=str(S.WAITING_FOR_LOGIN)))
        self.assertEqual(d.next_state, str(S.WAITING_FOR_LOGIN))
        self.assertEqual(d.reason, str(WorkspaceReason.ERROR))
        self.assertFalse(d.execution_ready)
        self.assertFalse(d.transition_required)


class UnknownObservationTests(SimpleTestCase):
    def test_unknown_previous_state_fails_closed(self):
        d = derive_workspace_decision(_obs(previous_state="BOGUS_STATE"))
        self.assertEqual(d.next_state, "BOGUS_STATE")
        self.assertEqual(d.reason, str(WorkspaceReason.ERROR))
        self.assertFalse(d.execution_ready)
        self.assertFalse(d.transition_required)


class GraphFidelityTests(SimpleTestCase):
    def test_every_required_transition_is_legal(self):
        bools = (True, False)
        count = 0
        for prev in list(S):
            for pr in bools:
                for ia in bools:
                    for cn in bools:
                        for am in bools:
                            for tr in bools:
                                for fr in bools:
                                    d = derive_workspace_decision(_obs(
                                        previous_state=str(prev), process_running=pr, ipc_available=ia,
                                        connected=cn, account_match=am, trade_allowed=tr, fresh=fr))
                                    count += 1
                                    if d.transition_required:
                                        ok, _ = evaluate_workspace_transition(str(prev), d.next_state)
                                        self.assertTrue(ok, f"{prev}->{d.next_state} must be legal")
                                    # EXECUTION_READY implies ALL conditions (the safety invariant).
                                    if d.execution_ready:
                                        self.assertTrue(pr and ia and cn and am and tr and fr)
        self.assertEqual(count, 9 * 64)


# --- mutation adequacy -----------------------------------------------------------------------------------
_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
         ast.In: ast.NotIn, ast.NotIn: ast.In, ast.And: ast.Or, ast.Or: ast.And}
_CMP = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)


class _Mutant(ast.NodeTransformer):
    def __init__(self, target):
        self.i = -1
        self.target = target

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


def _count_ops(src):
    c = _Mutant(-1)
    c.visit(ast.parse(textwrap.dedent(src)))
    return c.i + 1


class ExecutionGateMutationTests(SimpleTestCase):
    """The EXECUTION_READY conjunction must be unbreakable."""

    CASES = [dict()] + [{f: False} for f in
                        ("process_running", "ipc_available", "connected", "account_match", "fresh",
                         "trade_allowed")]

    def _run(self, fn):
        return [fn(_obs(**kw)) for kw in self.CASES]

    def setUp(self):
        self.src = textwrap.dedent(inspect.getsource(_all_execution_conditions))
        self.total = _count_ops(self.src)
        self.baseline = self._run(_all_execution_conditions)

    def test_baseline_true_only_when_all(self):
        self.assertEqual(self.baseline, [True] + [False] * 6)

    def test_every_mutant_killed(self):
        survivors = []
        for t in range(self.total):
            tree = ast.parse(self.src)
            _Mutant(t).visit(tree)
            ast.fix_missing_locations(tree)
            ns = {"__builtins__": __builtins__}
            exec(compile(tree, "<m>", "exec"), ns)
            if self._run(ns["_all_execution_conditions"]) == self.baseline:
                survivors.append(t)
        self.assertEqual(survivors, [], f"unkilled mutants: {survivors}")


# Comprehensive derivation cases used as the derive-engine mutation oracle.
_DERIVE_CASES = [
    _obs(previous_state=str(S.CONNECTED)),                                   # -> EXECUTION_READY
    _obs(previous_state=str(S.EXECUTION_READY)),                            # stay ready (no transition)
    _obs(previous_state=str(S.EXECUTION_READY), account_match=False),       # -> SUSPENDED
    _obs(previous_state=str(S.CONNECTED), connected=False),                 # -> DISCONNECTED (recovery)
    _obs(previous_state=str(S.CONNECTED), fresh=False),                     # stay CONNECTED (stale)
    _obs(previous_state=str(S.PROVISIONING), process_running=False, connected=False),  # -> WAITING
    _obs(previous_state=str(S.DISCONNECTED), process_running=False),        # -> RECOVERING
    _obs(previous_state=str(S.WAITING_FOR_LOGIN)),                          # illegal->fail closed
    _obs(previous_state="BOGUS_STATE"),                                     # unknown->fail closed
]


def _decide(fn, obs):
    d = fn(obs)
    return (d.next_state, d.reason, d.transition_required, d.telemetry_event, d.execution_ready,
            d.recovery_required)


class DeriveEngineMutationTests(SimpleTestCase):
    def setUp(self):
        self.src = textwrap.dedent(inspect.getsource(derive_workspace_decision))
        self.total = _count_ops(self.src)
        self.baseline = [_decide(derive_workspace_decision, o) for o in _DERIVE_CASES]

    def test_has_operators(self):
        self.assertGreaterEqual(self.total, 4)

    def test_every_mutant_killed(self):
        # Compile each mutant with the real module globals so it can call the helpers it depends on.
        import hosted_workspace.manager as mod
        survivors = []
        for t in range(self.total):
            tree = ast.parse(self.src)
            _Mutant(t).visit(tree)
            ast.fix_missing_locations(tree)
            ns = dict(mod.__dict__)
            exec(compile(tree, "<m>", "exec"), ns)
            fn = ns["derive_workspace_decision"]
            try:
                result = [_decide(fn, o) for o in _DERIVE_CASES]
            except Exception:
                result = "RAISED"
            if result == self.baseline:
                survivors.append(t)
        self.assertEqual(survivors, [], f"unkilled mutants: {survivors}")
