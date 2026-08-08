"""ADR-0034 / M2b — Workspace telemetry taxonomy.

Proves the ``workspace.*`` event family (ADR-0034 §6): taxonomy completeness + meta validity + the pure
event-builder (correct category/severity/state, fail-closed on unknown) + the secret-free ``_redact``
(AST mutation adequacy — every mutant killed, since secret-free telemetry is a security property).
"""
import ast
import copy
import inspect
import textwrap

from django.test import SimpleTestCase

from operational_events.models import OperationalEvent as OE

from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason
from hosted_workspace.telemetry import (
    EVENT_META,
    WorkspaceEvent,
    _SECRET_KEYS,
    _redact,
    build_workspace_event,
)

# ADR-0034 §6 minimum event family (18).
_EXPECTED_EVENTS = {
    "CREATED", "STARTED", "WAITING_FOR_LOGIN", "CONNECTED", "DISCONNECTED", "ATTACH_SUCCEEDED",
    "ATTACH_FAILED", "ACCOUNT_CHANGED", "EXECUTION_READY", "EXECUTION_PAUSED", "EXECUTION_STARTED",
    "EXECUTION_FINISHED", "RECOVERING", "RECOVERED", "REMOTEAPP_CONNECTED", "REMOTEAPP_DISCONNECTED",
    "CRASHED", "RESTARTED",
}


class TaxonomyTests(SimpleTestCase):
    def test_all_adr_events_present(self):
        self.assertEqual({e.name for e in WorkspaceEvent}, _EXPECTED_EVENTS)

    def test_values_are_namespaced(self):
        for e in WorkspaceEvent:
            self.assertTrue(str(e).startswith("workspace."), e)

    def test_every_event_has_valid_meta(self):
        categories = set(OE.Category.values)
        severities = set(OE.Severity.values)
        states = set(S)
        self.assertEqual(set(EVENT_META.keys()), set(WorkspaceEvent))  # complete
        for event, (cat, sev, canonical) in EVENT_META.items():
            self.assertIn(str(cat), categories, event)
            self.assertIn(str(sev), severities, event)
            self.assertTrue(canonical is None or canonical in states, event)


class BuildEventTests(SimpleTestCase):
    def test_known_event_output(self):
        out = build_workspace_event(WorkspaceEvent.CONNECTED, "ws-1", correlation_id="c1")
        self.assertEqual(out["event_type"], "workspace.connected")
        self.assertEqual(out["category"], str(OE.Category.CONNECTIVITY))
        self.assertEqual(out["severity"], str(OE.Severity.INFO))
        self.assertEqual(out["status"], str(S.CONNECTED))
        self.assertFalse(out["customer_visible"])
        self.assertEqual(out["detail"]["workspace_uuid"], "ws-1")
        self.assertEqual(out["detail"]["canonical_state"], str(S.CONNECTED))
        self.assertEqual(out["detail"]["correlation_id"], "c1")

    def test_reason_and_edge_event(self):
        out = build_workspace_event(WorkspaceEvent.EXECUTION_PAUSED, "ws-1",
                                    reason=WorkspaceReason.ACCOUNT_MISMATCH)
        self.assertEqual(out["reason_code"], str(WorkspaceReason.ACCOUNT_MISMATCH))
        self.assertEqual(out["status"], str(S.SUSPENDED))

    def test_unknown_event_fails_closed(self):
        out = build_workspace_event("not-an-event", "ws-1")
        self.assertEqual(out["event_type"], "workspace.unknown_event")
        self.assertEqual(out["category"], str(OE.Category.SYSTEM))
        self.assertEqual(out["severity"], str(OE.Severity.ERROR))

    def test_detail_is_secret_free(self):
        out = build_workspace_event(
            WorkspaceEvent.ATTACH_SUCCEEDED, "ws-1",
            detail={"password": "hunter2", "token": "abc", "login": 987654, "server": "Demo", "note": "ok"})
        d = out["detail"]
        self.assertNotIn("password", d)
        self.assertNotIn("token", d)
        self.assertEqual(d["login"], "****7654")
        self.assertEqual(d["note"], "ok")
        # No raw secret value survives anywhere in the payload.
        self.assertNotIn("hunter2", str(out))
        self.assertNotIn("987654", str(out))


# --- _redact mutation adequacy (secret-free telemetry is a security property) ----------------------------
# (input_detail, expected_output)
REDACT_CASES = [
    (None, {}),
    ("not-a-dict", {}),
    ({"password": "p"}, {}),
    ({"token": "t"}, {}),
    ({"login": 123456}, {"login": "****3456"}),
    ({"login": None}, {"login": None}),
    ({"benign": "ok"}, {"benign": "ok"}),
    ({"login": 12, "password": "p", "keep": 1}, {"login": "****12", "keep": 1}),
]

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
    ns = {"_SECRET_KEYS": _SECRET_KEYS, "__builtins__": __builtins__}
    exec(compile(tree, "<mutant>", "exec"), ns)
    return ns["_redact"]


def _results(fn):
    out = []
    for detail, _ in REDACT_CASES:
        try:
            out.append(fn(detail))
        except Exception as exc:
            out.append(("RAISED", type(exc).__name__))
    return out


class RedactTests(SimpleTestCase):
    def test_cases(self):
        for detail, expected in REDACT_CASES:
            self.assertEqual(_redact(detail), expected, detail)

    def setUp(self):
        self.tree = ast.parse(textwrap.dedent(inspect.getsource(_redact)))
        c = _Mutant(-1)
        c.visit(copy.deepcopy(self.tree))
        self.total = c.i + 1
        self.baseline = _results(_redact)

    def test_has_operators(self):
        self.assertGreaterEqual(self.total, 4)

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
        self.assertNotEqual([{} for _ in REDACT_CASES], self.baseline)
