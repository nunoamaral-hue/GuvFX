"""STREAM 9E — tests for the live host observation bridge (observer harness + backend transport + resolver).

No-fake-ready: every lock asserts the FAIL-CLOSED behaviour that keeps a workspace from advancing without a
real, matching host observation. Covers the A (harness), B/C (op wiring), D (resolver/transport), and
G (execution isolation) matrix rows that are unit-testable off-host; the live E/F state-machine progression is
exercised against the certified consumer with a synthesised host snapshot.
"""
from types import SimpleNamespace

from django.test import TestCase, override_settings

from hosted_workspace import live_observe
from hosted_workspace.host_agent_dispatch import OP_PRIMITIVES
from hosted_workspace.host_protocol import CREDENTIALED_HOSTED_OPERATIONS, HOSTED_OPERATIONS
from terminal_provisioning.windows import run_observer


# ── Fakes for the session-bound observer harness ──────────────────────────────────────────────────────────
class _FakeTerm:
    def __init__(self, connected, trade_allowed):
        self.connected = connected
        self.trade_allowed = trade_allowed


class _FakeAcc:
    def __init__(self, login, server, trade_mode):
        self.login = login
        self.server = server
        self.trade_mode = trade_mode


class _FakeMt5:
    def __init__(self, *, term=None, acc=None, raise_on=None):
        self._term = term
        self._acc = acc
        self._raise_on = raise_on or set()
        self.shutdown_called = False
        self.login_called = False

    def terminal_info(self):
        if "terminal_info" in self._raise_on:
            raise RuntimeError("boom")
        return self._term

    def account_info(self):
        if "account_info" in self._raise_on:
            raise RuntimeError("boom")
        return self._acc

    def login(self, *a, **k):  # must NEVER be called by the observer
        self.login_called = True
        raise AssertionError("observer must never call mt5.login()")

    def shutdown(self):
        self.shutdown_called = True


class _FakeBridge:
    """Mimics scripts.mt5_signal_bridge: guarded_initialize (never launches) + _terminal_process_running."""
    def __init__(self, *, running=True, attach_ok=True, launched_flag=None):
        self._running = running
        self._attach_ok = attach_ok
        self._launched_flag = launched_flag

    def _terminal_process_running(self, path):
        return self._running

    def guarded_initialize(self, mt5, params):
        # A real guarded attach never launches; assert the observer passes ONLY a path (no login/pwd/server).
        assert set(params.keys()) <= {"path"}, "observer must pass only {'path'} to the guarded attach"
        if self._launched_flag is not None:
            self._launched_flag.append(params.get("path"))
        return self._attach_ok


