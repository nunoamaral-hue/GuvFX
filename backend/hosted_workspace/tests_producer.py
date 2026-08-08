"""ADR-0034 / M3b-1 — Workspace Observation Producer.

Oracle + AST mutation adequacy + account-match (positive/login-mismatch/server-mismatch/missing) + missing
process + failed attach + disconnected + trade-disabled + fresh/boundary/stale/future/malformed timestamps +
unknown raw values + secret-free proof + output-type proof + no-state-derivation proof + exception safety.
The producer is pure, so tests need no DB and no mocks.
"""
import ast
import copy
import inspect
import os
import textwrap

from django.test import SimpleTestCase

from hosted_workspace.manager import WorkspaceObservation
from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason
from hosted_workspace.producer import (
    RawWorkspaceSnapshot,
    _compute_freshness,
    _is_number,
    _is_true,
    build_workspace_observation,
)


def _snap(**kw):
    base = dict(
        workspace_id="ws-1", expected_login="12345", expected_server="Demo",
        target_pid=999, target_path="C:\\t\\terminal64.exe",
        process_running=True, attach_attempted=True, attach_succeeded=True, ipc_available=True,
        terminal_connected=True, trade_allowed=True,
        observed_login="12345", observed_server="Demo", observed_trade_mode=0,
        observed_at=100.0, freshness_limit_seconds=60.0)
    base.update(kw)
    return RawWorkspaceSnapshot(**base)


def _build(snap, now=120.0, previous_state=S.CONNECTED):
    return build_workspace_observation(snap, now=now, previous_state=str(previous_state))


class OutputTypeAndOracleTests(SimpleTestCase):
    def test_output_is_canonical_workspace_observation(self):  # deliverable R
        obs = _build(_snap())
        self.assertIsInstance(obs, WorkspaceObservation)

    def test_all_good_is_all_true(self):
        obs = _build(_snap())
        self.assertEqual(
            (obs.process_running, obs.ipc_available, obs.connected, obs.account_match, obs.trade_allowed,
             obs.fresh),
            (True, True, True, True, True, True))

    def test_previous_state_is_carried_not_derived(self):
        obs = _build(_snap(), previous_state=S.DISCONNECTED)
        self.assertEqual(obs.previous_state, str(S.DISCONNECTED))


class FailClosedFieldTests(SimpleTestCase):
    def test_missing_process(self):  # G
        self.assertFalse(_build(_snap(process_running=None)).process_running)

    def test_failed_attach_blocks_ipc(self):  # H
        self.assertFalse(_build(_snap(attach_succeeded=False)).ipc_available)
        self.assertFalse(_build(_snap(ipc_available=None)).ipc_available)

    def test_disconnected(self):  # I
        self.assertFalse(_build(_snap(terminal_connected=False)).connected)
        self.assertFalse(_build(_snap(terminal_connected=None)).connected)

    def test_trade_disabled(self):  # J
        self.assertFalse(_build(_snap(trade_allowed=False)).trade_allowed)

    def test_strict_truthiness_of_unknown_values(self):  # P
        for bad in (None, 1, "true", "yes", 0, ""):
            self.assertFalse(_build(_snap(process_running=bad)).process_running, bad)


