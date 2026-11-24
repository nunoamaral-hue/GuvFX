"""GFX-DASHBOARD-LIVE-POSITIONS-PROVISIONING-WIRING-137 (ADR-0111) — adversarial matrix for the monitoring-only
read-only-positions bridge reconciler. Proves: dry-run mutates nothing; only the read flag is ever rendered;
MT5_ALLOW_LIVE can never be introduced; owner/identity/allow-list refusals fail closed; #48 read enablement arms NO
execution; byte-exact rollback on failure; partial-failure isolation; idempotency; synthetic read-back; and the
module touches no order surface. The signed host transport is a fake — no bridge is contacted.
"""
import inspect
import os
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from hosted_workspace import monitoring_reconcile as MR
from hosted_workspace.monitoring_reconcile import (MonitoringReconcileOps, render_readonly_env,
                                                   _env_delta_is_only_readonly, _verify_off_attestation)

U = get_user_model()
RO_LINE = "set MT5_LIVE_READONLY_POSITIONS_ENABLED=1"


def _ep(account_id=48, login="500123", server="IS6Technologies-Live"):
    return SimpleNamespace(trading_account_id=account_id, runtime_path="C:/x", port=8801,
                           expected_login=login, expected_server=server, windows_username="guvfx_u_48",
                           workspace_uuid="11111111-1111-1111-1111-111111111111", is_demo=False)


# ───────────────────────── pure-core safety invariants (no DB) ─────────────────────────
class RenderInvariants(SimpleTestCase):
    def test_monitoring_render_adds_only_read_flag_never_allow_live(self):
        with mock.patch.object(MR, "resolve_ready_endpoint", return_value=_ep()):
            _b, _s, base = render_readonly_env(SimpleNamespace(id=48), readonly=False)
            _b2, _s2, mon = render_readonly_env(SimpleNamespace(id=48), readonly=True)
        self.assertNotIn("MT5_ALLOW_LIVE", base)
        self.assertNotIn("MT5_LIVE_EXPIRY", base)
        self.assertNotIn(RO_LINE, base)                      # baseline carries NO read flag
        self.assertIn(RO_LINE, mon)                          # monitoring carries exactly the read flag
        self.assertNotIn("MT5_ALLOW_LIVE", mon)              # and NEVER an execution key
        self.assertTrue(_env_delta_is_only_readonly(base, mon))

    def test_env_delta_guard_rejects_any_non_readonly_delta(self):
        with mock.patch.object(MR, "resolve_ready_endpoint", return_value=_ep()):
            _b, _s, base = render_readonly_env(SimpleNamespace(id=48), readonly=False)
        smuggled = base + "\r\n" + RO_LINE + "\r\nset MT5_ALLOW_LIVE=1"
        self.assertFalse(_env_delta_is_only_readonly(base, smuggled))     # extra MT5_ALLOW_LIVE line => refused
        self.assertFalse(_env_delta_is_only_readonly(base, base))          # no read flag added => not an enable
        self.assertTrue(_env_delta_is_only_readonly(base, base + "\r\n" + RO_LINE))

    def test_no_ready_endpoint_fails_closed(self):
        with mock.patch.object(MR, "resolve_ready_endpoint", return_value=None):
            with self.assertRaises(ValueError):
                render_readonly_env(SimpleNamespace(id=48), readonly=True)


class AttestationVerify(SimpleTestCase):
    def _att(self, **over):
        d = {"ok": True, "account_id": 48, "port": 8801, "env_sha256": "abc", "allow_live": False,
             "require_pin": True, "guarded_attach": True, "listening": True}
        d.update(over)
        return d

    def test_good_off_attestation_passes(self):
        ok, r = _verify_off_attestation(self._att(), account_id=48, port=8801, expect_env_sha="abc")
        self.assertTrue(ok, r)

    def test_live_posture_in_attestation_is_hard_failure(self):
        ok, r = _verify_off_attestation(self._att(allow_live=True), account_id=48, port=8801, expect_env_sha="abc")
        self.assertFalse(ok)
        self.assertEqual(r, "unexpected_live_posture")

    def test_env_sha_mismatch_refused(self):
        ok, r = _verify_off_attestation(self._att(env_sha256="zzz"), account_id=48, port=8801, expect_env_sha="abc")
        self.assertFalse(ok)
        self.assertEqual(r, "env_sha_mismatch")

    def test_account_or_pin_or_listen_failures(self):
        self.assertFalse(_verify_off_attestation(self._att(account_id=99), account_id=48, port=8801, expect_env_sha="abc")[0])
        self.assertFalse(_verify_off_attestation(self._att(require_pin=False), account_id=48, port=8801, expect_env_sha="abc")[0])
        self.assertFalse(_verify_off_attestation(self._att(listening=False), account_id=48, port=8801, expect_env_sha="abc")[0])


