"""STREAM 9E — tests for the live host observation bridge (observer harness + backend transport + resolver).

No-fake-ready: every lock asserts the FAIL-CLOSED behaviour that keeps a workspace from advancing without a
real, matching host observation. Covers the A (harness), B/C (op wiring), D (resolver/transport), and
G (execution isolation) matrix rows that are unit-testable off-host; the live E/F state-machine progression is
exercised against the certified consumer with a synthesised host snapshot.
"""
import time
from types import SimpleNamespace

from django.test import TestCase, override_settings

from hosted_workspace import live_observe
from hosted_workspace.host_agent_dispatch import OP_PRIMITIVES
from hosted_workspace.host_protocol import CREDENTIALED_HOSTED_OPERATIONS, HOSTED_OPERATIONS
from hosted_workspace.manager import _all_execution_conditions, derive_workspace_decision
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
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


def _corr(*, account_id=18, network_active=True, collected_at=1_000_000.0, remote_endpoints=None, **over):
    """A VALID LocalSystem corroboration block matching the server-derived identity of ``account_id`` (18 ->
    guvfx_u_18, C:\\GuvFX\\accounts\\18). LocalSystem enumerates raw ``remote_endpoints``; the backend classifies
    them into network_active. ``network_active=True`` -> one public endpoint; False -> none. Override any field."""
    if remote_endpoints is None:
        remote_endpoints = ["203.0.113.5"] if network_active else []
    d = {
        "account_id": account_id,
        "process_present": True,
        "exe_path": rf"C:\GuvFX\accounts\{account_id}\terminal\terminal64.exe",
        "owner_user": f"guvfx_u_{account_id}",
        "session_id": 2,
        "runtime_root": rf"C:\GuvFX\accounts\{account_id}",
        "remote_endpoints": remote_endpoints,
        "collected_at": collected_at,
    }
    d.update(over)
    return d


def _connected_result(*, corr=None, collected_at=None, **over):
    """A fully-positive tenant snapshot COMBINED with a valid LocalSystem corroboration block (the shape the host
    now returns). Overrides apply to the tenant fields; pass ``corr=`` to substitute the corroboration block."""
    tenant = {"ok": True, "process_running": True, "attach_attempted": True, "attach_succeeded": True,
              "ipc_available": True, "terminal_connected": True, "trade_allowed": True,
              "observed_login": "1302575", "observed_server": "IS6Technologies-Demo",
              "observed_trade_mode": 0, "observed_at": 1_000_000.0}
    tenant.update(over)
    c = _corr() if corr is None else corr
    if collected_at is not None:
        c = dict(c, collected_at=collected_at)
    tenant["corroboration"] = c
    return tenant


# now shortly after collected_at (age 10s < 60s limit) -> fresh; and far after -> stale.
_FRESH_NOW = 1_000_010.0
_STALE_NOW = 1_000_100.0


class BuildObservationFromHostTests(TestCase):
    def test_non_ok_result_is_none(self):
        self.assertIsNone(live_observe.build_observation_from_host(_fake_workspace(), {"ok": False}))

    def test_non_dict_result_is_none(self):
        self.assertIsNone(live_observe.build_observation_from_host(_fake_workspace(), None))

    def test_connected_matching_yields_connected_matched_observation(self):
        obs = live_observe.build_observation_from_host(_fake_workspace(), _connected_result(), now=_FRESH_NOW)
        self.assertIsNotNone(obs)
        self.assertTrue(obs.connected)
        self.assertTrue(obs.account_match)

    def test_wrong_login_does_not_match(self):
        r = _connected_result(observed_login="9999999")
        obs = live_observe.build_observation_from_host(_fake_workspace(), r, now=_FRESH_NOW)
        self.assertTrue(obs.connected)
        self.assertFalse(obs.account_match)     # producer compares observed-vs-expected -> no false match

    def test_wrong_server_does_not_match(self):
        r = _connected_result(observed_server="Some-Other-Server")
        obs = live_observe.build_observation_from_host(_fake_workspace(), r, now=_FRESH_NOW)
        self.assertFalse(obs.account_match)

    def test_host_cannot_assert_identity_expected_comes_from_workspace(self):
        # Even if the host returns a login/server, the EXPECTED identity is the workspace's; a host that lies
        # about BOTH observed and (hypothetically) expected cannot force a match because expected is server-side.
        ws = _fake_workspace(login="1302575", server="IS6Technologies-Demo")
        r = _connected_result(observed_login="1302575", observed_server="IS6Technologies-Demo")
        self.assertTrue(live_observe.build_observation_from_host(ws, r, now=_FRESH_NOW).account_match)
        r2 = _connected_result(observed_login="1302575", observed_server="Evil-Server")
        self.assertFalse(live_observe.build_observation_from_host(ws, r2, now=_FRESH_NOW).account_match)