class AccountMatchTests(SimpleTestCase):
    def test_positive(self):  # C+
        self.assertTrue(_build(_snap()).account_match)

    def test_login_mismatch(self):  # D
        self.assertFalse(_build(_snap(observed_login="99999")).account_match)

    def test_server_mismatch(self):  # E
        self.assertFalse(_build(_snap(observed_server="Other")).account_match)

    def test_missing_identity(self):  # F
        self.assertFalse(_build(_snap(observed_login=None)).account_match)
        self.assertFalse(_build(_snap(observed_server=None)).account_match)
        self.assertFalse(_build(_snap(expected_login=None)).account_match)
        self.assertFalse(_build(_snap(expected_server=None)).account_match)

    def test_unknown_trade_mode_fails_closed(self):  # demo/live confusion
        self.assertFalse(_build(_snap(observed_trade_mode=None)).account_match)

    def test_live_account_fails_closed_for_demo_workspace(self):
        self.assertFalse(_build(_snap(observed_trade_mode=2)).account_match)  # real -> not authorised

    def test_bool_trade_mode_fails_closed(self):  # adversarial MEDIUM regression (False == 0 was DEMO)
        self.assertFalse(_build(_snap(observed_trade_mode=False)).account_match)
        self.assertFalse(_build(_snap(observed_trade_mode=True)).account_match)

    def test_blank_or_whitespace_identity_fails_closed(self):  # adversarial MEDIUM regression
        for blank in ("", " ", "   ", "\t"):
            self.assertFalse(_build(_snap(observed_login=blank, expected_login=blank)).account_match, repr(blank))
            self.assertFalse(_build(_snap(observed_server=blank, expected_server=blank)).account_match, repr(blank))
            self.assertFalse(_build(_snap(expected_login=blank)).account_match, repr(blank))
            self.assertFalse(_build(_snap(observed_login=blank)).account_match, repr(blank))

    def test_whitespace_padding_normalised(self):
        # A padded-but-equal identity normalises to a match (defense: not a spurious mismatch either).
        self.assertTrue(_build(_snap(observed_login=" 12345 ", expected_login="12345")).account_match)


class FreshnessTests(SimpleTestCase):
    def test_fresh(self):  # K
        self.assertTrue(_build(_snap(observed_at=100.0, freshness_limit_seconds=60.0), now=120.0).fresh)

    def test_exact_boundary_is_fresh(self):  # L (age == limit)
        self.assertTrue(_build(_snap(observed_at=100.0, freshness_limit_seconds=60.0), now=160.0).fresh)

    def test_stale(self):  # M (age > limit)
        self.assertFalse(_build(_snap(observed_at=100.0, freshness_limit_seconds=60.0), now=161.0).fresh)

    def test_future_within_tolerance_ok(self):
        self.assertTrue(_build(_snap(observed_at=100.0, freshness_limit_seconds=60.0), now=97.0).fresh)

    def test_future_beyond_tolerance_fails(self):  # N
        self.assertFalse(_build(_snap(observed_at=100.0, freshness_limit_seconds=60.0), now=90.0).fresh)

    def test_malformed_timestamp(self):  # O
        self.assertFalse(_build(_snap(observed_at="not-a-number")).fresh)
        self.assertFalse(_build(_snap(observed_at=True)).fresh)  # bool is not a timestamp
        self.assertFalse(_build(_snap(observed_at=None)).fresh)

    def test_missing_or_bad_limit(self):
        self.assertFalse(_build(_snap(freshness_limit_seconds=None)).fresh)
        self.assertFalse(_build(_snap(freshness_limit_seconds=0)).fresh)
        self.assertFalse(_build(_snap(freshness_limit_seconds=-30)).fresh)

    def test_malformed_observed_at_not_emitted(self):
        self.assertIsNone(_build(_snap(observed_at="x")).observed_at)

    def test_nan_inf_timestamps_fail_closed(self):  # adversarial HIGH regression
        for bad in (float("nan"), float("inf"), float("-inf")):
            obs = _build(_snap(observed_at=bad, freshness_limit_seconds=60.0), now=120.0)
            self.assertFalse(obs.fresh, bad)
            self.assertIsNone(obs.observed_at, bad)  # non-finite is never emitted
        self.assertFalse(_build(_snap(observed_at=100.0, freshness_limit_seconds=float("nan")),
                                now=1e9).fresh)
        self.assertFalse(_build(_snap(observed_at=100.0, freshness_limit_seconds=float("inf")),
                                now=1e9).fresh)
        self.assertFalse(_build(_snap(observed_at=100.0), now=float("nan")).fresh)

    def test_non_finite_tolerance_does_not_disable_future_guard(self):  # defense-in-depth (config, not snapshot)
        # A non-finite clock_tolerance_seconds must default to zero tolerance (fail-closed), NOT disable the
        # future-observation guard: a future reading (now < observed_at) beyond zero tolerance stays stale.
        for bad_tol in (float("nan"), float("inf")):
            obs = build_workspace_observation(
                _snap(observed_at=100.0, freshness_limit_seconds=60.0), now=90.0,
                previous_state=str(S.CONNECTED), clock_tolerance_seconds=bad_tol)
            self.assertFalse(obs.fresh, bad_tol)


