"""GUARDED COORDINATED DEPLOY (GFX-167) — tests for the ONE-lease, rollback-first, backend+frontend deploy wrapper.

Proves the GFX-167 contract with an injected CoordinatedDeployOps fake (no production command runs):
  * exactly ONE KIND_DEPLOY lease is acquired before any mutation and held CONTINUOUSLY across both phases;
  * a live recovery / live same-kind deploy / malformed lease REFUSES before any mutation (no override, no nested bypass);
  * rollback-FIRST: both images tagged before any build/recreate; a failure at ANY step rolls BOTH services back and
    re-verifies both, then releases; a rollback that cannot restore a healthy baseline RETAINS the lease and ALERTS;
  * structural guarantees: single-service --no-deps --force-recreate, impossible --remove-orphans, no capability flag,
    required frontend API-base, exact-SHA pinning;
  * --dry-run is mutation-free; output carries no secret/env; it runs with no Django/DB (SimpleTestCase, file-only lease).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import SimpleTestCase

from hosted_workspace import recovery_lease as RL

_GCD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "scripts", "guarded_coordinated_deploy.py")


def _load_gcd():
    spec = importlib.util.spec_from_file_location("guarded_coordinated_deploy", _GCD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod            # register so @dataclass can resolve string annotations
    spec.loader.exec_module(mod)
    return mod


GCD = _load_gcd()
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_MUTATION_STEPS = ("build_backend", "build_frontend", "recreate_backend", "recreate_frontend")


class _FakeCoordOps(GCD.CoordinatedDeployOps):
    def __init__(self, *, lease_path, correlation="dep1",
                 verify_backend_ok=True, verify_frontend_ok=True, inventory_ok=True,
                 restore_backend_ok=True, restore_frontend_ok=True,
                 restored_backend_healthy=True, restored_frontend_healthy=True,
                 preflight_raises=None, raise_in=None, interrupt_in=None):
        self._now = T0
        self.lease_path = lease_path
        self.correlation = correlation
        self._vb = verify_backend_ok
        self._vf = verify_frontend_ok
        self._inv = inventory_ok
        self._rb = restore_backend_ok
        self._rf = restore_frontend_ok
        self._rhb = restored_backend_healthy
        self._rhf = restored_frontend_healthy
        self._preflight_raises = preflight_raises
        self._raise_in = raise_in
        self._interrupt_in = interrupt_in
        self.calls = []
        self.alerts = []
        self.lease_during = {}

    def now(self):
        self._now = self._now + timedelta(seconds=1)
        return self._now

    def _record_lease(self, step):
        status, lease = RL.read_lease(self._now, path=self.lease_path)
        self.lease_during[step] = (status, getattr(lease, "correlation_id", None))

    def _maybe_raise(self, where):
        if self._raise_in == where:
            raise RuntimeError("boom@" + where)
        if self._interrupt_in == where:
            raise KeyboardInterrupt("interrupt@" + where)

    def preflight(self):
        self.calls.append("preflight")
        if self._preflight_raises:
            raise GCD.PreflightError(self._preflight_raises)

    def snapshot_inventory(self):
        self.calls.append("snapshot_inventory"); self._maybe_raise("snapshot_inventory")
        return {"guvfx-backend": "running", "guvfx-frontend": "running", "traefik": "running",
                "guvfx-wayond-listener": "running"}

    def tag_rollback_backend(self):
        self.calls.append("tag_rollback_backend"); self._record_lease("tag_rollback_backend")
        self._maybe_raise("tag_rollback_backend")

    def tag_rollback_frontend(self):
        self.calls.append("tag_rollback_frontend"); self._maybe_raise("tag_rollback_frontend")

    def build_backend(self):
        self.calls.append("build_backend"); self._record_lease("build_backend"); self._maybe_raise("build_backend")

    def build_frontend(self):
        self.calls.append("build_frontend"); self._record_lease("build_frontend"); self._maybe_raise("build_frontend")

    def recreate_backend(self):
        self.calls.append("recreate_backend"); self._record_lease("recreate_backend")
        self._maybe_raise("recreate_backend")

    def recreate_frontend(self):
        self.calls.append("recreate_frontend"); self._record_lease("recreate_frontend")
        self._maybe_raise("recreate_frontend")

    def verify_backend(self):
        # DEPLOY-phase verify (target identity + health). Distinct from the restored-baseline verify below.
        self.calls.append("verify_backend")
        return self._vb

    def verify_frontend(self):
        self.calls.append("verify_frontend")
        return self._vf

    def verify_restored_backend(self):
        # ROLLBACK-phase verify: the restored BASELINE image (prior commit) is healthy. Modelled as a SEPARATE signal
        # from the deploy-phase verify, exactly as the real ops implement it (baseline image id, never the target sha).
        self.calls.append("verify_restored_backend")
        return self._rhb

    def verify_restored_frontend(self):
        self.calls.append("verify_restored_frontend")
        return self._rhf

    def verify_inventory_unchanged(self, before):
        self.calls.append("verify_inventory_unchanged")
        return self._inv

    def restore_backend(self):
        self.calls.append("restore_backend"); self._maybe_raise("restore_backend")
        return self._rb

    def restore_frontend(self):
        self.calls.append("restore_frontend"); self._maybe_raise("restore_frontend")
        return self._rf

    def alert(self, title, detail):
        self.alerts.append((title, detail))


class GuardedCoordinatedDeployTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "lease.json")

    def _run(self, ops, **kw):
        return GCD.run_guarded_coordinated_deploy(ops, correlation_id="dep1", owner="op", ttl_s=600,
                                                  lease_path=self.p, **kw)

    def _lease_status(self, t=None):
        return RL.read_lease(t or (T0 + timedelta(seconds=300)), path=self.p)[0]

    # ── one lease across both phases → clean success → release ────────────────────────────────────────────────────
    def test_clean_deploy_holds_one_lease_across_both_phases_then_releases(self):
        ops = _FakeCoordOps(lease_path=self.p)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_DEPLOYED)
        self.assertTrue(res.lease_held)
        self.assertTrue(res.lease_released)
        self.assertEqual(ops.calls, [
            "preflight", "snapshot_inventory", "tag_rollback_backend", "tag_rollback_frontend",
            "build_backend", "build_frontend", "recreate_backend", "recreate_frontend",
            "verify_backend", "verify_frontend", "verify_inventory_unchanged"])
        # The SAME lease (our correlation) was ACTIVE across EVERY mutating step of BOTH phases — one continuous window.
        for step in _MUTATION_STEPS:
            self.assertEqual(ops.lease_during[step], (RL.LeaseStatus.ACTIVE, "dep1"),
                             "lease not continuously held at %s" % step)
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ABSENT)   # released after success

    # ── gate: refuse before ANY mutation (no override, no nested bypass) ──────────────────────────────────────────
    def test_active_recovery_lease_refuses_before_any_mutation(self):
        RL.acquire(RL.KIND_RECOVERY, "pilot", "op", "rec1", 600, T0, path=self.p)
        ops = _FakeCoordOps(lease_path=self.p)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_REFUSED)
        self.assertFalse(res.lease_held)
        self.assertEqual(ops.calls, [])                         # preflight never even ran
        self.assertEqual(RL.read_lease(T0, path=self.p)[1].kind, RL.KIND_RECOVERY)  # recovery lease untouched

    def test_active_same_kind_deploy_lease_refuses_no_nested_bypass(self):
        RL.acquire(RL.KIND_DEPLOY, "other-deploy", "op", "otherdep", 600, T0, path=self.p)
        ops = _FakeCoordOps(lease_path=self.p)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_REFUSED)
        self.assertIn("held:deploy", res.reason)
        self.assertEqual(ops.calls, [])
        self.assertEqual(RL.read_lease(T0, path=self.p)[1].correlation_id, "otherdep")  # not clobbered

    def test_malformed_lease_refuses_before_mutation(self):
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write("{garbage")
        ops = _FakeCoordOps(lease_path=self.p)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_REFUSED)
        self.assertEqual(ops.calls, [])

    # ── preflight fail-closed AFTER acquire, BEFORE mutation → release lease (no rollback) ─────────────────────────
    def test_preflight_dirty_source_refuses_releases_lease_no_mutation(self):
        ops = _FakeCoordOps(lease_path=self.p, preflight_raises="dirty_or_untrusted_source_tree")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_REFUSED)
        self.assertIn("dirty", res.detail.get("preflight", ""))
        self.assertEqual(ops.calls, ["preflight"])              # no tag/build/recreate
        self.assertTrue(res.lease_held)
        self.assertTrue(res.lease_released)                     # lease released: nothing was mutated
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ABSENT)

    def test_preflight_sha_mismatch_fails_closed(self):
        ops = _FakeCoordOps(lease_path=self.p, preflight_raises="checkout_mismatch")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_REFUSED)
        self.assertEqual(ops.calls, ["preflight"])

    # ── rollback-first: failure at any step rolls BOTH back, then releases ────────────────────────────────────────
    def test_backend_build_failure_rolls_back_both_then_releases(self):
        ops = _FakeCoordOps(lease_path=self.p, raise_in="build_backend")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertTrue(res.rolled_back)
        self.assertTrue(res.lease_released)
        self.assertNotIn("build_frontend", ops.calls)           # failed before the frontend phase
        self.assertIn("restore_backend", ops.calls)
        self.assertIn("restore_frontend", ops.calls)            # COMBINED rollback restores BOTH
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ABSENT)

    def test_frontend_build_failure_after_backend_build_rolls_back_both(self):
        ops = _FakeCoordOps(lease_path=self.p, raise_in="build_frontend")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertIn("build_backend", ops.calls)
        self.assertNotIn("recreate_backend", ops.calls)         # no recreate happened yet
        self.assertIn("restore_backend", ops.calls)
        self.assertIn("restore_frontend", ops.calls)

    def test_frontend_recreate_failure_after_backend_success_rolls_back_both(self):
        # The critical case: backend already recreated with the NEW image; frontend recreate fails → BOTH restored.
        ops = _FakeCoordOps(lease_path=self.p, raise_in="recreate_frontend")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertIn("recreate_backend", ops.calls)
        self.assertIn("restore_backend", ops.calls)             # the already-recreated backend is genuinely reverted
        self.assertIn("restore_frontend", ops.calls)
        self.assertTrue(res.lease_released)

    def test_backend_verify_failure_rolls_back_both(self):
        ops = _FakeCoordOps(lease_path=self.p, verify_backend_ok=False)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertFalse(res.detail["verify"]["backend"])
        self.assertIn("restore_backend", ops.calls)
        self.assertIn("restore_frontend", ops.calls)

    def test_frontend_verify_failure_rolls_back_both(self):
        ops = _FakeCoordOps(lease_path=self.p, verify_frontend_ok=False)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertFalse(res.detail["verify"]["frontend"])

    def test_unexpected_inventory_change_rolls_back(self):
        ops = _FakeCoordOps(lease_path=self.p, inventory_ok=False)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertFalse(res.detail["verify"]["inventory"])

    def test_exception_in_recreate_rolls_back_both(self):
        ops = _FakeCoordOps(lease_path=self.p, raise_in="recreate_backend")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertEqual(res.detail.get("exception"), "RuntimeError")
        self.assertTrue(res.lease_released)

    def test_interrupt_path_rolls_back_both(self):
        ops = _FakeCoordOps(lease_path=self.p, interrupt_in="recreate_frontend")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertEqual(res.detail.get("exception"), "KeyboardInterrupt")
        self.assertTrue(res.lease_released)

    # ── rollback cannot restore → RETAIN lease fail-closed + alert (contract §10) ─────────────────────────────────
    def test_rollback_failure_retains_lease_and_alerts(self):
        ops = _FakeCoordOps(lease_path=self.p, verify_backend_ok=False, restore_backend_ok=False)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_INDETERMINATE)
        self.assertFalse(res.rolled_back)
        self.assertFalse(res.lease_released)
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ACTIVE)   # lease RETAINED
        titles = " ".join(t for t, _ in ops.alerts)
        self.assertIn("INDETERMINATE", titles)
        self.assertIn("RETAINED", titles)
        # the alert carries exact operator recovery steps
        self.assertTrue(any("recovery" in d for _, d in ops.alerts))

    def test_restored_baseline_unhealthy_retains_lease(self):
        # restore ops succeed but the restored baseline does not verify healthy → still fail-closed retain.
        ops = _FakeCoordOps(lease_path=self.p, verify_backend_ok=False,
                            restored_backend_healthy=False)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_INDETERMINATE)
        self.assertFalse(res.lease_released)
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ACTIVE)

    def test_rollback_restore_exception_retains_lease(self):
        ops = _FakeCoordOps(lease_path=self.p, verify_frontend_ok=False, raise_in="restore_frontend")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_INDETERMINATE)
        self.assertEqual(res.detail.get("rollback_exceptions", {}).get("frontend"), "RuntimeError")
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ACTIVE)

    # ── rollback verifies the BASELINE (not the target) + pre-mutation error releases + independent restores ───────
    def test_rollback_confirmed_via_baseline_verify_not_target_verify(self):
        # Regression for the CRITICAL: a rollback is confirmed against the restored BASELINE (verify_restored_*),
        # NOT the target-sha deploy oracle. Deploy-verify fails (triggers rollback); restored-baseline verify passes
        # -> ROLLED_BACK + lease released (the real bug was INDETERMINATE + leaked lease on every real rollback).
        ops = _FakeCoordOps(lease_path=self.p, verify_backend_ok=False,
                            restored_backend_healthy=True, restored_frontend_healthy=True)
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ROLLED_BACK)
        self.assertTrue(res.rolled_back)
        self.assertTrue(res.lease_released)
        self.assertIn("verify_restored_backend", ops.calls)       # baseline oracle, not the target verify
        self.assertIn("verify_restored_frontend", ops.calls)
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ABSENT)

    def test_pre_mutation_snapshot_error_releases_lease_not_false_indeterminate(self):
        # Regression for the HIGH: a non-PreflightError BEFORE any build/recreate (e.g. a docker-ps hiccup) must
        # RELEASE the lease (production untouched), never a false INDETERMINATE + retained lease.
        ops = _FakeCoordOps(lease_path=self.p, raise_in="snapshot_inventory")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ERROR)
        self.assertEqual(res.detail.get("exception"), "RuntimeError")
        self.assertTrue(res.lease_released)
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ABSENT)
        self.assertNotIn("build_backend", ops.calls)              # nothing was built/recreated
        self.assertFalse(any("INDETERMINATE" in t for t, _ in ops.alerts))

    def test_pre_mutation_tag_rollback_error_releases_lease(self):
        ops = _FakeCoordOps(lease_path=self.p, raise_in="tag_rollback_backend")
        res = self._run(ops)
        self.assertEqual(res.outcome, GCD.OUTCOME_ERROR)
        self.assertTrue(res.lease_released)                       # only a harmless image tag was (partly) created
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ABSENT)
        self.assertNotIn("build_backend", ops.calls)

    def test_restore_frontend_attempted_even_if_restore_backend_raises(self):
        # Regression for M2: the two restores are independent; restore_frontend runs even if restore_backend raises.
        ops = _FakeCoordOps(lease_path=self.p, verify_backend_ok=False, raise_in="restore_backend")
        res = self._run(ops)
        self.assertIn("restore_frontend", ops.calls)              # attempted despite backend restore raising
        self.assertEqual(res.outcome, GCD.OUTCOME_INDETERMINATE)  # backend restore failed -> fail-closed retain
        self.assertEqual(res.detail.get("rollback_exceptions", {}).get("backend"), "RuntimeError")
        self.assertEqual(self._lease_status(), RL.LeaseStatus.ACTIVE)

    def test_inventory_unchanged_helper(self):
        # M4: a vanished/state-changed PRIOR container fails closed; a NEW transient container is benign.
        before = {"guvfx-backend": "running", "guvfx-frontend": "running", "traefik": "running"}
        self.assertTrue(GCD.inventory_unchanged(before, dict(before)))
        self.assertTrue(GCD.inventory_unchanged(before, {**before, "guvfx-fx-ingest-run-abc": "running"}))  # transient
        self.assertFalse(GCD.inventory_unchanged(before, {"guvfx-backend": "running", "guvfx-frontend": "running"}))  # vanished
        self.assertFalse(GCD.inventory_unchanged(before, {**before, "traefik": "restarting"}))  # state changed

    # ── structural guarantees (pure command builders — no docker) ─────────────────────────────────────────────────
    def test_recreate_is_single_service_no_deps_force_recreate(self):
        be = GCD.recreate_cmd("/compose", GCD.BACKEND_SERVICE)
        fe = GCD.recreate_cmd("/compose", GCD.FRONTEND_SERVICE)
        self.assertEqual(be[-3:], ["--no-deps", "--force-recreate", "guvfx-backend"])
        self.assertEqual(fe[-3:], ["--no-deps", "--force-recreate", "guvfx-frontend"])
        with self.assertRaises(ValueError):
            GCD.recreate_cmd("/compose", "guvfx-postgres")      # refuses any non-target service

    def test_remove_orphans_is_impossible(self):
        cmds = [GCD.recreate_cmd("/c", GCD.BACKEND_SERVICE), GCD.recreate_cmd("/c", GCD.FRONTEND_SERVICE),
                GCD.backend_build_cmd("/app", "abc123", "ts", "pre"),
                GCD.frontend_build_cmd("/app", "abc123", "ts", "https://api.guvfx.com")]
        for cmd in cmds:
            self.assertNotIn("--remove-orphans", cmd)

    def test_no_capability_flags_in_frontend_build(self):
        cmd = GCD.frontend_build_cmd("/app", "abc123", "ts", "https://api.guvfx.com")
        self.assertFalse(any(GCD._CAP_FLAG_RE.search(tok) for tok in cmd),
                         "frontend build must carry NO NEXT_PUBLIC_*_ENABLED flag")
        self.assertIn("--build-arg", cmd)
        self.assertIn("NEXT_PUBLIC_API_BASE_URL=https://api.guvfx.com", cmd)

    def test_frontend_build_requires_api_base(self):
        with self.assertRaises(ValueError):
            GCD.frontend_build_cmd("/app", "abc123", "ts", "")
        with self.assertRaises(ValueError):
            GCD.frontend_build_cmd("/app", "abc123", "ts", "   ")

    def test_exact_sha_pinning_in_builds(self):
        sha = "f047cf5e6f46dcc2872f04454a13f586b18b08d6"
        be = GCD.backend_build_cmd("/app", sha, "ts", "pre")
        fe = GCD.frontend_build_cmd("/app", sha, "ts", "https://api.guvfx.com")
        self.assertIn("GIT_COMMIT=%s" % sha, be)
        self.assertIn("org.opencontainers.image.revision=%s" % sha, be)
        self.assertIn("GIT_COMMIT=%s" % sha, fe)
        self.assertIn("org.guvfx.git_commit=%s" % sha, fe)

    # ── CLI: dry-run mutation-free; fail-closed; sanitized ────────────────────────────────────────────────────────
    def test_dry_run_is_mutation_free(self):
        with mock.patch.object(GCD.subprocess, "run",
                               side_effect=AssertionError("dry-run must not shell out")), \
             mock.patch.object(RL, "acquire", side_effect=AssertionError("dry-run must not acquire a lease")):
            rc = GCD.main(["--dry-run", "--sha", "f047cf5e6f46dcc2872f04454a13f586b18b08d6",
                           "--compose-dir", "/none", "--app-dir", "/none", "--owner", "op", "--lease-file", self.p])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.p))                # no lease file created

    def test_main_no_lease_file_fails_closed(self):
        rc = GCD.main(["--sha", "deadbeef", "--compose-dir", "/none", "--app-dir", "/none",
                       "--owner", "op", "--lease-file", ""])
        self.assertEqual(rc, 3)

    def test_main_empty_frontend_api_base_fails_closed(self):
        rc = GCD.main(["--sha", "deadbeef", "--compose-dir", "/none", "--app-dir", "/none",
                       "--owner", "op", "--lease-file", self.p, "--frontend-api-base", ""])
        self.assertEqual(rc, 3)
        self.assertFalse(os.path.exists(self.p))

    def test_real_preflight_probe_failure_raises_preflighterror(self):
        # M1 regression on the REAL ops path: a failing git-status / compose-ps probe must raise PreflightError (a
        # clean, lease-releasing refuse), never escape as a bare Exception into the retain-the-lease branch.
        import subprocess as _sub

        def _git_status_fails(cmd, capture=False):
            class _R:
                stdout = "abc"                 # fetch/checkout/rev-parse all return the target sha
            if "status" in cmd:                # the dirty-tree probe fails transiently
                raise _sub.CalledProcessError(1, cmd)
            return _R()

        ops = GCD._RealCoordinatedDeployOps(sha="abc", compose_dir="/none", app_dir="/none",
                                            frontend_api_base="https://api.guvfx.com")
        with mock.patch.object(GCD, "_run", side_effect=_git_status_fails):
            with self.assertRaises(GCD.PreflightError):
                ops.preflight()

        def _compose_ps_fails(cmd, capture=False):
            class _R:
                stdout = "abc" if ("rev-parse" in cmd) else ("guvfx-backend guvfx-frontend" if "config" in cmd else "")
            if "ps" in cmd:                    # the per-service running-container probe fails
                raise _sub.CalledProcessError(1, cmd)
            return _R()

        ops2 = GCD._RealCoordinatedDeployOps(sha="abc", compose_dir="/none", app_dir="/none",
                                             frontend_api_base="https://api.guvfx.com")
        with mock.patch.object(GCD, "_run", side_effect=_compose_ps_fails):
            with self.assertRaises(GCD.PreflightError):
                ops2.preflight()

    def test_runs_without_django_or_db(self):
        # This whole class is a SimpleTestCase (no DB). A clean coordinated deploy completing proves the wrapper needs
        # only the stdlib recovery_lease file primitives — a down backend/DB cannot bypass the gate.
        res = self._run(_FakeCoordOps(lease_path=self.p))
        self.assertEqual(res.outcome, GCD.OUTCOME_DEPLOYED)

    def test_no_secret_or_env_in_result_or_audit_or_alerts(self):
        ops = _FakeCoordOps(lease_path=self.p, verify_backend_ok=False, restore_backend_ok=False)
        res = self._run(ops)
        blob = json.dumps(res.detail) + json.dumps(ops.alerts, default=str)
        audit_f = os.path.join(self.d, "recovery_operational_lease_audit.jsonl")
        if os.path.exists(audit_f):
            blob += open(audit_f, encoding="utf-8").read()
        for bad in ("SECRET", "PASSWORD", "KEYRING", "TOKEN=", "DJANGO_SECRET_KEY", "DB_PASSWORD"):
            self.assertNotIn(bad, blob)

    def test_exception_message_secret_not_leaked_only_class_name(self):
        # M6 (strengthened): a subprocess/exception whose MESSAGE embeds a secret-shaped value must never reach the
        # result/detail/alert -- only the class name is recorded (the code uses type(exc).__name__, never str(exc)).
        secret = "TOKEN=" + "z" * 44
        test = self

        class _SecretExcOps(_FakeCoordOps):
            def recreate_backend(self):
                self.calls.append("recreate_backend")
                raise RuntimeError("docker daemon error leaking " + secret)

        ops = _SecretExcOps(lease_path=self.p)
        res = test._run(ops)
        blob = json.dumps(res.detail) + json.dumps(ops.alerts, default=str)
        audit_f = os.path.join(self.d, "recovery_operational_lease_audit.jsonl")
        if os.path.exists(audit_f):
            blob += open(audit_f, encoding="utf-8").read()
        self.assertNotIn(secret, blob)                       # the secret-bearing message never appears
        self.assertNotIn("zzzz", blob)
        self.assertEqual(res.detail.get("exception"), "RuntimeError")   # only the class name is kept

    def test_secret_bearing_owner_is_refused_by_lease(self):
        # M6 (strengthened): a secret-shaped owner/correlation is rejected by the lease's secret guard BEFORE any
        # mutation, and no lease is written.
        ops = _FakeCoordOps(lease_path=self.p)
        res = GCD.run_guarded_coordinated_deploy(ops, correlation_id="dep1", owner="PASSWORD=" + "a" * 44,
                                                 ttl_s=600, lease_path=self.p)
        self.assertEqual(res.outcome, GCD.OUTCOME_REFUSED)
        self.assertIn("secret_bearing_field_rejected", res.reason)
        self.assertEqual(ops.calls, [])
        self.assertFalse(os.path.exists(self.p))             # nothing written
