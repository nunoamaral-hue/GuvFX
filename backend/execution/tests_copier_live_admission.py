"""ADR-0087 — NARROW RED copier live-mutation admission tests (the live-money demo-wall change).

These pin the single most safety-critical predicate: `_copier_live_mutation_admitted` / `_demo_or_admitted_ok`
in `scripts/mt5_signal_bridge.py`, which narrows the bridge `account_not_demo` wall for the ONE governed
route only. The bar: with the admission OFF (default) or any condition unmet, a LIVE account is refused exactly
as before (byte-identical); it admits ONLY the exact account/server/symbol with the full live stack on and the
command/connected/runtime identities in triple agreement; and it fails CLOSED on every miss and on any error.

The bridge is imported from source (mirroring execution.tests_bridge_binding); no MT5 and no network.
"""
import importlib.util
import os
import tempfile

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")
_SYNTH_AGENT = "synthetic-agent-" + "token"
_SYNTH_WORKER = "synthetic-worker-" + "token"

# The admission env knobs (all per-runtime; set in-test, never in the repo). The lifecycle guard is MANDATORY
# for a live mutation (it binds the ACTUAL position symbol/pre-state), so the guard flag is part of the stack.
_ADMIT_ENV = {
    "COPIER_LIVE_MUTATION_ADMIT_ENABLED": "1",
    "COPIER_LIFECYCLE_GUARD_ENABLED": "1",
    "MT5_ALLOW_LIVE": "1",
    "MT5_LIVE_EXPIRY": str(2 ** 31),   # overridden per-test by _Env with a valid future value
    "MT5_EXPECTED_LOGIN": "51763831",
    "MT5_EXPECTED_SERVER": "IS6Technologies-Live",
    "COPIER_LIVE_ADMIT_SYMBOL": "XAUUSD",
}