class IsTrueTests(SimpleTestCase):
    def test_strict(self):
        self.assertTrue(_is_true(True))
        for v in (False, None, 1, 0, "true", "", [], object()):
            self.assertFalse(_is_true(v), v)

    def test_is_number_rejects_non_finite_and_bool(self):
        for good in (0, 1, -3, 1.5, 1e9):
            self.assertTrue(_is_number(good), good)
        for bad in (float("nan"), float("inf"), float("-inf"), True, False, None, "5", "", []):
            self.assertFalse(_is_number(bad), bad)


class SecretFreeTests(SimpleTestCase):  # Q
    def test_output_has_no_identity_or_secret_fields(self):
        obs = _build(_snap())
        for forbidden in ("login", "server", "password", "password_enc", "token", "secret", "keyring",
                          "accounts_dat"):
            self.assertFalse(hasattr(obs, forbidden), forbidden)

    def test_observed_login_not_emitted_anywhere(self):
        obs = _build(_snap(observed_login="SUPERSECRET123", expected_login="SUPERSECRET123",
                           observed_server="SRVSECRET", expected_server="SRVSECRET"))
        self.assertNotIn("SUPERSECRET123", str(obs))
        self.assertNotIn("SRVSECRET", str(obs))


class NoStateDerivationTests(SimpleTestCase):  # S
    def test_producer_never_derives_lifecycle_state(self):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__)))
        src = open(os.path.join(repo, "producer.py"), encoding="utf-8").read()
        # The producer must not derive canonical state: no state machine, no manager derivation.
        self.assertNotIn("WorkspaceLifecycleState", src)
        self.assertNotIn("evaluate_workspace_transition", src)
        self.assertNotIn("derive_workspace_decision", src)


class ExceptionSafetyTests(SimpleTestCase):  # exception must never become a positive
    def test_none_snapshot_fails_closed(self):
        obs = build_workspace_observation(None, now=120.0, previous_state=str(S.CONNECTED))
        self.assertEqual(
            (obs.process_running, obs.ipc_available, obs.connected, obs.account_match, obs.trade_allowed,
             obs.fresh, obs.observed_at),
            (False, False, False, False, False, False, None))

    def test_garbage_snapshot_fails_closed(self):
        class _Bad:
            def __getattr__(self, k):
                raise RuntimeError("boom")
        obs = build_workspace_observation(_Bad(), now=120.0, previous_state=str(S.CONNECTED))
        self.assertFalse(obs.process_running or obs.ipc_available or obs.connected
                         or obs.account_match or obs.trade_allowed or obs.fresh)


# --- AST mutation adequacy on the freshness gate (safety-significant) -------------------------------------
_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
         ast.In: ast.NotIn, ast.NotIn: ast.In, ast.And: ast.Or, ast.Or: ast.And,
         ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt}
_CMP = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn, ast.Lt, ast.GtE, ast.Gt, ast.LtE)

