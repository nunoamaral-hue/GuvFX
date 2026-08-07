"""Tests for hosted_workspace.matching — the pure, fail-closed active-account-match decision.

Two layers, mirroring backend/execution/tests_bridge_binding.py + tests_mutation_binding.py:

1. An explicit ORACLE truth table covering the ok case + every distinct deny reason and boundary.
2. An AST operator-mutation adequacy proof: every comparison / boolean / ``not`` operator in
   ``evaluate_active_account_match`` is flipped, and each mutant must be KILLED by the oracle. Plus a
   RULE-11 positive control proving the oracle is not vacuous (it catches a known-wrong reference).

Pure (SimpleTestCase, no DB, no Django models).
"""
from __future__ import annotations

import ast
import copy
import inspect
import textwrap

from django.test import SimpleTestCase

from hosted_workspace import matching as M
from hosted_workspace.matching import (
    ExpectedAccount,
    MatchDecision,
    WorkspaceObservation,
    evaluate_active_account_match,
    normalize_observation,
)


def _obs(**kw) -> WorkspaceObservation:
    base = dict(process_running=True, ipc_available=True, connected=True, trade_allowed=True,
                login="500123", server="Broker-Demo", trade_mode=0)
    base.update(kw)
    return WorkspaceObservation(**base)


def _exp(**kw) -> ExpectedAccount:
    base = dict(login="500123", server="Broker-Demo", is_demo=True, allow_live=False)
    base.update(kw)
    return ExpectedAccount(**base)


# (label, observation, expected, want_ok, want_reason). This is the oracle for BOTH the direct
# truth-table test and the mutation-adequacy proof.
CASES = [
    ("ok_demo", _obs(), _exp(), True, "ok"),
    ("ok_live_authorised", _obs(trade_mode=2), _exp(is_demo=False, allow_live=True), True, "ok"),
    ("not_running", _obs(process_running=False), _exp(), False, "workspace_not_running"),
    ("no_ipc", _obs(ipc_available=False), _exp(), False, "workspace_ipc_unavailable"),
    ("not_connected_false", _obs(connected=False), _exp(), False, "terminal_not_connected"),
    ("not_connected_none", _obs(connected=None), _exp(), False, "terminal_not_connected"),
    ("no_trade_false", _obs(trade_allowed=False), _exp(), False, "trade_not_allowed"),
    ("no_trade_none", _obs(trade_allowed=None), _exp(), False, "trade_not_allowed"),
    ("login_unavail", _obs(login=None), _exp(), False, "active_login_unavailable"),
    ("server_unavail", _obs(server=None), _exp(), False, "active_server_unavailable"),
    ("trade_mode_unavail", _obs(trade_mode=None), _exp(), False, "trade_mode_unavailable"),
    ("expected_login_unset", _obs(), _exp(login=""), False, "expected_login_unconfigured"),
    ("expected_login_none", _obs(), _exp(login=None), False, "expected_login_unconfigured"),
    ("expected_server_unset", _obs(), _exp(server=""), False, "expected_server_unconfigured"),
    ("login_mismatch", _obs(login="999999"), _exp(), False, "active_account_login_mismatch"),
    ("server_mismatch", _obs(server="Other-Server"), _exp(), False, "active_account_server_mismatch"),
    ("classification_mismatch", _obs(trade_mode=2), _exp(is_demo=True), False, "classification_mismatch"),
    ("live_not_authorised", _obs(trade_mode=2), _exp(is_demo=False, allow_live=False),
     False, "live_execution_not_authorised"),
]


class OracleTruthTableTests(SimpleTestCase):
    def test_every_case(self):
        for label, obs, exp, want_ok, want_reason in CASES:
            with self.subTest(label):
                d = evaluate_active_account_match(obs, exp)
                self.assertEqual(d.ok, want_ok, f"{label}: ok")
                self.assertEqual(d.reason, want_reason, f"{label}: reason")

    def test_exactly_two_permitted_paths_all_else_deny(self):
        # Behavioural (calls the function): exactly two oracle inputs are permitted, both with reason
        # 'ok'; every other input denies (fail-closed).
        decisions = [evaluate_active_account_match(obs, exp) for _, obs, exp, _, _ in CASES]
        oks = [d for d in decisions if d.ok]
        self.assertEqual(len(oks), 2, "exactly two oracle inputs should be permitted")
        self.assertTrue(all(d.reason == "ok" for d in oks))
        self.assertTrue(all(not d.ok for d in decisions if d.reason != "ok"))


