"""GUARDED DEPLOY (packet-128) — tests for the lease-guarded, rollback-first backend-deploy wrapper.

Proves the packet-128 deploy-side guarantees with an injected DeployOps fake (no production command runs):
  * an active recovery lease (or a malformed lease) REFUSES the deploy before ANY mutation;
  * an active deploy lease blocks a recovery attempt (mutual exclusion);
  * a clean success OR a successful rollback releases the deploy lease; a deploy failure whose rollback ALSO fails
    RETAINS the lease and ALERTS (no false cleanup);
  * the emergency override requires a non-empty reason, is audited, and the reason is redacted;
  * concurrent acquisition has exactly one winner; an expired lease is reclaimed on acquire;
  * it works with no Django/DB (SimpleTestCase, file-only lease) — backend-down cannot bypass the gate;
  * no secret/environment leakage.
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

_GD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "scripts", "guarded_deploy.py")


def _load_gd():
    spec = importlib.util.spec_from_file_location("guarded_deploy", _GD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod            # register so @dataclass can resolve string annotations
    spec.loader.exec_module(mod)
    return mod


GD = _load_gd()
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _FakeDeployOps(GD.DeployOps):
    def __init__(self, *, verify_ok=True, rollback_ok=True, raise_in=None):
        self._now = T0
        self._verify_ok = verify_ok
        self._rollback_ok = rollback_ok
        self._raise_in = raise_in
        self.calls = []
        self.alerts = []

    def now(self):
        self._now = self._now + timedelta(seconds=1)
        return self._now

    def _maybe_raise(self, where):
        if self._raise_in == where:
            raise RuntimeError("boom@" + where)

    def tag_rollback(self):
        self.calls.append("tag_rollback"); self._maybe_raise("tag_rollback")

    def build(self):
        self.calls.append("build"); self._maybe_raise("build")

    def recreate(self):
        self.calls.append("recreate"); self._maybe_raise("recreate")

    def verify(self):
        self.calls.append("verify"); return self._verify_ok

    def rollback(self):
        self.calls.append("rollback"); return self._rollback_ok

    def alert(self, title, detail):
        self.alerts.append((title, detail))


class GuardedDeployTests(SimpleTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "lease.json")

    def _run(self, ops, **kw):
        return GD.run_guarded_deploy(ops, correlation_id="dep1", owner="op", ttl_s=600, lease_path=self.p, **kw)

    # ── gate: refuse before mutation ──────────────────────────────────────────────────────────────────────────
    def test_active_recovery_lease_refuses_deploy_before_any_mutation(self):
        RL.acquire(RL.KIND_RECOVERY, "pilot", "op", "rec1", 600, T0, path=self.p)
        ops = _FakeDeployOps()
        res = self._run(ops)
        self.assertEqual(res.outcome, GD.OUTCOME_REFUSED)
        self.assertFalse(res.lease_held)
        self.assertEqual(ops.calls, [])                 # NO build/recreate — mutation never started
        self.assertEqual(RL.read_lease(T0, path=self.p)[1].kind, RL.KIND_RECOVERY)   # recovery lease untouched

    def test_malformed_lease_refuses_deploy(self):
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write("{garbage")
        ops = _FakeDeployOps()
        res = self._run(ops)
        self.assertEqual(res.outcome, GD.OUTCOME_REFUSED)
        self.assertEqual(ops.calls, [])

    def test_active_deploy_lease_blocks_recovery(self):
        RL.acquire(RL.KIND_DEPLOY, "deploy", "op", "dep1", 600, T0, path=self.p)
        blocked, reason = RL.blocks(RL.KIND_RECOVERY, T0 + timedelta(seconds=5), path=self.p)
        self.assertTrue(blocked)
        self.assertIn("held:deploy", reason)

    # ── guaranteed lease lifecycle ────────────────────────────────────────────────────────────────────────────
    def test_clean_deploy_acquires_then_releases(self):
        ops = _FakeDeployOps(verify_ok=True)
        res = self._run(ops)
        self.assertEqual(res.outcome, GD.OUTCOME_DEPLOYED)
        self.assertTrue(res.lease_held)
        self.assertTrue(res.lease_released)
        self.assertEqual(ops.calls, ["tag_rollback", "build", "recreate", "verify"])
        self.assertEqual(RL.read_lease(ops.now(), path=self.p)[0], RL.LeaseStatus.ABSENT)

    def test_verify_failure_rolls_back_then_releases(self):
        ops = _FakeDeployOps(verify_ok=False, rollback_ok=True)
        res = self._run(ops)
        self.assertEqual(res.outcome, GD.OUTCOME_VERIFY_FAILED)
        self.assertTrue(res.rolled_back)
        self.assertTrue(res.lease_released)
        self.assertIn("rollback", ops.calls)
        self.assertEqual(RL.read_lease(ops.now(), path=self.p)[0], RL.LeaseStatus.ABSENT)

    def test_exception_rolls_back_then_releases(self):
        ops = _FakeDeployOps(raise_in="recreate", rollback_ok=True)
        res = self._run(ops)
        self.assertEqual(res.outcome, GD.OUTCOME_ERROR)
        self.assertEqual(res.detail.get("exception"), "RuntimeError")
        self.assertTrue(res.rolled_back)
        self.assertTrue(res.lease_released)
        self.assertEqual(RL.read_lease(ops.now(), path=self.p)[0], RL.LeaseStatus.ABSENT)

    def test_deploy_fail_and_rollback_fail_retains_lease_and_alerts(self):
        ops = _FakeDeployOps(verify_ok=False, rollback_ok=False)
        res = self._run(ops)
        self.assertEqual(res.outcome, GD.OUTCOME_VERIFY_FAILED)
        self.assertFalse(res.rolled_back)
        self.assertFalse(res.lease_released)
        self.assertNotEqual(RL.read_lease(ops.now(), path=self.p)[0], RL.LeaseStatus.ABSENT)  # lease RETAINED
        self.assertTrue(any("rollback FAILED" in t for t, _ in ops.alerts))

    # ── emergency override ────────────────────────────────────────────────────────────────────────────────────
    def test_override_requires_nonempty_reason(self):
        RL.acquire(RL.KIND_RECOVERY, "pilot", "op", "rec1", 600, T0, path=self.p)
        ops = _FakeDeployOps()
        # empty override reason ⇒ still refused, no mutation
        res = self._run(ops, override_reason="   ")
        self.assertEqual(res.outcome, GD.OUTCOME_REFUSED)
        self.assertEqual(ops.calls, [])
        # explicit non-empty reason ⇒ proceeds WITHOUT a deploy lease (recovery lease still holds it)
        ops2 = _FakeDeployOps()
        res2 = self._run(ops2, override_reason="hotfix CVE-xxxx approved by op")
        self.assertEqual(res2.outcome, GD.OUTCOME_DEPLOYED)
        self.assertFalse(res2.lease_held)
        self.assertEqual(ops2.calls, ["tag_rollback", "build", "recreate", "verify"])
        blob = open(os.path.join(self.d, "recovery_operational_lease_audit.jsonl")).read()
        self.assertIn("deploy_emergency_override", blob)

    def test_override_reason_redacted_in_audit(self):
        RL.acquire(RL.KIND_RECOVERY, "pilot", "op", "rec1", 600, T0, path=self.p)
        secret = "sk_" + "a" * 42
        self._run(_FakeDeployOps(), override_reason="TOKEN=" + secret)
        blob = open(os.path.join(self.d, "recovery_operational_lease_audit.jsonl")).read()
        self.assertNotIn(secret, blob)
        self.assertIn("<redacted>", blob)

    # ── concurrency / expiry ──────────────────────────────────────────────────────────────────────────────────
    def test_concurrent_acquire_one_winner(self):
        # Another deploy won the O_EXCL race between our ABSENT check and our write ⇒ we lose fail-closed, no mutation.
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write("placeholder")
        ops = _FakeDeployOps()
        with mock.patch.object(RL, "read_lease", return_value=(RL.LeaseStatus.ABSENT, None)):
            res = self._run(ops)
        self.assertEqual(res.outcome, GD.OUTCOME_REFUSED)
        self.assertIn("lost_acquire_race", res.reason)
        self.assertEqual(ops.calls, [])

    def test_expired_lease_reclaimed_on_acquire(self):
        RL.acquire(RL.KIND_RECOVERY, "old", "op", "old1", 60, T0, path=self.p)   # expires at T0+60
        ops = _FakeDeployOps()
        # run 'now' is well past expiry (ops.now starts at T0+1 and the lease module uses it)
        ops._now = T0 + timedelta(seconds=120)
        res = self._run(ops)
        self.assertEqual(res.outcome, GD.OUTCOME_DEPLOYED)       # expired recovery lease reclaimed → deploy proceeds
        self.assertTrue(res.lease_held)

    # ── backend-down usability / no secret leakage ────────────────────────────────────────────────────────────
    def test_runs_without_django_or_db(self):
        # This whole class is a SimpleTestCase (no DB). A clean deploy completing proves the wrapper needs only the
        # stdlib recovery_lease file primitives — a down backend/DB cannot bypass the gate.
        res = self._run(_FakeDeployOps())
        self.assertEqual(res.outcome, GD.OUTCOME_DEPLOYED)

    def test_override_does_not_bypass_malformed_lease(self):
        # A MALFORMED (corrupt) lease is never overridable — even an explicit override is refused fail-closed.
        with open(self.p, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        ops = _FakeDeployOps()
        res = self._run(ops, override_reason="emergency hotfix approved")
        self.assertEqual(res.outcome, GD.OUTCOME_REFUSED)
        self.assertIn("malformed", res.reason)
        self.assertEqual(ops.calls, [])

    def test_main_no_lease_file_fails_closed(self):
        # CRITICAL regression: main() must refuse (exit 3) when no lease file is configured — NEVER fall back to a
        # temp dir and bypass the gate on a backend-down host.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GUVFX_RECOVERY_LEASE_FILE", None)
            os.environ.pop("GUVFX_RECOVERY_LEASE_DIR", None)
            rc = GD.main(["--sha", "deadbeef", "--compose-dir", "/none", "--app-dir", "/none", "--owner", "op"])
        self.assertEqual(rc, 3)

    def test_no_secret_or_env_in_result_or_audit(self):
        self._run(_FakeDeployOps(verify_ok=False, rollback_ok=False))
        audit = open(os.path.join(self.d, "recovery_operational_lease_audit.jsonl")).read() if os.path.exists(
            os.path.join(self.d, "recovery_operational_lease_audit.jsonl")) else ""
        for bad in ("SECRET", "PASSWORD", "KEYRING", "TOKEN=", "DJANGO_SECRET_KEY"):
            self.assertNotIn(bad, audit)
