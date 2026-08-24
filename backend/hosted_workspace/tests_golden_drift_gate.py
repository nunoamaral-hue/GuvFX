"""P0 golden-build/manifest consistency gate + single-instance launcher static checks.

The gate is DARK by default and, when armed, fails provisioning closed (``PREP_GOLDEN_DRIFT``) if the materialised
runtime's terminal64 build != the pinned golden manifest -- so a customer never launches a known-drifted build
(the defect behind the customer-visible MT5 update/UAC dead-end). These tests prove the wiring + fail-closed
semantics against the in-memory host fake, and statically assert the two new host artefacts (the launcher + the
build-verify primitive) are ASCII-only, reserved-id-safe, and non-mutating where required. Zero host contact.
"""
import os

from django.test import SimpleTestCase, TestCase, override_settings

from hosted_workspace import host_agent_dispatch as D
from hosted_workspace import host_protocol as P
from hosted_workspace import slot_preparation as SP
from hosted_workspace.tests_host_executor import KR, T, _executor
from hosted_workspace.tests_slot_preparation import FakeExecutor, _PREP_ON, _bound_ws

_GATE_ON = dict(_PREP_ON, HOSTED_GOLDEN_DRIFT_GATE_ENABLED="1", HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")

_WIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "terminal_provisioning", "windows")
_LAUNCHER = os.path.join(_WIN_DIR, "Launch-GuvfxTenantTerminal.ps1")
_VERIFY = os.path.join(_WIN_DIR, "Verify-GuvfxRuntimeBuild.ps1")


class GateFakeExecutor(FakeExecutor):
    """Adds the read-only build-verify host step. ``matches`` controls the drift verdict."""

    def __init__(self, *, matches=True, **kw):
        super().__init__(**kw)
        self._matches = matches

    def verify_runtime_build(self, runtime_root, rdp_host=None):
        self.calls.append("verify_runtime_build")
        if self.raise_at == "verify_runtime_build":
            raise RuntimeError("host boom")
        return {"ok": "verify_runtime_build" not in self.fail, "runtime_build": "5.0.0.5833",
                "manifest_build": "5.0.0.5833" if self._matches else "5.0.0.6036",
                "build_matches_manifest": self._matches}


@override_settings(**_GATE_ON)
class GoldenDriftGateTests(TestCase):
    def _prep(self, ex):
        ws, acct, node = _bound_ws()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        return res, ws

    def test_match_passes_and_runs_after_runtime_before_containment(self):
        ex = GateFakeExecutor(matches=True)
        res, _ = self._prep(ex)
        self.assertTrue(res.prepared, res.reason)
        self.assertIn("verify_runtime_build", ex.calls)
        i = ex.calls.index("verify_runtime_build")
        self.assertGreater(i, ex.calls.index("populate_runtime"))
        self.assertLess(i, ex.calls.index("apply_autotrading_config"))

    def test_drift_fails_closed_not_ready(self):
        res, _ = self._prep(GateFakeExecutor(matches=False))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_GOLDEN_DRIFT)
        self.assertEqual(res.stage_reached, SP.ST_GOLDEN_DRIFT)
        self.assertEqual(res.detail.get("runtime_build"), "5.0.0.5833")
        self.assertEqual(res.detail.get("manifest_build"), "5.0.0.6036")

    def test_verify_host_error_fails_closed(self):
        res, _ = self._prep(GateFakeExecutor(raise_at="verify_runtime_build"))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_GOLDEN_DRIFT)

    def test_missing_on_older_host_is_executor_incomplete(self):
        res, _ = self._prep(GateFakeExecutor(matches=True, drop=("verify_runtime_build",)))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_EXECUTOR_INCOMPLETE)
        self.assertEqual(res.stage_reached, SP.ST_GOLDEN_DRIFT)

    @override_settings(HOSTED_GOLDEN_DRIFT_GATE_ENABLED="0")
    def test_flag_off_byte_identical_no_verify(self):
        ex = GateFakeExecutor(matches=False)   # would fail if the gate ran
        res, _ = self._prep(ex)
        self.assertTrue(res.prepared, res.reason)
        self.assertNotIn("verify_runtime_build", ex.calls)


class GoldenDriftExecutorDispatchTests(SimpleTestCase):
    def test_executor_confines_and_refuses_cz(self):
        ex = _executor(account_id=24, result_by_op={"VERIFY_RUNTIME_BUILD":
                       {"ok": True, "build_matches_manifest": True}})
        self.assertTrue(ex.verify_runtime_build(runtime_root=r"C:\GuvFX\accounts\24")["ok"])
        self.assertFalse(ex.verify_runtime_build(runtime_root=r"C:\evil")["ok"])
        from hosted_workspace.host_executor import SignedHostExecutor
        cz = SignedHostExecutor(account_id=1, rdp_host="x", transport=lambda *_a, **_k: {"ok": True},
                                keyring=KR, key_id="k1", base_url="x",
                                seal_password=lambda *a, **k: {}, reserved_ids=None, clock=lambda: T)
        self.assertFalse(cz.verify_runtime_build(runtime_root=r"C:\GuvFX\accounts\1")["ok"])

    def test_operation_registered_across_layers(self):
        self.assertIn("VERIFY_RUNTIME_BUILD", P.HOSTED_OPERATIONS)
        self.assertEqual(D.OP_PRIMITIVES["VERIFY_RUNTIME_BUILD"]["primitive"], "verify_runtime_build")
        self.assertEqual(D.OP_PRIMITIVES["VERIFY_RUNTIME_BUILD"]["params_allow"], ())


class HostScriptStaticTests(SimpleTestCase):
    def _read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def test_new_scripts_are_ascii_only(self):
        for p in (_LAUNCHER, _VERIFY):
            self.assertTrue(all(b < 128 for b in self._read(p)), f"{os.path.basename(p)} has non-ASCII bytes")

    def test_launcher_reserved_ids_and_never_kills(self):
        text = self._read(_LAUNCHER).decode("ascii")
        self.assertIn("$RESERVED_ACCOUNT_IDS = @(1, 18)", text)
        self.assertIn("refusing_reserved_identity", text)
        self.assertIn("duplicate_terminal", text)
        # the launcher must never terminate a terminal (only the governed relaunch may) and never use a
        # machine-global cross-tenant mutex.
        for forbidden in ("Stop-Process", "taskkill", "Global\\GuvFX_MT5_launch\"",):
            self.assertNotIn(forbidden, text, f"launcher must not contain '{forbidden}'")
        self.assertIn('"Global\\GuvFX_MT5_launch_" + $AccountId', text)  # per-tenant mutex name

    def test_verify_script_is_read_only(self):
        text = self._read(_VERIFY).decode("ascii")
        for forbidden in ("Start-Process", "Set-Acl", "Remove-Item", "New-Item", "Set-Content", "Stop-Process"):
            self.assertNotIn(forbidden, text, f"build-verify must be read-only (no '{forbidden}')")