class CorroborationAgreementTests(TestCase):
    """STREAM 9E hardening: a tenant snapshot advances ONLY with a matching, agreeing LocalSystem corroboration.
    Every mismatch / disagreement / forgery must fail closed to ``None`` (no ingest -> no advancement)."""

    def _build(self, result, now=_FRESH_NOW, ws=None):
        return live_observe.build_observation_from_host(ws or _fake_workspace(), result, now=now)

    def test_missing_corroboration_is_none(self):
        r = _connected_result()
        r.pop("corroboration")
        self.assertIsNone(self._build(r))     # a tenant-only snapshot can never advance

    def test_non_dict_corroboration_is_none(self):
        self.assertIsNone(self._build(_connected_result(corr="not-a-dict")))

    def test_wrong_account_id_is_none(self):
        # owner/runtime still match 18, but the corroboration's own account_id says 99 -> mismatch -> None.
        self.assertIsNone(self._build(_connected_result(corr=dict(_corr(), account_id=99))))

    def test_wrong_owner_is_none(self):
        self.assertIsNone(self._build(_connected_result(corr=_corr(owner_user="guvfx_u_99"))))

    def test_session_zero_is_none(self):
        self.assertIsNone(self._build(_connected_result(corr=_corr(session_id=0))))

    def test_process_absent_is_none(self):
        self.assertIsNone(self._build(_connected_result(corr=_corr(process_present=False))))

    def test_wrong_runtime_root_is_none(self):
        self.assertIsNone(self._build(_connected_result(corr=_corr(runtime_root=r"C:\GuvFX\accounts\99"))))

    def test_missing_collected_at_is_none(self):
        self.assertIsNone(self._build(_connected_result(corr=_corr(collected_at=None))))

    def test_forged_connected_without_network_is_none(self):
        # THE core hardening: a tenant claims terminal_connected=true, but LocalSystem enumerated NO endpoint for
        # that terminal -> disagreement -> no advancement (a forged 'connected' cannot pass).
        self.assertIsNone(self._build(_connected_result(corr=_corr(network_active=False))))

    def test_connected_with_only_private_endpoints_is_none(self):
        # LocalSystem enumerated endpoints, but ALL are loopback/private (no genuine external link). The backend
        # classifier decides network_active=false -> a tenant terminal_connected claim is refused.
        r = _connected_result(corr=_corr(remote_endpoints=["127.0.0.1", "10.0.0.5", "192.168.1.9"]))
        self.assertIsNone(self._build(r))

    def test_connected_with_a_public_endpoint_advances(self):
        # A genuine external link (a public remote endpoint) satisfies the agreement -> the observation is built.
        obs = self._build(_connected_result(corr=_corr(remote_endpoints=["10.0.0.5", "203.0.113.20"])))
        self.assertIsNotNone(obs)
        self.assertTrue(obs.connected)

    def test_not_connected_with_valid_corroboration_builds_waiting_observation(self):
        # A genuine 'still logging in' observation: tenant not connected, corroboration valid (no network yet).
        # No disagreement (the network agreement rule only gates a 'connected' claim) -> a (non-connected)
        # observation is produced so the manager can correctly hold WAITING_FOR_LOGIN.
        r = _connected_result(terminal_connected=False, corr=_corr(network_active=False))
        obs = self._build(r)
        self.assertIsNotNone(obs)
        self.assertFalse(obs.connected)


class FreshnessExecutionReadyTests(TestCase):
    """The single load-bearing safety lock: EXECUTION_READY is reachable for a FRESH, corroborated, connected,
    matched, trade-allowed workspace, and is NOT reachable when the observation is stale. Freshness is anchored on
    the LocalSystem ``collected_at`` vs the trusted clock, so neither staleness nor a forged tenant timestamp can
    flip it. If a regression made a stale/forged observation execution-ready, one of these fails."""

    def test_fresh_connected_matched_trade_allowed_is_execution_ready(self):
        obs = live_observe.build_observation_from_host(
            _fake_workspace(state="CONNECTED"), _connected_result(), now=_FRESH_NOW)
        self.assertTrue(obs.fresh)
        self.assertTrue(_all_execution_conditions(obs))
        decision = derive_workspace_decision(obs)   # legal CONNECTED -> EXECUTION_READY
        self.assertTrue(decision.execution_ready)
        self.assertEqual(decision.next_state, S.EXECUTION_READY.value)

    def test_stale_observation_is_not_execution_ready(self):
        obs = live_observe.build_observation_from_host(
            _fake_workspace(state="CONNECTED"), _connected_result(), now=_STALE_NOW)
        self.assertFalse(obs.fresh)                       # collected 100s ago, limit 60s -> stale
        self.assertFalse(_all_execution_conditions(obs))  # the fresh conjunct is required
        self.assertFalse(derive_workspace_decision(obs).execution_ready)

    def test_future_collected_at_beyond_tolerance_is_not_fresh(self):
        # A corroboration timestamp implausibly in the future (beyond clock tolerance) is not fresh (fail closed).
        obs = live_observe.build_observation_from_host(
            _fake_workspace(state="CONNECTED"), _connected_result(), now=1_000_000.0 - 100)
        self.assertFalse(obs.fresh)