class RunObserverHarnessTests(TestCase):
    def _snap(self, **kw):
        mt5 = kw.pop("mt5")
        bridge = kw.pop("bridge")
        return run_observer.observe(kw.pop("account_id", 18), mt5=mt5, bridge=bridge)

    def test_connected_matching_account_produces_ok_snapshot(self):
        mt5 = _FakeMt5(term=_FakeTerm(True, True), acc=_FakeAcc(1302575, "IS6Technologies-Demo", 0))
        snap = self._snap(account_id=18, mt5=mt5, bridge=_FakeBridge(running=True, attach_ok=True))
        self.assertTrue(snap["ok"])
        self.assertTrue(snap["process_running"])
        self.assertTrue(snap["attach_succeeded"])
        self.assertTrue(snap["ipc_available"])
        self.assertTrue(snap["terminal_connected"])
        self.assertTrue(snap["trade_allowed"])
        self.assertEqual(snap["observed_login"], "1302575")      # carried as string (mt5 login is int)
        self.assertEqual(snap["observed_server"], "IS6Technologies-Demo")
        self.assertEqual(snap["observed_trade_mode"], 0)
        self.assertEqual(snap["account_id"], 18)
        self.assertEqual(snap["target_path"], r"C:\GuvFX\accounts\18\terminal\terminal64.exe")
        self.assertFalse(mt5.login_called)                       # never authenticates
        self.assertTrue(mt5.shutdown_called)                     # releases the attach

    def test_no_terminal_running_fails_closed_no_attach(self):
        mt5 = _FakeMt5(term=_FakeTerm(True, True), acc=_FakeAcc(1302575, "IS6Technologies-Demo", 0))
        snap = self._snap(account_id=18, mt5=mt5, bridge=_FakeBridge(running=False))
        self.assertFalse(snap["ok"])
        self.assertFalse(snap["process_running"])
        self.assertFalse(snap["attach_attempted"])               # never attached to a down terminal
        self.assertEqual(snap["process_reason"], "terminal_not_running")

    def test_guarded_attach_refused_fails_closed(self):
        mt5 = _FakeMt5(term=_FakeTerm(True, True), acc=_FakeAcc(1302575, "X", 0))
        snap = self._snap(account_id=18, mt5=mt5, bridge=_FakeBridge(running=True, attach_ok=False))
        self.assertFalse(snap["ok"])
        self.assertTrue(snap["process_running"])
        self.assertTrue(snap["attach_attempted"])
        self.assertFalse(snap["attach_succeeded"])
        self.assertFalse(snap["ipc_available"])
        self.assertEqual(snap["attach_reason"], "guarded_attach_refused")

    def test_disconnected_terminal_reports_not_connected(self):
        mt5 = _FakeMt5(term=_FakeTerm(False, False), acc=None)
        snap = self._snap(account_id=18, mt5=mt5, bridge=_FakeBridge())
        # attach succeeded but broker not connected → ok True (a valid observation) but connected False
        self.assertTrue(snap["ok"])
        self.assertTrue(snap["ipc_available"])
        self.assertFalse(snap["terminal_connected"])
        self.assertIsNone(snap["observed_login"])

    def test_read_error_degrades_to_attached_but_unreadable(self):
        mt5 = _FakeMt5(raise_on={"terminal_info", "account_info"})
        snap = self._snap(account_id=18, mt5=mt5, bridge=_FakeBridge())
        # a raising read must not raise to the caller and must not fabricate a positive
        self.assertTrue(snap["ipc_available"])                   # attach held
        self.assertFalse(snap["terminal_connected"])
        self.assertIsNone(snap["observed_login"])

    def test_never_launches_only_path_passed(self):
        launched = []
        mt5 = _FakeMt5(term=_FakeTerm(True, True), acc=_FakeAcc(1302575, "IS6Technologies-Demo", 0))
        self._snap(account_id=18, mt5=mt5, bridge=_FakeBridge(launched_flag=launched))
        # guarded_initialize received only {'path'} (asserted inside the fake); path is the tenant terminal.
        self.assertEqual(launched, [r"C:\GuvFX\accounts\18\terminal\terminal64.exe"])

    def test_customer_zero_refused_by_main(self):
        rc = run_observer.main(["--account", "1"])
        self.assertEqual(rc, 2)

    def test_invalid_account_refused_by_main(self):
        self.assertEqual(run_observer.main(["--account", "0"]), 2)

    def test_result_path_is_server_derived_per_account(self):
        self.assertEqual(run_observer.result_path(18), r"C:\GuvFX\accounts\18\_obs\observation.json")


# ── Op wiring (B/C): the typed op is present, non-credentialed, mapped to exactly one primitive ────────────
class OpWiringTests(TestCase):
    def test_observe_workspace_is_a_hosted_operation(self):
        self.assertIn("OBSERVE_WORKSPACE", HOSTED_OPERATIONS)

    def test_observe_workspace_is_not_credentialed(self):
        self.assertNotIn("OBSERVE_WORKSPACE", CREDENTIALED_HOSTED_OPERATIONS)

    def test_observe_workspace_maps_to_one_reviewed_primitive(self):
        self.assertEqual(OP_PRIMITIVES["OBSERVE_WORKSPACE"]["primitive"], "observe_workspace")
        self.assertEqual(OP_PRIMITIVES["OBSERVE_WORKSPACE"]["params_allow"], ())   # no caller params

    def test_op_primitives_cover_exactly_the_operations(self):
        self.assertEqual(set(OP_PRIMITIVES), set(HOSTED_OPERATIONS))


# ── Backend transport / mapping (D) ───────────────────────────────────────────────────────────────────────
def _fake_workspace(*, state="WAITING_FOR_LOGIN", login="1302575", server="IS6Technologies-Demo",
                    account_id=18, rdp_host="100.79.101.19"):
    acct = SimpleNamespace(id=account_id, account_number=login,
                           broker_server=SimpleNamespace(server_name=server))
    return SimpleNamespace(id=5, workspace_uuid="ws-5", canonical_state=state, trading_account=acct,
                           execution_node=SimpleNamespace(rdp_host=rdp_host))


