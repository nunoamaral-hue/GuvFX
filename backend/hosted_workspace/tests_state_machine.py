"""ADR-0034 / M2a — canonical Workspace state machine.

Proves ``evaluate_workspace_transition`` (pure, fail-closed) against the ADR-0034 §3 graph via an oracle
truth-table + AST operator-mutation adequacy (every mutant killed) + a non-vacuous-oracle control; the
``to_canonical`` legacy→canonical mapping for completeness + fail-closed default; and graph fidelity to §3.
"""
import ast
import copy
import inspect
import textwrap

from django.test import SimpleTestCase

from hosted_workspace.models import WorkspaceState
from hosted_workspace.state_machine import (
    WORKSPACE_TRANSITIONS,
    WorkspaceLifecycleState as S,
    WorkspaceReason,
    evaluate_workspace_transition,
    to_canonical,
)

# (from, to, want_allowed, want_reason)
CASES = [
    (S.PROVISIONING, S.WAITING_FOR_LOGIN, True, "ok"),
    (S.PROVISIONING, S.CONNECTED, False, "illegal_transition"),
    (S.CONNECTED, S.CONNECTED, True, "idempotent"),
    ("BOGUS", S.CONNECTED, False, "unknown_state"),
    (S.CONNECTED, "BOGUS", False, "unknown_state"),
    (S.RETIRED, S.CONNECTED, False, "illegal_transition"),
    (S.EXECUTION_READY, S.EXECUTING, True, "ok"),
    (S.EXECUTING, S.CONNECTED, False, "illegal_transition"),
]


class EvaluateTransitionTests(SimpleTestCase):
    def test_every_case(self):
        for frm, to, want_ok, want_reason in CASES:
            with self.subTest(f"{frm}->{to}"):
                ok, reason = evaluate_workspace_transition(frm, to)
                self.assertEqual(ok, want_ok)
                self.assertEqual(reason, want_reason)

    def test_string_values_accepted(self):
        # Callers may pass the raw string value (TextChoices members are str subclasses).
        self.assertEqual(evaluate_workspace_transition("PROVISIONING", "WAITING_FOR_LOGIN"), (True, "ok"))


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


def _compile_mutant(tree):
    ns = {"WORKSPACE_TRANSITIONS": WORKSPACE_TRANSITIONS}
    ns["__builtins__"] = __builtins__
    exec(compile(tree, "<mutant>", "exec"), ns)
    return ns["evaluate_workspace_transition"]


def _results(fn):
    out = []
    for frm, to, _, _ in CASES:
        try:
            out.append(fn(frm, to))
        except Exception as exc:  # a mutant that crashes on an oracle case is DISTINGUISHED (killed)
            out.append(("RAISED", type(exc).__name__))
    return out


class MutationAdequacyTests(SimpleTestCase):
    def setUp(self):
        self.tree = ast.parse(textwrap.dedent(inspect.getsource(evaluate_workspace_transition)))
        c = _Mutant(-1)
        c.visit(copy.deepcopy(self.tree))
        self.total = c.i + 1
        self.baseline = _results(evaluate_workspace_transition)

    def test_has_operators(self):
        self.assertGreaterEqual(self.total, 5)

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


class GraphFidelityTests(SimpleTestCase):
    """WORKSPACE_TRANSITIONS must match the ADR-0034 §3 diagram exactly."""

    EXPECTED = {
        S.PROVISIONING: {S.WAITING_FOR_LOGIN, S.RETIRED},
        S.WAITING_FOR_LOGIN: {S.CONNECTED},
        S.CONNECTED: {S.EXECUTION_READY, S.DISCONNECTED, S.SUSPENDED},
        S.EXECUTION_READY: {S.EXECUTING, S.CONNECTED, S.DISCONNECTED, S.SUSPENDED},
        S.EXECUTING: {S.EXECUTION_READY},
        S.DISCONNECTED: {S.RECOVERING, S.RETIRED},
        S.RECOVERING: {S.CONNECTED},
        S.SUSPENDED: {S.CONNECTED, S.RETIRED},
        S.RETIRED: set(),
    }

    def test_graph_matches_adr(self):
        self.assertEqual(WORKSPACE_TRANSITIONS, self.EXPECTED)

    def test_all_nine_states_present(self):
        self.assertEqual(set(WORKSPACE_TRANSITIONS.keys()), set(S))
        self.assertEqual(len(list(S)), 9)

    def test_retired_is_terminal(self):
        self.assertEqual(WORKSPACE_TRANSITIONS[S.RETIRED], set())

    def test_no_transition_targets_unknown_state(self):
        for targets in WORKSPACE_TRANSITIONS.values():
            for t in targets:
                self.assertIn(t, set(S))


class ToCanonicalTests(SimpleTestCase):
    def test_every_legacy_state_maps_to_a_canonical_state(self):
        for legacy in WorkspaceState.values:
            state, reason = to_canonical(legacy)
            self.assertIn(state, set(S), legacy)
            self.assertIn(reason, set(WorkspaceReason), legacy)

    def test_legacy_never_maps_to_an_execution_state(self):
        # No legacy state may silently become execution-authorised under the canonical model.
        for legacy in WorkspaceState.values:
            state, _ = to_canonical(legacy)
            self.assertNotIn(state, {S.EXECUTION_READY, S.EXECUTING}, legacy)

    def test_unknown_legacy_fails_closed_to_suspended_error(self):
        self.assertEqual(to_canonical("SOMETHING_NEW"), (S.SUSPENDED, WorkspaceReason.ERROR))
        self.assertEqual(to_canonical(None), (S.SUSPENDED, WorkspaceReason.ERROR))

    def test_representative_mappings(self):
        self.assertEqual(to_canonical("AWAITING_USER_LOGIN"), (S.WAITING_FOR_LOGIN, WorkspaceReason.NONE))
        self.assertEqual(to_canonical("ACTIVE_ACCOUNT_MISMATCH"),
                         (S.SUSPENDED, WorkspaceReason.ACCOUNT_MISMATCH))
        self.assertEqual(to_canonical("DEGRADED"), (S.RECOVERING, WorkspaceReason.DEGRADED))