class ModuleOrderSurfaceHygiene(SimpleTestCase):
    """The reconciler + command must touch NO broker order surface and import NO broker API (hosted-capstone invariant)."""
    def test_no_order_surface_or_broker_import(self):
        import re
        from hosted_workspace.management.commands import reconcile_monitoring_readonly_positions as CMD
        for mod in (MR, CMD):
            src = inspect.getsource(mod)
            self.assertIsNone(re.search(r"\border_send\s*\(|\border_check\s*\(", src), mod.__name__)
            self.assertIsNone(re.search(r"^[ \t]*(?:import|from)[ \t]+MetaTrader5\b", src, re.M), mod.__name__)
            self.assertNotIn("activate_tenant_bridge_live_authorized", src)   # never the LIVE/execution transport


# ───────────────────────── fake host transport ─────────────────────────
class _FakeOps(MonitoringReconcileOps):
    def __init__(self, *, bad_attest_for=(), bad_readback_for=(), undispatched_for=(), bad_rollback_for=()):
        self.applies = []                       # (account_id, env_sha, expect_readonly)
        self.bad_attest_for = set(bad_attest_for)
        self.bad_readback_for = set(bad_readback_for)
        # Host-gated DORMANT path: the signed executor is unavailable, so NOTHING is dispatched to the bridge
        # (mirrors ``_RealMonitoringOps.apply_off_env`` returning ``(False, None, "host_executor_unavailable")``).
        self.undispatched_for = set(undispatched_for)
        # The baseline ROLLBACK dispatch (expect_readonly=False) is sent but attests badly — a genuine rollback failure.
        self.bad_rollback_for = set(bad_rollback_for)

    def apply_off_env(self, account, *, env_b64, env_sha, port, runtime_root, expect_readonly):
        self.applies.append((account.id, env_sha, expect_readonly))
        if account.id in self.undispatched_for:
            return (False, None, "host_executor_unavailable")     # never dispatched: bridge byte-unchanged
        # The ENABLE apply (expect_readonly=True) fails attestation for bad_attest_for; the baseline ROLLBACK
        # (expect_readonly=False) fails attestation for bad_rollback_for. Otherwise the apply attests OK.
        bad = ((expect_readonly and account.id in self.bad_attest_for)
               or ((not expect_readonly) and account.id in self.bad_rollback_for))
        sha = "WRONG_SHA" if bad else env_sha
        return (True, {"ok": True, "account_id": account.id, "port": port, "env_sha256": sha,
                       "allow_live": False, "require_pin": True, "guarded_attach": True, "listening": True}, "dispatched")

    def read_back_positions(self, account):
        st = 400 if account.id in self.bad_readback_for else 200
        return {"http_status": st, "ok": st == 200, "reason": "synthetic"}


