"""P0 native single-instance launcher integrity gate — wiring + fail-closed semantics + static host-artefact checks.

The gate is DARK by default and, when armed, fails provisioning closed (``PREP_LAUNCHER_FAILED``) unless the host
read-only-verifies the certified launcher (exists / SHA256 matches the pinned manifest / ACL non-tenant-writable /
AppLocker allow present / runtime exists) — so no tenant is ever pointed at an absent/tampered/unallow-listed
launcher. These tests prove the wiring + fail-closed behaviour against the in-memory host fake and statically
assert the new read-only verify script is ASCII-only + non-mutating and the RemoteApp arming branch is present.
Zero host contact.
"""
import os

from django.test import SimpleTestCase, TestCase, override_settings

from hosted_workspace import host_agent_dispatch as D
from hosted_workspace import host_protocol as P
from hosted_workspace import slot_preparation as SP
from hosted_workspace.tests_host_executor import KR, T, _executor
from hosted_workspace.tests_slot_preparation import FakeExecutor, _PREP_ON, _bound_ws

_LGATE_ON = dict(_PREP_ON, HOSTED_NATIVE_LAUNCHER_GATE_ENABLED="1", HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")

_WIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "terminal_provisioning", "windows")
_VERIFY = os.path.join(_WIN_DIR, "Verify-GuvfxNativeLauncher.ps1")
_REMOTEAPP = os.path.join(_WIN_DIR, "Set-GuvfxRemoteApp.ps1")

_ALL_TRUE = dict(launcher_exists=True, sha256_matches=True, acl_safe=True,
                 applocker_allow_present=True, runtime_exists=True)


class LauncherGateFakeExecutor(FakeExecutor):
    """Adds the read-only native-launcher verify step. ``verdict`` overrides individual booleans."""

    def __init__(self, *, verdict=None, **kw):
        super().__init__(**kw)
        self._verdict = dict(_ALL_TRUE, **(verdict or {}))

    def verify_native_launcher(self, username, runtime_root, rdp_host=None):
        self.calls.append("verify_native_launcher")
        if self.raise_at == "verify_native_launcher":
            raise RuntimeError("host boom")
        return {"ok": "verify_native_launcher" not in self.fail, **self._verdict}


@override_settings(**_LGATE_ON)
class NativeLauncherGateTests(TestCase):
    def _prep(self, ex):
        ws, acct, node = _bound_ws()
        return SP.prepare_hosted_slot(ws, executor=ex), ws

    def test_all_true_passes_and_runs_after_remoteapp(self):
        ex = LauncherGateFakeExecutor()
        res, _ = self._prep(ex)
        self.assertTrue(res.prepared, res.reason)
        self.assertIn("verify_native_launcher", ex.calls)
        self.assertGreater(ex.calls.index("verify_native_launcher"), ex.calls.index("verify_remoteapp"))

    def test_any_false_verdict_fails_closed(self):
        # One bound workspace reused across sub-cases: each verdict fails closed (slot never advances), so the
        # same ws can be re-prepared (a fresh _bound_ws per iteration would collide on the fixed node hostname).
        ws, _, _ = _bound_ws()
        for bad in ("launcher_exists", "sha256_matches", "acl_safe", "applocker_allow_present", "runtime_exists"):
            ex = LauncherGateFakeExecutor(verdict={bad: False})
            res = SP.prepare_hosted_slot(ws, executor=ex)
            self.assertFalse(res.prepared, bad)
            self.assertEqual(res.reason, SP.PREP_LAUNCHER_FAILED, bad)
            self.assertEqual(res.stage_reached, SP.ST_LAUNCHER, bad)
            self.assertFalse(res.detail.get(bad), bad)

    def test_ok_false_fails_closed(self):
        res, _ = self._prep(LauncherGateFakeExecutor(fail=("verify_native_launcher",)))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_LAUNCHER_FAILED)

    def test_verify_host_error_fails_closed(self):
        res, _ = self._prep(LauncherGateFakeExecutor(raise_at="verify_native_launcher"))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_LAUNCHER_FAILED)

    def test_missing_on_older_host_is_executor_incomplete(self):
        res, _ = self._prep(LauncherGateFakeExecutor(drop=("verify_native_launcher",)))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_EXECUTOR_INCOMPLETE)
        self.assertEqual(res.stage_reached, SP.ST_LAUNCHER)

    @override_settings(HOSTED_NATIVE_LAUNCHER_GATE_ENABLED="0")
    def test_flag_off_byte_identical_no_verify(self):
        ex = LauncherGateFakeExecutor(verdict={"sha256_matches": False})  # would fail if the gate ran
        res, _ = self._prep(ex)
        self.assertTrue(res.prepared, res.reason)
        self.assertNotIn("verify_native_launcher", ex.calls)