def _load_bridge():
    env = {"GUVFX_AGENT_TOKEN": _SYNTH_AGENT, "GUVFX_WORKER_TOKEN": _SYNTH_WORKER}
    prev = os.getcwd()
    prev_env = {k: os.environ.get(k) for k in env}
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        os.environ.update(env)
        try:
            spec = importlib.util.spec_from_file_location("mt5_bridge_admit_under_test", _BRIDGE_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            os.chdir(prev)
            for k, v in prev_env.items():
                os.environ[k] = v if v is not None else os.environ.pop(k, "")
                if v is None:
                    os.environ.pop(k, None)


class _Acct:
    def __init__(self, login="51763831", server="IS6Technologies-Live", trade_mode=1):
        self.login = login
        self.server = server
        self.trade_mode = trade_mode


def _cmd(**over):
    base = {"copier_operation": "FULL_CLOSE", "require_identity_pin": True, "symbol": "XAUUSD",
            "expected_login": "51763831", "expected_server": "IS6Technologies-Live"}
    base.update(over)
    return base


class _Env:
    """Set the admission env (with a valid future expiry) and restore afterwards."""
    def __init__(self, **over):
        import time
        self._vals = dict(_ADMIT_ENV)
        self._vals["MT5_LIVE_EXPIRY"] = str(int(time.time()) + 1800)   # 30 min future (under 2h cap)
        self._vals.update(over)

    def __enter__(self):
        self._prev = {k: os.environ.get(k) for k in self._vals}
        for k, v in self._vals.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class CopierLiveAdmissionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_bridge()

    def _admit(self, acct=None, command=None, op="FULL_CLOSE"):
        return self.b._copier_live_mutation_admitted(acct or _Acct(), command or _cmd(), op)

    # ── default OFF / fail-closed baseline ──────────────────────────────────────────────────────────────
    def test_default_off_never_admits(self):
        # No admission env at all -> a LIVE account is refused (demo wall unchanged).
        for k in _ADMIT_ENV:
            os.environ.pop(k, None)
        self.assertFalse(self._admit())
        self.assertFalse(self.b._demo_or_admitted_ok(_Acct(trade_mode=1), _cmd(), "FULL_CLOSE"))

    def test_flag_on_but_no_live_stack_fails_closed(self):
        # The narrow flag on, but MT5_ALLOW_LIVE off -> _bridge_live_admitted False -> not admitted.
        with _Env(MT5_ALLOW_LIVE="0"):
            self.assertFalse(self._admit())

    def test_expiry_missing_or_past_fails_closed(self):
        with _Env(MT5_LIVE_EXPIRY=""):
            self.assertFalse(self._admit())
        with _Env(MT5_LIVE_EXPIRY="1"):   # far past
            self.assertFalse(self._admit())

    # ── the full happy admission ────────────────────────────────────────────────────────────────────────
    def test_full_stack_exact_route_admits(self):
        with _Env():
            self.assertTrue(self._admit())
            # _demo_or_admitted_ok mirrors it for a LIVE account.
            self.assertTrue(self.b._demo_or_admitted_ok(_Acct(trade_mode=1), _cmd(), "FULL_CLOSE"))

    def test_demo_account_always_ok(self):
        # trade_mode==0 is DEMO -> always ok, regardless of the admission (and even with it OFF).
        for k in _ADMIT_ENV:
            os.environ.pop(k, None)
        self.assertTrue(self.b._demo_or_admitted_ok(_Acct(trade_mode=0), None, "FULL_CLOSE"))

    # ── identity triple-agreement ───────────────────────────────────────────────────────────────────────
    def test_connected_login_mismatch_fails_closed(self):
        with _Env():
            self.assertFalse(self._admit(acct=_Acct(login="99999999")))   # wrong broker-connected account

    def test_command_login_mismatch_fails_closed(self):
        with _Env():
            self.assertFalse(self._admit(command=_cmd(expected_login="99999999")))

    def test_runtime_login_mismatch_fails_closed(self):
        with _Env(MT5_EXPECTED_LOGIN="99999999"):
            self.assertFalse(self._admit())

    def test_server_mismatch_fails_closed(self):
        with _Env():
            self.assertFalse(self._admit(acct=_Acct(server="OtherBroker-Live")))
            self.assertFalse(self._admit(command=_cmd(expected_server="OtherBroker-Live")))

    # ── symbol pin ──────────────────────────────────────────────────────────────────────────────────────
    def test_symbol_mismatch_fails_closed(self):
        with _Env():
            self.assertFalse(self._admit(command=_cmd(symbol="EURUSD")))
        with _Env(COPIER_LIVE_ADMIT_SYMBOL="EURUSD"):
            self.assertFalse(self._admit(command=_cmd(symbol="XAUUSD")))

    # ── command shape / operation allow-list ────────────────────────────────────────────────────────────
    def test_missing_command_fails_closed(self):
        with _Env():
            self.assertFalse(self.b._copier_live_mutation_admitted(_Acct(), None, "FULL_CLOSE"))

    def test_wrong_operation_fails_closed(self):
        with _Env():
            self.assertFalse(self._admit(command=_cmd(copier_operation="MODIFY_PROTECTION"), op="FULL_CLOSE"))
            # OPEN is not a mutation op -> not admissible via this path.
            self.assertFalse(self._admit(command=_cmd(copier_operation="PLACE_ORDER"), op="PLACE_ORDER"))

    def test_require_identity_pin_absent_fails_closed(self):
        with _Env():
            self.assertFalse(self._admit(command=_cmd(require_identity_pin=False)))

    def test_modify_and_partial_ops_admit(self):
        with _Env():
            self.assertTrue(self._admit(command=_cmd(copier_operation="MODIFY_PROTECTION"), op="MODIFY_PROTECTION"))
            self.assertTrue(self._admit(command=_cmd(copier_operation="PARTIAL_CLOSE"), op="PARTIAL_CLOSE"))

    # ── no global bypass / fail-closed on error ─────────────────────────────────────────────────────────
    def test_other_account_never_admitted_even_fully_armed(self):
        # A different, fully-"matching"-looking account (its own login/server) is NOT the governed route and is
        # refused, because the runtime MT5_EXPECTED_LOGIN pins the exact account.
        with _Env():
            other = _Acct(login="12345678", server="IS6Technologies-Live")
            self.assertFalse(self._admit(acct=other, command=_cmd(expected_login="12345678")))

    def test_garbage_account_fails_closed(self):
        with _Env():
            self.assertFalse(self._admit(acct=object()))   # no login/server attrs -> fail closed, no raise

    # ── C: the lifecycle guard is MANDATORY for a live mutation ─────────────────────────────────────────
    def test_guard_required_for_admission(self):
        with _Env(COPIER_LIFECYCLE_GUARD_ENABLED="0"):
            self.assertFalse(self._admit())   # admission + full stack on, but guard OFF -> denied

    # ── D: server pin is fail-closed on a blank leg ─────────────────────────────────────────────────────
    def test_blank_server_leg_fails_closed(self):
        with _Env():
            self.assertFalse(self._admit(acct=_Acct(server="")))              # blank connected server
            self.assertFalse(self._admit(command=_cmd(expected_server="")))   # blank command server

    # ── A: the SEND-TIME identity gate re-asserts the SAME admission (both walls consistent) ────────────
    def test_send_time_identity_gate_consistent_with_top_wall(self):
        acc = {"login": "51763831", "server": "IS6Technologies-Live", "trade_mode": 2}   # LIVE
        term = {"connected": True}
        expected = {"expected_login": "51763831", "expected_server": "IS6Technologies-Live"}
        # Armed -> the send-time gate admits the governed LIVE account (NOT account_not_demo).
        with _Env():
            ok, reason = self.b.evaluate_mutation_identity(acc, term, expected, command=_cmd(), op="FULL_CLOSE")
            self.assertTrue(ok, reason)
        # Admission OFF -> the send-time gate rejects with account_not_demo (byte-identical to before).
        for k in _ADMIT_ENV:
            os.environ.pop(k, None)
        ok2, reason2 = self.b.evaluate_mutation_identity(acc, term, expected, command=_cmd(), op="FULL_CLOSE")
        self.assertFalse(ok2)
        self.assertEqual(reason2, "account_not_demo")

    def test_send_time_gate_demo_always_ok_no_command(self):
        demo = {"login": "999", "server": "Demo", "trade_mode": 0}
        term = {"connected": True}
        ok, _ = self.b.evaluate_mutation_identity(
            demo, term, {"expected_login": None, "expected_server": None}, command=None, op=None)
        self.assertTrue(ok)   # demo + legacy (no command) -> unchanged