# ───────────────────────── DB-backed reconcile matrix ─────────────────────────
@override_settings(LIVE_READONLY_POSITIONS_MONITORING_ENABLED="", COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED="")
class ReconcileMatrix(TestCase):
    _n = 0

    @classmethod
    def _uniq(cls):
        cls._n += 1
        return "9%05d" % cls._n

    def _mk(self, owner, *, canonical_state="CONNECTED", ep_ready=True, ep_is_demo=False, login=None, server=None):
        from trading.models import TradingAccount, BrokerServer
        from execution.models import HostedExecutionEndpoint, TerminalNode
        from hosted_workspace.models import HostedMt5Workspace
        from hosted_workspace.models import HostedMt5Workspace as _H
        login = login or self._uniq()
        server = server or ("Srv-%s-Live" % self._uniq())
        bs = BrokerServer.objects.create(broker_display_name="B", server_name=server, environment="live")
        node = TerminalNode.objects.create(hostname="n-%s" % self._uniq(), status=TerminalNode.Status.ACTIVE,
                                           rdp_host="100.0.0.9", max_accounts=8)
        acct = TradingAccount.objects.create(
            user=owner, name="a", broker_name="B", account_number=login, is_demo=ep_is_demo,
            is_active=True, broker_server=bs, terminal_node=node,
            readiness_provider="persistent_workspace")
        ws = HostedMt5Workspace.objects.create(
            trading_account=acct, execution_node=node, workspace_node=node,
            state="NOT_PROVISIONED",                      # stale legacy field (production shape) — must be IGNORED
            canonical_state=canonical_state, proj_connected=True, execution_enabled=False)
        ep_state = HostedExecutionEndpoint.State.READY if ep_ready else HostedExecutionEndpoint.State.ALLOCATED
        HostedExecutionEndpoint.objects.create(
            workspace=ws, trading_account=acct, terminal_node=node, host="h",
            port=8800 + (self._n % 90) + 1, base_url="http://h:8800",
            windows_username="guvfx_u_%s" % acct.id, runtime_path="C:/x",
            expected_login=login, expected_server=server, is_demo=ep_is_demo,
            workspace_uuid=ws.workspace_uuid, state=ep_state)
        return acct, ws

    def setUp(self):
        self.owner = U.objects.create_user(username="o%s" % self._uniq(), email="o@x.invalid", password="x")
        self.other = U.objects.create_user(username="x%s" % self._uniq(), email="x@x.invalid", password="x")
        self._live = mock.patch("trading.account_policy.is_live_environment", return_value=True)
        self._live.start()
        self.addCleanup(self._live.stop)

    def _allow(self, *accts):
        return override_settings(LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST=" ".join(str(a.id) for a in accts))

    # (1) dry-run makes no host change
    def test_dry_run_mutates_nothing(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps()
        with self._allow(a):
            rep = MR.reconcile([a.id], commit=False, expected_owner_id=self.owner.id, ops=ops)
        self.assertEqual(ops.applies, [])                              # NEVER dispatched
        self.assertTrue(rep["results"][0]["dry_run"])
        self.assertTrue(rep["results"][0]["ok"])
        self.assertEqual(rep["results"][0]["preview"]["env_carries_allow_live"], False)

    # (2) one approved LIVE account receives ONLY the read flag
    def test_single_approved_receives_only_read_flag(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps()
        with self._allow(a):
            rep = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
        r = rep["results"][0]
        self.assertTrue(r["ok"] and r["applied"], r)
        # exactly one ENABLE apply carrying the read flag, and it verified
        self.assertEqual([x for x in ops.applies if x[2] is True].__len__(), 1)
        self.assertEqual(r["reason"], "readonly_enabled")
        self.assertEqual(r["read_back"]["http_status"], 200)

    # (3) multiple exact approved accounts remain tenant-isolated
    def test_multiple_accounts_tenant_isolated(self):
        a, _ = self._mk(self.owner)
        b, _ = self._mk(self.owner)
        ops = _FakeOps()
        with self._allow(a, b):
            rep = MR.reconcile([a.id, b.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
        self.assertEqual(rep["summary"]["applied"], 2)
        applied_ids = sorted(x[0] for x in ops.applies if x[2] is True)
        self.assertEqual(applied_ids, sorted([a.id, b.id]))           # each account's OWN apply
        self.assertEqual(len(set(x[1] for x in ops.applies)), 2)       # distinct per-account env shas

    # (4) unapproved / cross-owner / malformed refuse (fail-closed), nothing dispatched
    def test_refusals_fail_closed(self):
        approved, _ = self._mk(self.owner)
        unapproved, _ = self._mk(self.owner)
        cross, _ = self._mk(self.other)
        ops = _FakeOps()
        with override_settings(LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST=str(approved.id)):
            # unapproved account (not on allow-list)
            r1 = MR.reconcile([unapproved.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
            self.assertFalse(r1["results"][0]["ok"])
            self.assertIn("allowlisted", r1["results"][0]["failed_gates"])
            # cross-owner account (allow-list even containing it, but wrong expected owner)
            with override_settings(LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST=str(cross.id)):
                r2 = MR.reconcile([cross.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
            self.assertFalse(r2["results"][0]["ok"])
            self.assertIn("owner_matches", r2["results"][0]["failed_gates"])
            # empty/malformed allow-list => approved account refused
            with override_settings(LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST="not,ints ; garbage"):
                r3 = MR.reconcile([approved.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
            self.assertFalse(r3["results"][0]["ok"])
            self.assertIn("allowlisted", r3["results"][0]["failed_gates"])
        self.assertEqual(ops.applies, [])                              # NONE dispatched

    # (4b) identity mismatch refuses
    def test_identity_mismatch_refused(self):
        a, _ = self._mk(self.owner, login="500111", server="IS6Technologies-Live")
        ops = _FakeOps()
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id,
                             expected_login="999999", ops=ops)       # operator pin disagrees with the account
        self.assertFalse(r["results"][0]["ok"])
        self.assertIn("operator_login_pin", r["results"][0]["failed_gates"])
        self.assertEqual(ops.applies, [])

    # (4c) unsupported workspace state refuses
    def test_unsupported_workspace_refused(self):
        a, _ = self._mk(self.owner, canonical_state="PROVISIONING")
        ops = _FakeOps()
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
        self.assertFalse(r["results"][0]["ok"])
        self.assertIn("workspace_supported", r["results"][0]["failed_gates"])

    # ───── GFX-141: workspace_supported uses the AUTHORITATIVE canonical_state, not the stale legacy state ─────
    def test_gfx141_legacy_state_ignored_canonical_connected_permitted(self):
        # The exact production shape: stale legacy state=NOT_PROVISIONED, authoritative canonical_state=CONNECTED.
        a, ws = self._mk(self.owner, canonical_state="CONNECTED")
        self.assertEqual(ws.state, "NOT_PROVISIONED")              # the stale field the OLD gate wrongly read
        with self._allow(a):
            r = MR.reconcile([a.id], commit=False, expected_owner_id=self.owner.id)["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertNotIn("workspace_supported", r.get("failed_gates", []))

    def test_gfx141_canonical_execution_ready_permitted_execution_off(self):
        from trade_copier.models import CopierLiveArm
        from execution.models import LiveExecutionAuthorization
        a, ws = self._mk(self.owner, canonical_state="EXECUTION_READY")
        ops = _FakeOps()
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)["results"][0]
        self.assertTrue(r["ok"] and r["applied"], r)              # EXECUTION_READY is monitoring-capable...
        ws.refresh_from_db()
        self.assertFalse(ws.execution_enabled)                    # ...and execution stays OFF
        self.assertFalse(CopierLiveArm.objects.filter(follower_account_id=a.id).exists())
        self.assertFalse(LiveExecutionAuthorization.objects.filter(trading_account_id=a.id).exists())

    def test_gfx141_unsafe_canonical_states_refused(self):
        # transitional / login-required / disconnected / recovering / suspended / retired / active-execution all refuse
        for cs in ("PROVISIONING", "WAITING_FOR_LOGIN", "DISCONNECTED", "RECOVERING",
                   "SUSPENDED", "RETIRED", "EXECUTING"):
            a, _ = self._mk(self.owner, canonical_state=cs)
            ops = _FakeOps()
            with self._allow(a):
                r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)["results"][0]
            self.assertFalse(r["ok"], cs)
            self.assertIn("workspace_supported", r["failed_gates"], cs)
            self.assertEqual(ops.applies, [], cs)                 # nothing dispatched

    def test_gfx141_missing_or_malformed_canonical_refused(self):
        # empty, unknown, and a case/format variation of a real state are all malformed => fail closed
        for cs in ("", "BOGUS_STATE", "connected"):
            a, _ = self._mk(self.owner, canonical_state=cs)
            with self._allow(a):
                r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=_FakeOps())["results"][0]
            self.assertFalse(r["ok"], repr(cs))
            self.assertIn("workspace_supported", r["failed_gates"], repr(cs))

    def test_gfx141_proj_connected_cannot_override_unsafe_canonical(self):
        # _mk always sets proj_connected=True; a DISCONNECTED canonical must STILL refuse (proj_connected is not authority)
        a, ws = self._mk(self.owner, canonical_state="DISCONNECTED")
        self.assertTrue(ws.proj_connected)
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=_FakeOps())["results"][0]
        self.assertFalse(r["ok"])
        self.assertIn("workspace_supported", r["failed_gates"])

    def test_gfx141_six_production_shapes_pass(self):
        # six accounts in the exact prod shape (stale state=NOT_PROVISIONED, canonical=CONNECTED) all pass the gate
        accts = [self._mk(self.owner, canonical_state="CONNECTED")[0] for _ in range(6)]
        ops = _FakeOps()
        with self._allow(*accts):
            rep = MR.reconcile([a.id for a in accts], commit=True, expected_owner_id=self.owner.id, ops=ops)
        self.assertEqual(rep["summary"]["ok"], 6)
        self.assertEqual(rep["summary"]["applied"], 6)
        for r in rep["results"]:
            self.assertEqual(r["read_back"]["http_status"], 200)

    # (5) #48-style read enablement leaves Copy-Trading execution controls OFF
    def test_readonly_enable_leaves_execution_off(self):
        from trade_copier.models import CopierLiveArm, CopierLiveAuthorization
        from execution.models import LiveExecutionAuthorization
        a, ws = self._mk(self.owner)
        ops = _FakeOps()
        with self._allow(a):
            rep = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
        self.assertTrue(rep["results"][0]["applied"])
        ws.refresh_from_db()
        self.assertFalse(ws.execution_enabled)                        # execution flag untouched
        self.assertFalse(CopierLiveArm.objects.filter(follower_account_id=a.id).exists())
        self.assertFalse(CopierLiveAuthorization.objects.filter(follower_account_id=a.id).exists())
        self.assertFalse(LiveExecutionAuthorization.objects.filter(trading_account_id=a.id).exists())

    # (5b) an account with ACTIVE live execution is refused (never reconcile mid-execution)
    def test_active_execution_account_refused(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps()
        with self._allow(a):
            with mock.patch.object(MR, "_account_has_active_execution", return_value=True):
                r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
        self.assertFalse(r["results"][0]["ok"])
        self.assertIn("execution_off", r["results"][0]["failed_gates"])
        self.assertEqual(ops.applies, [])

    # (8) rollback restores the exact byte baseline on attestation failure
    def test_rollback_restores_baseline_on_attestation_failure(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps(bad_attest_for={a.id})
        with self._allow(a):
            rep = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
        r = rep["results"][0]
        self.assertFalse(r["ok"])
        self.assertTrue(r["rolled_back"])
        self.assertTrue(r["reason"].startswith("attestation_failed"))
        enable = [x for x in ops.applies if x[2] is True]
        rollback = [x for x in ops.applies if x[2] is False]
        self.assertEqual(len(enable), 1)
        self.assertEqual(len(rollback), 1)
        self.assertEqual(rollback[0][1], r["preview"]["baseline_env_sha256"])   # byte-exact baseline sha

    # (8b) GFX-168 — host-gated DORMANT path: the signed env is NEVER dispatched (executor unavailable). Nothing is
    #      mutated on the bridge, so the result must NOT fabricate a rollback failure. Reporting ``rollback_failed``
    #      here (as the pre-fix code did) is a false operator alarm implying a dirty bridge that was never touched.
    def test_undispatched_apply_is_not_reported_as_rollback_failure(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps(undispatched_for={a.id})
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)["results"][0]
        self.assertFalse(r["ok"])
        self.assertFalse(r["applied"])
        self.assertFalse(r["rolled_back"])
        self.assertIsNone(r["rollback_reason"])                      # nothing applied ⇒ nothing to roll back
        self.assertEqual(r["reason"], "apply_not_dispatched:host_executor_unavailable")
        # EXACTLY one dispatch attempt (the enable); no doomed second "rollback" dispatch against the dormant host
        self.assertEqual([x[2] for x in ops.applies], [True])

    # (8c) GFX-168 — the fix does NOT suppress a GENUINE rollback failure. The enable dispatches + verifies (the
    #      bridge IS mutated to read-only), the read-back then fails, and the baseline ROLLBACK dispatch attests
    #      badly: the operator MUST still see ``rollback_failed`` (the bridge may be left dirty).
    def test_genuine_rollback_failure_still_reported(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps(bad_readback_for={a.id}, bad_rollback_for={a.id})
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)["results"][0]
        self.assertFalse(r["ok"])
        self.assertFalse(r["applied"])
        self.assertFalse(r["rolled_back"])
        self.assertEqual(r["reason"], "read_back_failed")
        self.assertEqual(r["rollback_reason"], "rollback_failed")     # legitimate — NOT masked by the GFX-168 fix
        self.assertEqual([x[2] for x in ops.applies], [True, False])  # enable dispatched + a real rollback attempted

    # (8d) GFX-168 — a DISPATCHED-but-unverified attestation still fails closed to a rollback; if that rollback also
    #      attests badly it is a genuine ``rollback_failed`` (distinct from the never-dispatched case in 8b).
    def test_attestation_failure_with_failed_rollback_reported(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps(bad_attest_for={a.id}, bad_rollback_for={a.id})
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)["results"][0]
        self.assertFalse(r["ok"])
        self.assertTrue(r["reason"].startswith("attestation_failed"))
        self.assertFalse(r["rolled_back"])
        self.assertEqual(r["rollback_reason"], "rollback_failed")
        self.assertEqual([x[2] for x in ops.applies], [True, False])  # dispatched enable + attempted rollback

    # (8e) GFX-168 — a dormant-path failure leaves NO residue: a retry on a healthy host applies cleanly and
    #      idempotently (same desired env sha), proving the failed run neither mutated nor "half-applied" anything.
    def test_retry_after_undispatched_cleanly_applies(self):
        a, _ = self._mk(self.owner)
        with self._allow(a):
            r1 = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id,
                              ops=_FakeOps(undispatched_for={a.id}))["results"][0]
            r2 = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=_FakeOps())["results"][0]
        self.assertFalse(r1["ok"]); self.assertFalse(r1["applied"]); self.assertIsNone(r1["rollback_reason"])
        self.assertTrue(r2["ok"] and r2["applied"])
        self.assertEqual(r2["reason"], "readonly_enabled")
        self.assertEqual(r1["preview"]["desired_env_sha256"], r2["preview"]["desired_env_sha256"])  # idempotent target

    # (8f) stale/concurrent state — an active-execution authorization appears BETWEEN the initial gate eval and the
    #      pre-dispatch TOCTOU recheck: the signed dispatch is REFUSED (gate_failed_recheck), nothing is applied.
    def test_toctou_recheck_failure_dispatches_nothing(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps()
        with self._allow(a):
            with mock.patch.object(MR, "_account_has_active_execution", side_effect=[False, True]):
                r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)["results"][0]
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "gate_failed_recheck")
        self.assertIn("execution_off", r["failed_gates"])
        self.assertEqual(ops.applies, [])                             # nothing dispatched across the state change

    # (8g) transaction boundary — ``applied=True`` is reported ONLY on full success (dispatch + attestation +
    #      enable read-back). EVERY failure mode reports ``applied=False`` — there is no partial/"half" applied.
    def test_applied_true_only_on_full_success(self):
        a, _ = self._mk(self.owner)
        for ops in (_FakeOps(undispatched_for={a.id}), _FakeOps(bad_attest_for={a.id}),
                    _FakeOps(bad_readback_for={a.id})):
            with self._allow(a):
                r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)["results"][0]
            self.assertFalse(r["applied"], r["reason"])
            self.assertFalse(r["ok"], r["reason"])

    # (8h) observability/accounting consistency — a mixed batch (clean apply / dormant-never-dispatched /
    #      dispatched-then-dirty-rollback-failed / refused) yields truthful, internally-consistent counters with
    #      no double-count: ok+refused==total, applied counts only true applies, rolled_back counts only true rollbacks.
    def test_accounting_consistency_mixed_batch(self):
        good, _ = self._mk(self.owner)        # clean apply
        dormant, _ = self._mk(self.owner)     # host-gated: never dispatched → no rollback, no fabricated failure
        dirty, _ = self._mk(self.owner)       # dispatched+verified, read-back fails, rollback FAILS (bridge dirty)
        refused, _ = self._mk(self.owner)     # not on allow-list → refused before any dispatch
        ops = _FakeOps(undispatched_for={dormant.id}, bad_readback_for={dirty.id}, bad_rollback_for={dirty.id})
        with override_settings(
                LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST=" ".join(str(x.id) for x in (good, dormant, dirty))):
            rep = MR.reconcile([good.id, dormant.id, dirty.id, refused.id], commit=True,
                               expected_owner_id=self.owner.id, ops=ops)
        by = {r["account_id"]: r for r in rep["results"]}
        self.assertTrue(by[good.id]["ok"] and by[good.id]["applied"])
        self.assertIsNone(by[dormant.id]["rollback_reason"]); self.assertFalse(by[dormant.id]["rolled_back"])
        self.assertEqual(by[dirty.id]["rollback_reason"], "rollback_failed")
        self.assertFalse(by[refused.id]["ok"])
        s = rep["summary"]
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["ok"], 1)                                 # only `good`
        self.assertEqual(s["applied"], 1)                            # exactly the one applied
        self.assertEqual(s["refused"], 3)                            # dormant + dirty + refused
        self.assertEqual(s["rolled_back"], 0)                        # dormant: nothing to undo; dirty: rollback FAILED
        self.assertEqual(s["ok"] + s["refused"], s["total"])         # internally consistent — no double-count

    # (9) partial failure rolls back ONLY the affected account and reports truthfully
    def test_partial_failure_isolated(self):
        good, _ = self._mk(self.owner)
        bad, _ = self._mk(self.owner)
        ops = _FakeOps(bad_readback_for={bad.id})
        with self._allow(good, bad):
            rep = MR.reconcile([good.id, bad.id], commit=True, expected_owner_id=self.owner.id, ops=ops)
        by_id = {r["account_id"]: r for r in rep["results"]}
        self.assertTrue(by_id[good.id]["ok"] and by_id[good.id]["applied"])
        self.assertFalse(by_id[bad.id]["ok"])
        self.assertTrue(by_id[bad.id]["rolled_back"])
        self.assertEqual(by_id[bad.id]["reason"], "read_back_failed")
        self.assertEqual(rep["summary"], {**rep["summary"], "total": 2, "ok": 1, "applied": 1, "rolled_back": 1})
        # only the bad account was rolled back; the good account had exactly one enable apply, no rollback
        self.assertEqual([x for x in ops.applies if x[0] == good.id and x[2] is False], [])

    # (10) rerun is idempotent (same per-account outcome + env sha)
    def test_rerun_idempotent(self):
        a, _ = self._mk(self.owner)
        with self._allow(a):
            r1 = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=_FakeOps())["results"][0]
            r2 = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=_FakeOps())["results"][0]
        self.assertEqual(r1["preview"]["desired_env_sha256"], r2["preview"]["desired_env_sha256"])
        self.assertEqual((r1["ok"], r1["applied"], r1["reason"]), (r2["ok"], r2["applied"], r2["reason"]))

    # (11) read-back proves HTTP 200 positions capability (synthetic), no broker mutation
    def test_read_back_is_synthetic_200(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps()
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops)["results"][0]
        self.assertEqual(r["read_back"], {"http_status": 200, "ok": True, "reason": "synthetic"})

    # teardown removes ONLY the read flag (applies the baseline env)
    def test_teardown_applies_baseline(self):
        a, _ = self._mk(self.owner)
        ops = _FakeOps()
        with self._allow(a):
            r = MR.reconcile([a.id], commit=True, expected_owner_id=self.owner.id, ops=ops, teardown=True)["results"][0]
        self.assertTrue(r["ok"] and r["applied"])
        self.assertEqual(r["reason"], "readonly_removed")
        # the single apply carried the BASELINE (read flag absent)
        self.assertEqual([x[2] for x in ops.applies], [False])
        self.assertEqual(r["preview"]["desired_env_sha256"], r["preview"]["baseline_env_sha256"])


# (12) host-dispatch allow-list remains fail-closed for the read key (cited: tests_live_bridge_dispatch covers
#      the full posture matrix; a focused assertion that the rendered monitoring env only uses allow-listed keys).
class HostAllowlistFailClosed(SimpleTestCase):
    def test_monitoring_env_keys_are_all_host_allowlisted(self):
        from hosted_workspace.host_agent_dispatch import _BRIDGE_ENV_ALLOWED_KEYS
        with mock.patch.object(MR, "resolve_ready_endpoint", return_value=_ep()):
            _b, _s, mon = render_readonly_env(SimpleNamespace(id=48), readonly=True)
        for line in mon.replace("\r\n", "\n").split("\n"):
            s = line.strip()
            if not s or s.upper().startswith("REM "):
                continue
            key = s[4:].split("=", 1)[0].strip()               # "set KEY=val"
            self.assertIn(key, _BRIDGE_ENV_ALLOWED_KEYS, key)   # every emitted key is host-permitted
        self.assertIn("MT5_LIVE_READONLY_POSITIONS_ENABLED", _BRIDGE_ENV_ALLOWED_KEYS)