class BuildObservationFromHostTests(TestCase):
    _CONNECTED = {"ok": True, "process_running": True, "attach_attempted": True, "attach_succeeded": True,
                  "ipc_available": True, "terminal_connected": True, "trade_allowed": True,
                  "observed_login": "1302575", "observed_server": "IS6Technologies-Demo",
                  "observed_trade_mode": 0, "observed_at": 1_000_000.0}

    def test_non_ok_result_is_none(self):
        self.assertIsNone(live_observe.build_observation_from_host(_fake_workspace(), {"ok": False}))

    def test_non_dict_result_is_none(self):
        self.assertIsNone(live_observe.build_observation_from_host(_fake_workspace(), None))

    def test_connected_matching_yields_connected_matched_observation(self):
        obs = live_observe.build_observation_from_host(_fake_workspace(), dict(self._CONNECTED))
        self.assertIsNotNone(obs)
        self.assertTrue(obs.connected)
        self.assertTrue(obs.account_match)

    def test_wrong_login_does_not_match(self):
        r = dict(self._CONNECTED, observed_login="9999999")
        obs = live_observe.build_observation_from_host(_fake_workspace(), r)
        self.assertTrue(obs.connected)
        self.assertFalse(obs.account_match)     # producer compares observed-vs-expected → no false match

    def test_wrong_server_does_not_match(self):
        r = dict(self._CONNECTED, observed_server="Some-Other-Server")
        obs = live_observe.build_observation_from_host(_fake_workspace(), r)
        self.assertFalse(obs.account_match)

    def test_host_cannot_assert_identity_expected_comes_from_workspace(self):
        # Even if the host returns a login/server, the EXPECTED identity is the workspace's; a host that lies
        # about BOTH observed and (hypothetically) expected cannot force a match because expected is server-side.
        ws = _fake_workspace(login="1302575", server="IS6Technologies-Demo")
        r = dict(self._CONNECTED, observed_login="1302575", observed_server="IS6Technologies-Demo")
        self.assertTrue(live_observe.build_observation_from_host(ws, r).account_match)
        r2 = dict(self._CONNECTED, observed_login="1302575", observed_server="Evil-Server")
        self.assertFalse(live_observe.build_observation_from_host(ws, r2).account_match)


class LiveObserveFnGatingTests(TestCase):
    class _Executor:
        def __init__(self, result):
            self._result = result
        def observe(self):
            return self._result

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="0")
    def test_flag_off_returns_none(self):
        self.assertIsNone(live_observe.live_observe_fn(_fake_workspace()))

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1")
    def test_ineligible_state_short_circuits_no_host_contact(self):
        # PROVISIONING is not observation-meaningful → None WITHOUT resolving/contacting the executor.
        called = {"resolved": False}
        import hosted_workspace.host_executor as he
        orig = he.resolve_signed_host_executor
        he.resolve_signed_host_executor = lambda **k: called.__setitem__("resolved", True) or None
        try:
            self.assertIsNone(live_observe.live_observe_fn(_fake_workspace(state="PROVISIONING")))
        finally:
            he.resolve_signed_host_executor = orig
        self.assertFalse(called["resolved"])

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1")
    def test_executor_unresolved_returns_none(self):
        import hosted_workspace.host_executor as he
        orig = he.resolve_signed_host_executor
        he.resolve_signed_host_executor = lambda **k: None
        try:
            self.assertIsNone(live_observe.live_observe_fn(_fake_workspace()))
        finally:
            he.resolve_signed_host_executor = orig

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1")
    def test_valid_snapshot_is_consumed(self):
        import hosted_workspace.host_executor as he
        orig = he.resolve_signed_host_executor
        he.resolve_signed_host_executor = lambda **k: LiveObserveFnGatingTests._Executor(
            dict(BuildObservationFromHostTests._CONNECTED))
        try:
            obs = live_observe.live_observe_fn(_fake_workspace())
        finally:
            he.resolve_signed_host_executor = orig
        self.assertIsNotNone(obs)
        self.assertTrue(obs.connected and obs.account_match)


# ── Resolver (D): dark by default; live only when the flag is on ──────────────────────────────────────────
class ResolverTests(TestCase):
    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="0")
    def test_resolver_dark_when_flag_off(self):
        from hosted_workspace.management.commands.run_hosted_observations import _dark_observe_fn, resolve_observe_fn
        self.assertIs(resolve_observe_fn(), _dark_observe_fn)

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1")
    def test_resolver_live_when_flag_on(self):
        from hosted_workspace.management.commands.run_hosted_observations import resolve_observe_fn
        self.assertIs(resolve_observe_fn(), live_observe.live_observe_fn)
