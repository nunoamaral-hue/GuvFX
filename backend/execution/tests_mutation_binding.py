"""Phase 2 (Control 7) — mutation-adequacy test for the bridge's safety-critical binding decision.

The exact-binding gate (`evaluate_binding` in scripts/mt5_signal_bridge.py) is the single most
safety-critical pure decision in the execution plane: it is the broker-truth check that refuses to
trade against the wrong account or classification. This test performs REAL mutation testing on it,
scoped and CI-fast:

  * it parses the live source of ``evaluate_binding`` and auto-generates EVERY single comparison-operator
    (==/!=/is/is-not/<.../in...) and boolean-operator (and/or) mutant;
  * it runs a comprehensive truth-table oracle (the real function itself as the reference) against each
    mutant;
  * it asserts EVERY mutant is KILLED (differs from the real function on >=1 input) — i.e. the test
    oracle is strong enough to detect any single-operator defect in the decision. A surviving mutant is
    an equivalent mutant (would need documenting) or a real oracle gap.

Scope note: a full mutmut/cosmic-ray sweep across the whole 1900-line single-file bridge is deferred to
Phase 13 release-readiness tooling; this test proves adequacy where it matters most, in CI, today.
"""
import ast
import copy
import inspect
import textwrap

from django.test import SimpleTestCase

from execution.tests_bridge_symbols import _load_bridge

_CMP_SWAP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt,
    ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
_BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}


def _count_sites(tree):
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            n += sum(1 for op in node.ops if type(op) in _CMP_SWAP)
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
            n += 1
    return n


def _make_mutant(tree, target):
    """Deep-copy ``tree`` and mutate the ``target``-th mutable operator (deterministic walk order)."""
    t = copy.deepcopy(tree)
    idx = 0
    for node in ast.walk(t):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in _CMP_SWAP:
                    if idx == target:
                        node.ops[i] = _CMP_SWAP[type(op)]()
                        return t
                    idx += 1
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
            if idx == target:
                node.op = _BOOL_SWAP[type(node.op)]()
                return t
            idx += 1
    return None


def _compile_func(tree, name):
    ns = {}
    exec(compile(ast.fix_missing_locations(tree), "<mutant>", "exec"), ns)  # noqa: S102 - controlled AST
    return ns[name]


# --- comprehensive truth-table oracle inputs (acc, term, expected) — exercise every branch + boundary
_TERM_OK = {"connected": True, "trade_allowed": True}
_DEMO = {"login": 62133489, "server": "PepperstoneUK-Demo", "trade_mode": 0}
_REAL = {"login": 900001, "server": "Live", "trade_mode": 2}
_CONTEST = {"login": 700001, "server": "Contest", "trade_mode": 1}


def _exp(is_demo=True, allow_live=False, login=None, server=None):
    return {"is_demo": is_demo, "allow_live": allow_live, "expected_login": login, "expected_server": server}


ORACLE = [
    (_DEMO, None, _exp()),                                             # term None
    (_DEMO, {"connected": False, "trade_allowed": True}, _exp()),      # not connected
    (_DEMO, {"connected": True, "trade_allowed": False}, _exp()),      # trade not allowed
    (None, _TERM_OK, _exp()),                                          # acc None (term ok)
    ({"login": 1, "server": "x", "trade_mode": None}, _TERM_OK, _exp()),   # trade_mode None
    (_DEMO, _TERM_OK, _exp(is_demo=True)),                             # demo + demo job -> ok
    (_DEMO, _TERM_OK, _exp(is_demo=False)),                            # demo + non-demo job -> ok
    (_REAL, _TERM_OK, _exp(is_demo=True)),                             # real + demo job -> mismatch
    (_REAL, _TERM_OK, _exp(is_demo=False, allow_live=False)),          # real, no live auth -> deny
    (_REAL, _TERM_OK, _exp(is_demo=False, allow_live=True)),           # real + live auth -> ok
    (_CONTEST, _TERM_OK, _exp(is_demo=True)),                          # contest + demo job -> mismatch
    (_CONTEST, _TERM_OK, _exp(is_demo=False, allow_live=False)),       # contest, no live auth -> deny
    (_DEMO, _TERM_OK, _exp(login="99999999")),                        # login pin mismatch
    (_DEMO, _TERM_OK, _exp(login="62133489")),                        # login pin match
    (_DEMO, _TERM_OK, _exp(server="Other")),                          # server pin mismatch
    (_DEMO, _TERM_OK, _exp(server="PepperstoneUK-Demo")),            # server pin match
    (_DEMO, _TERM_OK, _exp(login="62133489", server="PepperstoneUK-Demo")),  # both pins match
]


class BindingMutationAdequacyTests(SimpleTestCase):
    def test_all_operator_mutants_are_killed(self):
        bridge = _load_bridge()
        real = bridge.evaluate_binding
        tree = ast.parse(textwrap.dedent(inspect.getsource(real)))
        n = _count_sites(tree)
        self.assertGreater(n, 5, "expected several mutable operators in evaluate_binding")

        reference = [real(a, t, e) for (a, t, e) in ORACLE]
        survivors = []
        for i in range(n):
            mutant = _compile_func(_make_mutant(tree, i), "evaluate_binding")
            killed = False
            for (a, t, e), ref in zip(ORACLE, reference):
                try:
                    if mutant(a, t, e) != ref:
                        killed = True
                        break
                except Exception:
                    killed = True  # a mutant that crashes on a valid input is also detected
                    break
            if not killed:
                survivors.append(i)
        self.assertEqual(
            survivors, [],
            f"{len(survivors)}/{n} operator mutants survived (equivalent mutants or an oracle gap): {survivors}")

    def test_harness_detects_survivors_with_a_weak_oracle(self):
        # RULE 11 positive control: prove the measurement CAN produce a non-empty result. A deliberately
        # weak oracle (one happy-path input) must let some mutants survive — otherwise "zero survivors"
        # above would be a vacuous pass (a broken harness that always kills, or finds no sites).
        bridge = _load_bridge()
        real = bridge.evaluate_binding
        tree = ast.parse(textwrap.dedent(inspect.getsource(real)))
        n = _count_sites(tree)
        weak = [(_DEMO, _TERM_OK, _exp())]  # a single happy-path case cannot distinguish most mutants
        reference = [real(a, t, e) for (a, t, e) in weak]
        survivors = []
        for i in range(n):
            mutant = _compile_func(_make_mutant(tree, i), "evaluate_binding")
            killed = False
            for (a, t, e), ref in zip(weak, reference):
                try:
                    if mutant(a, t, e) != ref:
                        killed = True
                        break
                except Exception:
                    killed = True
                    break
            if not killed:
                survivors.append(i)
        self.assertGreater(len(survivors), 0, "a weak oracle must leave survivors — harness sanity")