# (observed_at, now, limit, tolerance) -> expected fresh
_FRESH_CASES = [
    (100.0, 120.0, 60.0, 5.0, True),    # fresh
    (100.0, 161.0, 60.0, 5.0, False),   # stale (age > limit)
    (100.0, 160.0, 60.0, 5.0, True),    # boundary (age == limit)
    (100.0, 97.0, 60.0, 5.0, True),     # future within tolerance
    (100.0, 90.0, 60.0, 5.0, False),    # future beyond tolerance
    (100.0, 120.0, 0.0, 5.0, False),    # non-positive limit
    (100.0, 120.0, -5.0, 5.0, False),   # negative limit
    (None, 120.0, 60.0, 5.0, False),    # missing observed_at
    (100.0, None, 60.0, 5.0, False),    # missing now
    ("x", 120.0, 60.0, 5.0, False),     # malformed observed_at
    (True, 120.0, 60.0, 5.0, False),    # bool observed_at
    (float("nan"), 120.0, 60.0, 5.0, False),   # NaN observed_at (was fail-open)
    (100.0, float("nan"), 60.0, 5.0, False),   # NaN now
    (100.0, 1e9, float("nan"), 5.0, False),    # NaN limit (was fail-open vs a huge age)
    (100.0, 1e9, float("inf"), 5.0, False),    # inf limit (was fail-open)
    (float("inf"), 120.0, 60.0, 5.0, False),   # +inf observed_at
    (float("-inf"), 120.0, 60.0, 5.0, False),  # -inf observed_at
    # A non-finite tolerance must default to ZERO tolerance (fail-closed), never disable the future guard:
    (100.0, 90.0, 60.0, float("nan"), False),  # NaN tolerance -> tol 0 -> future (age -10) rejected
    (100.0, 90.0, 60.0, float("inf"), False),  # inf tolerance -> tol 0 -> future (age -10) rejected
    (100.0, 100.0, 60.0, float("nan"), True),  # NaN tolerance still permits a non-future, in-limit reading
]


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


def _results(fn):
    out = []
    for oa, now, limit, tol, _ in _FRESH_CASES:
        try:
            out.append(fn(oa, now, limit, tol))
        except Exception as exc:
            out.append(("RAISED", type(exc).__name__))
    return out


class FreshnessMutationTests(SimpleTestCase):
    def setUp(self):
        import hosted_workspace.producer as mod
        self.mod = mod
        self.tree = ast.parse(textwrap.dedent(inspect.getsource(_compute_freshness)))
        c = _Mutant(-1)
        c.visit(copy.deepcopy(self.tree))
        self.total = c.i + 1
        self.baseline = _results(_compute_freshness)

    def test_oracle_matches_expected(self):
        self.assertEqual(self.baseline, [c[-1] for c in _FRESH_CASES])

    def test_has_operators(self):
        self.assertGreaterEqual(self.total, 5)

    def test_every_mutant_killed(self):
        survivors = []
        for t in range(self.total):
            tree = copy.deepcopy(self.tree)
            _Mutant(t).visit(tree)
            ast.fix_missing_locations(tree)
            ns = dict(self.mod.__dict__)
            exec(compile(tree, "<m>", "exec"), ns)
            if _results(ns["_compute_freshness"]) == self.baseline:
                survivors.append(t)
        self.assertEqual(survivors, [], f"unkilled mutants: {survivors}")


_NUM_CASES = [0, 1, -3, 1.5, 1e9, float("nan"), float("inf"), float("-inf"), True, False, None, "5", ""]


class IsNumberMutationTests(SimpleTestCase):
    def setUp(self):
        import hosted_workspace.producer as mod
        self.mod = mod
        self.tree = ast.parse(textwrap.dedent(inspect.getsource(_is_number)))
        c = _Mutant(-1)
        c.visit(copy.deepcopy(self.tree))
        self.total = c.i + 1
        self.baseline = [_is_number(v) for v in _NUM_CASES]

    def test_has_operators(self):
        self.assertGreaterEqual(self.total, 2)

    def test_every_mutant_killed(self):
        survivors = []
        for t in range(self.total):
            tree = copy.deepcopy(self.tree)
            _Mutant(t).visit(tree)
            ast.fix_missing_locations(tree)
            ns = dict(self.mod.__dict__)
            exec(compile(tree, "<m>", "exec"), ns)
            fn = ns["_is_number"]
            try:
                result = [fn(v) for v in _NUM_CASES]
            except Exception:
                result = "RAISED"
            if result == self.baseline:
                survivors.append(t)
        self.assertEqual(survivors, [], f"unkilled mutants: {survivors}")