@override_settings(HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="1")   # ADR-0041 trust anchor present for these cases
class LiveObserveFnGatingTests(TestCase):
    class _Executor:
        def __init__(self, result):
            self._result = result
        def observe(self):
            return self._result

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="0")
    def test_flag_off_returns_none(self):
        self.assertIsNone(live_observe.live_observe_fn(_fake_workspace()))

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1", HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="0")
    def test_uncertified_isolation_returns_none_even_when_armed(self):
        # ADR-0041 trust anchor: with the observation flag ON and a valid, fresh, corroborated snapshot
        # available, an observation is STILL not produced while RemoteApp isolation is uncertified -> the
        # channel stays DARK and nothing advances. This is the code enforcement of the certification dependency.
        obs = self._run_with_executor(_connected_result(collected_at=time.time()))
        self.assertIsNone(obs)

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

    def _run_with_executor(self, result):
        import hosted_workspace.host_executor as he
        orig = he.resolve_signed_host_executor
        he.resolve_signed_host_executor = lambda **k: LiveObserveFnGatingTests._Executor(result)
        try:
            return live_observe.live_observe_fn(_fake_workspace())
        finally:
            he.resolve_signed_host_executor = orig

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1")
    def test_valid_corroborated_snapshot_is_consumed(self):
        # collected_at ~= now so the observation is fresh under the real backend clock used by live_observe_fn.
        obs = self._run_with_executor(_connected_result(collected_at=time.time()))
        self.assertIsNotNone(obs)
        self.assertTrue(obs.connected and obs.account_match and obs.fresh)

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1")
    def test_forged_connected_without_corroboration_is_not_consumed(self):
        # End-to-end: a fully-positive tenant snapshot whose LocalSystem corroboration shows NO live connection is
        # refused by the transport mapper -> live_observe_fn yields None -> the workspace never advances.
        obs = self._run_with_executor(_connected_result(corr=_corr(network_active=False), collected_at=time.time()))
        self.assertIsNone(obs)


# ── Resolver (D): dark by default; live only when the flag is on ──────────────────────────────────────────
class ResolverTests(TestCase):
    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="0")
    def test_resolver_dark_when_flag_off(self):
        from hosted_workspace.management.commands.run_hosted_observations import _dark_observe_fn, resolve_observe_fn
        self.assertIs(resolve_observe_fn(), _dark_observe_fn)

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1", HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="1")
    def test_resolver_live_when_both_gates_on(self):
        from hosted_workspace.management.commands.run_hosted_observations import resolve_observe_fn
        self.assertIs(resolve_observe_fn(), live_observe.live_observe_fn)

    @override_settings(HOSTED_MT5_OBSERVATION_ENABLED="1", HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="0")
    def test_resolver_dark_when_isolation_uncertified(self):
        # ADR-0041 trust anchor is required even with observation armed -> dark until RemoteApp isolation certified.
        from hosted_workspace.management.commands.run_hosted_observations import _dark_observe_fn, resolve_observe_fn
        self.assertIs(resolve_observe_fn(), _dark_observe_fn)