class NativeLauncherExecutorDispatchTests(SimpleTestCase):
    def test_executor_confines_and_refuses_cz(self):
        ex = _executor(account_id=24, result_by_op={"VERIFY_NATIVE_LAUNCHER": {"ok": True, **_ALL_TRUE}})
        self.assertTrue(ex.verify_native_launcher(username="guvfx_u_24",
                                                  runtime_root=r"C:\GuvFX\accounts\24")["ok"])
        self.assertFalse(ex.verify_native_launcher(username="guvfx_u_24", runtime_root=r"C:\evil")["ok"])
        from hosted_workspace.host_executor import SignedHostExecutor
        cz = SignedHostExecutor(account_id=1, rdp_host="x", transport=lambda *_a, **_k: {"ok": True},
                                keyring=KR, key_id="k1", base_url="x",
                                seal_password=lambda *a, **k: {}, reserved_ids=None, clock=lambda: T)
        self.assertFalse(cz.verify_native_launcher(username="guvfx_u_1",
                                                   runtime_root=r"C:\GuvFX\accounts\1")["ok"])

    def test_operation_registered_across_layers(self):
        self.assertIn("VERIFY_NATIVE_LAUNCHER", P.HOSTED_OPERATIONS)
        self.assertEqual(D.OP_PRIMITIVES["VERIFY_NATIVE_LAUNCHER"]["primitive"], "verify_native_launcher")
        self.assertEqual(D.OP_PRIMITIVES["VERIFY_NATIVE_LAUNCHER"]["params_allow"], ())


class NativeLauncherHostScriptStaticTests(SimpleTestCase):
    def _read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def test_verify_script_is_ascii_only(self):
        self.assertTrue(all(b < 128 for b in self._read(_VERIFY)), "Verify-GuvfxNativeLauncher.ps1 has non-ASCII bytes")

    def test_verify_script_is_read_only(self):
        text = self._read(_VERIFY).decode("ascii")
        for forbidden in ("Start-Process", "Set-Acl", "Remove-Item", "New-Item", "Set-Content",
                          "Stop-Process", "Set-AppLockerPolicy"):
            self.assertNotIn(forbidden, text, f"launcher-verify must be read-only (no '{forbidden}')")
        for expect in ("launcher_exists", "sha256_matches", "acl_safe", "applocker_allow_present", "runtime_exists"):
            self.assertIn(expect, text)

    def test_remoteapp_has_launcher_arming_branch(self):
        text = self._read(_REMOTEAPP).decode("ascii")
        self.assertIn('[ValidateSet("terminal64","launcher")][string]$Target', text)
        self.assertIn('$Target -eq "launcher"', text)
        self.assertIn(r"C:\GuvFX\launcher\guvfx_launch.exe", text)
        # default (Target=terminal64) still publishes the legacy terminal64 /portable target byte-identically
        self.assertIn('"RequiredCommandLine" -Value "/portable"', text)


class NativeLauncherRemoteAppCouplingTests(SimpleTestCase):
    """verify_remoteapp (-> ENSURE_REMOTEAPP) repoints publish+verify to the launcher iff the flag is ON."""

    _SLOT = {"username": "guvfx_u_24", "runtime_root": r"C:\GuvFX\accounts\24",
             "terminal_root": r"C:\GuvFX\accounts\24\terminal", "remoteapp_alias": "guvfx_mt5_24",
             "account_id": 24}

    def test_ensure_remoteapp_allows_target_param(self):
        self.assertEqual(D.OP_PRIMITIVES["ENSURE_REMOTEAPP"]["params_allow"], ("target",))
        self.assertEqual(D.OP_PRIMITIVES["REMOVE_REMOTEAPP"]["params_allow"], ())

    def test_build_args_target_launcher(self):
        args = D._build_args("ENSURE_REMOTEAPP", self._SLOT, {"params": {"target": "launcher"}}, envelope_open=None)
        self.assertEqual(args["target"], "launcher")
        self.assertEqual(args["alias"], "guvfx_mt5_24")

    def test_build_args_defaults_to_terminal64(self):
        args = D._build_args("ENSURE_REMOTEAPP", self._SLOT, {}, envelope_open=None)
        self.assertEqual(args["target"], "terminal64")

    def test_build_args_rejects_unknown_target(self):
        from hosted_workspace.host_protocol import HostProtocolError
        with self.assertRaises(HostProtocolError):
            D._build_args("ENSURE_REMOTEAPP", self._SLOT, {"params": {"target": "evil.exe"}}, envelope_open=None)

    def test_remove_remoteapp_has_no_target(self):
        args = D._build_args("REMOVE_REMOTEAPP", self._SLOT, {}, envelope_open=None)
        self.assertNotIn("target", args)

    def _capture_verify_remoteapp(self, flag):
        ex = _executor(account_id=24, result_by_op={"ENSURE_REMOTEAPP": {"ok": True}})
        captured = {}
        ex._send = lambda op, **kw: (captured.update(op=op, **kw) or {"ok": True})
        with override_settings(HOSTED_NATIVE_LAUNCHER_GATE_ENABLED=flag):
            ex.verify_remoteapp(username="guvfx_u_24", runtime_root=r"C:\GuvFX\accounts\24")
        return captured

    def test_verify_remoteapp_flag_on_sends_launcher_target(self):
        cap = self._capture_verify_remoteapp("1")
        self.assertEqual(cap["op"], "ENSURE_REMOTEAPP")
        self.assertEqual(cap["params"], {"target": "launcher"})

    def test_verify_remoteapp_flag_off_sends_no_target(self):
        cap = self._capture_verify_remoteapp("0")
        self.assertEqual(cap["op"], "ENSURE_REMOTEAPP")
        self.assertIsNone(cap["params"])
