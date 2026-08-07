"""ADR-0033 Increment 3 — IDENTITY gate for account-mutating operations (CLOSE / MODIFY).

Proves the new bridge functions:
- ``evaluate_mutation_identity`` (pure): connected + account present + login/server match (when the pin is
  set); deliberately NOT trade_allowed / classification. Oracle truth-table + AST operator-mutation
  adequacy (every mutant killed) + a non-vacuous-oracle control.
- ``verify_mutation_identity`` (live wrapper): pin mandatory on the workspace path (payload
  require_identity_pin OR terminal MT5_REQUIRE_IDENTITY_PIN), env-optional for legacy; fail-closed.
- ``close_position`` / ``modify_position`` re-verify identity IMMEDIATELY before order_send and ENFORCE
  the result (identity_rejected), with no account-changing MT5 call in between (asserted structurally).
"""
import ast
import copy
import inspect
import os
import re
import textwrap
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_binding import (
    _DEMO,
    _FakeInfo,
    _FakeMt5,
    _REAL,
    _TERM_OK,
    _load_bridge,
)

_MOD = _load_bridge()
_eval = _MOD.evaluate_mutation_identity


def _acc(**kw):
    base = {"login": _DEMO["login"], "server": _DEMO["server"], "trade_mode": 0}
    base.update(kw)
    return base


def _term(connected=True, trade_allowed=True):
    return {"connected": connected, "trade_allowed": trade_allowed}


# (label, acc, term, expected, want_ok, want_reason)
CASES = [
    ("ok_no_pins", _acc(), _term(), {}, True, "ok"),
    ("ok_matching_pins", _acc(), _term(),
     {"expected_login": str(_DEMO["login"]), "expected_server": _DEMO["server"]}, True, "ok"),
    ("ok_even_if_trade_not_allowed", _acc(), _term(trade_allowed=False), {}, True, "ok"),
    ("term_none", _acc(), None, {}, False, "terminal_info_unavailable"),
    ("not_connected", _acc(), _term(connected=False), {}, False, "terminal_not_connected"),
    ("acc_none", None, _term(), {}, False, "account_info_unavailable"),
    ("real_account_at_send", _acc(trade_mode=2), _term(), {}, False, "account_not_demo"),
    ("login_mismatch", _acc(), _term(), {"expected_login": "999", "expected_server": _DEMO["server"]},
     False, "account_login_mismatch"),
    ("server_mismatch", _acc(), _term(),
     {"expected_login": str(_DEMO["login"]), "expected_server": "Other"}, False, "broker_server_mismatch"),
]


class EvaluateMutationIdentityTests(SimpleTestCase):
    def test_every_case(self):
        for label, acc, term, exp, want_ok, want_reason in CASES:
            with self.subTest(label):
                ok, reason = _eval(acc, term, exp)
                self.assertEqual(ok, want_ok, label)
                self.assertEqual(reason, want_reason, label)

    def test_trade_allowed_is_not_required(self):
        # Explicit: a risk-reducing close/modify is NOT blocked by a trading halt (packet E2).
        ok, _ = _eval(_acc(), _term(trade_allowed=False), {})
        self.assertTrue(ok)


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
    return ns["evaluate_mutation_identity"]


def _results(fn):
    out = []
    for _, a, t, e, _, _ in CASES:
        try:
            out.append(fn(a, t, e))
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


class VerifyMutationIdentityTests(SimpleTestCase):
    def _mt5(self, acc, term):
        return _FakeMt5(acc=_FakeInfo(**acc), term=_FakeInfo(**term))

    def _verify(self, acc, identity, env=None):
        env = env or {"MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": "", "MT5_REQUIRE_IDENTITY_PIN": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            return _MOD.verify_mutation_identity(self._mt5(acc, _TERM_OK), identity)

    def test_legacy_no_pin_ok(self):
        ok, reason, _ = self._verify(_acc(), None)
        self.assertTrue(ok, reason)

    def test_workspace_missing_pin_fails_closed(self):
        ok, reason, _ = self._verify(_acc(), {"require_identity_pin": True})
        self.assertFalse(ok)
        self.assertEqual(reason, "identity_pin_required")

    def test_workspace_matching_pin_ok_and_redacted(self):
        ok, reason, details = self._verify(
            _acc(), {"require_identity_pin": True, "expected_login": str(_DEMO["login"]),
                     "expected_server": _DEMO["server"]})
        self.assertTrue(ok, reason)
        self.assertNotIn(str(_DEMO["login"]), str(details))  # login redacted

    def test_workspace_wrong_login_denies(self):
        ok, reason, _ = self._verify(
            _acc(), {"require_identity_pin": True, "expected_login": "999",
                     "expected_server": _DEMO["server"]})
        self.assertEqual(reason, "account_login_mismatch")

    def test_terminal_level_require_pin(self):
        # MT5_REQUIRE_IDENTITY_PIN makes the pin mandatory even without a payload flag.
        ok, reason, _ = self._verify(_acc(), None, env={"MT5_REQUIRE_IDENTITY_PIN": "1"})
        self.assertFalse(ok)
        self.assertEqual(reason, "identity_pin_required")

    def test_read_error_fails_closed(self):
        ok, reason, _ = _MOD.verify_mutation_identity(
            _FakeMt5(acc=_FakeInfo(**_acc()), term=_FakeInfo(**_TERM_OK), acc_raises=True), None)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("mutation_identity_error"))


class MutationEnforcementSourceTests(SimpleTestCase):
    """close_position / modify_position must re-verify identity immediately before order_send and enforce
    the result (identity_rejected), with no account-changing MT5 call in between — asserted structurally
    so the guard cannot be silently removed."""

    def setUp(self):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self.src = open(os.path.join(repo, "scripts", "mt5_signal_bridge.py"), encoding="utf-8").read()

    def _assert_guarded(self, func_name):
        m = re.search(rf"\ndef {func_name}\(", self.src)
        self.assertIsNotNone(m, f"{func_name} not found")
        rest = self.src[m.end():]
        nxt = re.search(r"\ndef ", rest)
        body = rest[: nxt.start()] if nxt else rest
        send = body.find("mt5.order_send(request)")
        self.assertGreater(send, -1, f"{func_name}: no order_send")
        pre = body.rfind("verify_mutation_identity(", 0, send)
        self.assertGreater(pre, -1, f"{func_name}: no verify_mutation_identity before order_send")
        between = body[pre:send]
        self.assertIn("identity_rejected", between, f"{func_name}: identity result not enforced")
        # Guard POLARITY: rejection must be gated on `not _idok` — an inversion (`if _idok:`) would
        # otherwise pass a structural "call exists" check while executing the mutation on a mismatch.
        self.assertIn("not _idok", between, f"{func_name}: rejection not guarded by `not _idok`")
        self.assertNotIn("mt5.login(", between)
        self.assertNotIn("mt5.initialize(", between)

    def test_close_position_guarded(self):
        self._assert_guarded("close_position")

    def test_modify_position_guarded(self):
        self._assert_guarded("modify_position")