class NormalizeObservationTests(SimpleTestCase):
    def test_none_and_garbage_are_failclosed(self):
        for bad in (None, "not-a-dict", 42, []):
            o = normalize_observation(bad)
            self.assertFalse(o.process_running)
            self.assertIsNone(o.connected)
            d = evaluate_active_account_match(o, _exp())
            self.assertFalse(d.ok)

    def test_maps_scalars_and_coerces_login_to_str(self):
        o = normalize_observation({
            "process_running": True, "ipc_available": True, "connected": True,
            "trade_allowed": True, "login": 500123, "server": "Broker-Demo", "trade_mode": 0,
            "observed_at": "2026-08-07T00:00:00Z",
        })
        self.assertEqual(o.login, "500123")  # coerced int -> str
        self.assertIs(o.connected, True)
        self.assertTrue(evaluate_active_account_match(o, _exp()).ok)

    def test_non_bool_connected_is_none_not_truthy(self):
        # A truthy non-bool (e.g. 1) must NOT be read as connected=True (fail closed).
        o = normalize_observation({"process_running": True, "ipc_available": True,
                                   "connected": 1, "trade_allowed": 1, "login": "500123",
                                   "server": "Broker-Demo", "trade_mode": 0})
        self.assertIsNone(o.connected)
        self.assertFalse(evaluate_active_account_match(o, _exp()).ok)


# ── AST operator-mutation adequacy (mirrors execution/tests_mutation_binding.py) ──

_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
         ast.And: ast.Or, ast.Or: ast.And}


class _Mutant(ast.NodeTransformer):
    """Post-order visitor that mutates exactly the ``target``-th mutable operator (or counts them all
    when target is -1)."""

    def __init__(self, target: int):
        self.i = -1
        self.target = target

    def _hit(self) -> bool:
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
                return node.operand  # drop the ``not``
        return node


def _compile_mutant(tree) -> object:
    ns = {
        "WorkspaceObservation": WorkspaceObservation,
        "ExpectedAccount": ExpectedAccount,
        "MatchDecision": MatchDecision,
        "TRADE_MODE_DEMO": M.TRADE_MODE_DEMO,
        "__builtins__": __builtins__,
    }
    exec(compile(tree, "<mutant>", "exec"), ns)
    return ns["evaluate_active_account_match"]


def _results(fn):
    return [(fn(obs, exp).ok, fn(obs, exp).reason) for _, obs, exp, _, _ in CASES]


class MutationAdequacyTests(SimpleTestCase):
    def setUp(self):
        src = textwrap.dedent(inspect.getsource(evaluate_active_account_match))
        self.tree = ast.parse(src)
        counter = _Mutant(-1)
        counter.visit(copy.deepcopy(self.tree))
        self.total = counter.i + 1
        self.baseline = _results(evaluate_active_account_match)

    def test_has_several_mutable_operators(self):
        # Positive control: the parser actually found operators to mutate (guards a vacuous pass).
        self.assertGreaterEqual(self.total, 10, "expected many mutable operators")

    def test_every_mutant_is_killed(self):
        survivors = []
        for t in range(self.total):
            tree_t = copy.deepcopy(self.tree)
            _Mutant(t).visit(tree_t)
            ast.fix_missing_locations(tree_t)
            fn = _compile_mutant(tree_t)
            if _results(fn) == self.baseline:
                survivors.append(t)
        self.assertEqual(survivors, [], f"unkilled mutant operator indices: {survivors}")

    def test_oracle_is_not_vacuous(self):
        # RULE-11 positive control: a known-WRONG reference (always-ok) must differ from the real
        # decision on at least one oracle case — proving the oracle can actually fail.
        def _always_ok(obs, exp):
            return MatchDecision(True, "ok")
        self.assertNotEqual(_results(_always_ok), self.baseline)