# ── LocalSystem orchestrator guards (Invoke-GuvfxObserver.ps1) ────────────────────────────────────────────
# The load-bearing isolation / replay / Customer-Zero / corroboration locks live in the PowerShell orchestrator,
# which cannot be executed off-host. These STATIC assertions ensure a regression that DROPS one of the guards
# fails a test rather than shipping silently (the review's "orchestrator locks have zero coverage" finding).
class InvokeObserverGuardTests(TestCase):
    def _script(self):
        import hosted_workspace
        import os
        base = os.path.join(os.path.dirname(os.path.dirname(hosted_workspace.__file__)),
                            "terminal_provisioning", "windows", "Invoke-GuvfxObserver.ps1")
        with open(base, "r", encoding="ascii") as fh:
            return fh.read()

    def test_customer_zero_and_identity_locks_present(self):
        s = self._script()
        self.assertIn("$AccountId -eq 1", s)                 # Customer Zero refused
        self.assertIn("username_mismatch", s)                # server-derived username enforced
        self.assertIn("runtime_mismatch", s)
        self.assertIn("terminal_root_mismatch", s)

    def test_single_owned_session_terminal_proof_present(self):
        s = self._script()
        self.assertIn("GetOwner", s)                         # owner attribution
        self.assertIn("$ownerUser -ne $expectedUser", s)     # must be owned by the expected tenant
        self.assertIn("SessionId", s)                        # interactive session (>0)
        self.assertIn("duplicate_terminal", s)               # more than one -> fail closed
        self.assertIn("terminal_not_running", s)

    def test_replay_and_task_trust_locks_present(self):
        s = self._script()
        self.assertIn("triggerAt", s)                        # delete-before-trigger + written-after freshness
        self.assertIn("LastWriteTime", s)
        self.assertIn("observer_task_action_untrusted", s)   # observer task action validated
        self.assertIn("result_account_mismatch", s)

    def test_localsystem_corroboration_present(self):
        s = self._script()
        self.assertIn("corroboration", s)
        self.assertIn("Get-TerminalRemoteEndpoints", s)      # LocalSystem ENUMERATES endpoints (raw)
        self.assertIn("remote_endpoints", s)
        self.assertIn("collected_at", s)
        # The public/private CLASSIFICATION must NOT live in the .ps1 - it moved to the tested backend classifier
        # (live_observe._is_public_ip), so a PowerShell edit can never silently change the load-bearing decision.
        self.assertNotIn("Test-PublicIp", s)
        self.assertNotIn("network_active", s)

    def test_netstat_fallback_parses_the_correct_fields(self):
        # RULE 11 (field-index regression guard): the netstat fallback must read STATE from column 4 and PID
        # from column 5, and only count ESTABLISHED connections. A silent index shift (e.g. reading the LOCAL
        # address, or the wrong PID) would weaken the corroboration; assert the exact indices are present.
        s = self._script()
        self.assertIn('$parts[3] -ne "ESTABLISHED"', s)      # column 4 = connection state
        self.assertIn("[int]$parts[4] -ne $procId", s)       # column 5 = owning PID
        self.assertIn("$remote = $parts[2]", s)              # column 3 = REMOTE address (not local)


# ── Network public-IP classification: RULE 11 positive + negative control on the REAL classifier ──────────
# network_active is the sole LocalSystem agreement conjunct for a tenant `terminal_connected` claim. The
# classification was deliberately moved OUT of PowerShell (which cannot run in CI) INTO the backend
# (live_observe._is_public_ip), so these positive/negative controls exercise the ACTUAL production code path
# that gates the agreement - not a mirror. The LocalSystem primitive only ENUMERATES raw endpoints.
class NetworkClassificationTests(TestCase):
    def test_positive_control_public_ips(self):
        # A genuine broker/public endpoint MUST classify as public (else a connected workspace would stall).
        for ip in ("203.0.113.7", "8.8.8.8", "172.32.0.1", "172.15.0.1", "100.63.0.1", "100.128.0.1",
                   "2606:4700:4700::1111"):
            self.assertTrue(live_observe._is_public_ip(ip), ip)

    def test_negative_control_reserved_ips(self):
        # Loopback / RFC1918 / link-local / CGNAT/Tailscale / IPv6 loopback+ULA / junk MUST NOT be public.
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.10.10", "172.16.0.1", "172.31.255.254",
                   "100.64.0.1", "100.127.255.254", "::1", "::", "0.0.0.0", "fe80::1", "fc00::1", "fd12::1",
                   # full fe80::/10 link-local range, case-insensitive, and IPv4-mapped private:
                   "fe90::1", "feab::1", "FE80::1", "FC00::1", "::ffff:10.0.0.1", "::ffff:192.168.1.1",
                   "", None):
            self.assertFalse(live_observe._is_public_ip(ip), ip)

    def test_ipv4_mapped_public_is_public(self):
        # An IPv4-mapped IPv6 wrapping a PUBLIC v4 address is still public (classified by the embedded v4).
        self.assertTrue(live_observe._is_public_ip("::ffff:203.0.113.7"))

    def test_network_active_requires_a_public_endpoint(self):
        self.assertTrue(live_observe._network_active({"remote_endpoints": ["10.0.0.1", "203.0.113.9"]}))
        self.assertFalse(live_observe._network_active({"remote_endpoints": ["10.0.0.1", "192.168.1.2"]}))  # private only
        self.assertFalse(live_observe._network_active({"remote_endpoints": []}))
        self.assertFalse(live_observe._network_active({}))                                # missing key
        self.assertFalse(live_observe._network_active({"remote_endpoints": "203.0.113.9"}))  # not a list
