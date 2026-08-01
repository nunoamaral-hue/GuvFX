"""ADR-0024 — governed orphan-reclaim (Phase 1) + failed-runtime recovery (Phase 2) tooling.

Covers: the RELEASE client + occupancy probe; the recovery helpers/guards; the reclaim command
(dry-run default, fail-closed guards, STABLE job_id, STOP->TOMBSTONE->RELEASE via PR#252 _step,
already-released idempotency, fail-closed quarantine); the recover command (exactly-one job, idempotent,
require-REMOVED, DARK preserved). No agent, no host, no order.
"""
from contextlib import contextmanager
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from billing.models import BetaTester
from trading.models import TradingAccount
from trading.crypto import encrypt_password
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning import beta_worker
from terminal_provisioning import provisioner as prov
from terminal_provisioning import recovery
from terminal_provisioning.mgmt_client import (
    AgentWindowsProvisioner, ManagementChannelError, ManagementChannelTimeout)
from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState
from terminal_provisioning.runtime_state import record_transition
from terminal_provisioning.tests_mgmt_channel import KEYRING

U = get_user_model()
CZ_UUID = "66972e0e-c803-49b5-8da2-a5df1c14e90d"


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────────────────
def _beta_runtime(n=1, state=RuntimeState.FAILED, uuid=None):
    email = f"r{n}@example.invalid"
    BetaTester.objects.create(email=email)
    u = U.objects.create_user(username=f"r{n}", email=email, password="x")
    acct = TradingAccount.objects.create(user=u, name=f"A{n}", account_number=str(910000 + n),
                                         broker_name="DemoBroker", is_demo=True,
                                         password_enc=encrypt_password("pw"))
    rt = cap.get_or_create_beta_runtime(acct)      # BETA, canonical root
    if uuid:
        AccountRuntime.objects.filter(pk=rt.pk).update(runtime_uuid=uuid)
        rt.refresh_from_db()
    if rt.state != state:
        record_transition(rt, state, reason_code="setup")
        rt.refresh_from_db()
    return rt


def _fake_transport(script=None, ops=("MATERIALISE", "START", "VERIFY", "STOP", "TOMBSTONE", "RELEASE")):
    """A scripted agent transport. ``script[op]`` may be a dict (returned) or a callable (may raise)."""
    script = script or {}

    def transport(base, req):
        op = req.get("operation")
        if op == "NEGOTIATE":
            return {"outcome": "ok", "operation": "NEGOTIATE", "protocol_version": 1,
                    "agent_version": "beta-agent-1.0.0", "manifest_version": "m", "supported_operations": list(ops)}
        if op in script:
            r = script[op]
            return r() if callable(r) else r
        if op == "VERIFY":   # echo the signed uuid so the occupancy guard matches by default
            return {"outcome": "ok", "running": False, "slot": 2, "generation": 4,
                    "occupancy_id": "379ff98c4149a4b5", "runtime_uuid": req.get("runtime_uuid")}
        if op == "RELEASE":
            return {"outcome": "ok", "released": True, "available": True, "slot": 2, "generation": 5}
        return {"outcome": "ok"}
    return transport


@contextmanager
def _agent(script=None, ops=("MATERIALISE", "START", "VERIFY", "STOP", "TOMBSTONE", "RELEASE")):
    """Install the scripted transport as the reclaim client's transport + a signing keyring, with a fake
    reconcile clock so any in-attempt reconcile completes instantly."""
    saved = beta_worker.make_http_transport
    beta_worker.make_http_transport = lambda *a, **k: _fake_transport(script, ops)
    state = {"t": 0.0}
    sn, ss = prov._reconcile_now, prov._reconcile_sleep
    prov._reconcile_now = lambda: state["t"]
    prov._reconcile_sleep = lambda s: state.__setitem__("t", state["t"] + s)
    try:
        with override_settings(BETA_AGENT_KEYRING='{"k1": "s3cret-key-one"}', BETA_AGENT_KEY_ID="k1",
                               BETA_AGENT_BASE_URL="http://agent.invalid:8791"):
            yield
    finally:
        beta_worker.make_http_transport = saved
        prov._reconcile_now, prov._reconcile_sleep = sn, ss


# ── RELEASE client + probe ────────────────────────────────────────────────────────────────────────────────
class ReleaseClientTests(TestCase):
    def _client(self, transport, job_id=1):
        return AgentWindowsProvisioner(job_id=job_id, transport=transport, keyring=KEYRING, key_id="k1")

    def test_release_signs_a_wellformed_release_request(self):
        seen = {}

        def transport(base, req):
            seen.update(req)
            return {"outcome": "ok", "released": True, "available": True, "slot": 2, "generation": 5}
        out = self._client(transport).release(type("R", (), {"runtime_uuid": CZ_UUID})())
        self.assertEqual(seen["operation"], "RELEASE")
        self.assertEqual(seen["runtime_uuid"], CZ_UUID)
        self.assertIn("signature", seen)
        self.assertIn("nonce", seen)
        self.assertNotIn("path", seen)          # path-free
        self.assertEqual(out, {"released": True, "available": True, "slot": 2,
                               "generation": 5, "occupancy_id": None})

    def test_release_not_ok_raises(self):
        t = lambda b, r: {"outcome": "denied", "reason_code": "release_proof_missing"}
        with self.assertRaises(ManagementChannelError) as c:
            self._client(t).release(type("R", (), {"runtime_uuid": CZ_UUID})())
        self.assertEqual(c.exception.reason_code, "release_proof_missing")

    def test_release_ok_but_released_false_is_not_silent_success(self):
        t = lambda b, r: {"outcome": "ok", "released": False}
        with self.assertRaises(ManagementChannelError) as c:
            self._client(t).release(type("R", (), {"runtime_uuid": CZ_UUID})())
        self.assertEqual(c.exception.reason_code, "release_not_confirmed")

    def test_probe_occupancy_reads_allowlisted_fields(self):
        t = lambda b, r: {"outcome": "ok", "running": False, "slot": 2, "generation": 4,
                          "runtime_uuid": CZ_UUID, "canonical_path": "C:\\secret"}
        out = self._client(t).probe_occupancy(type("R", (), {"runtime_uuid": CZ_UUID})())
        self.assertEqual(out["slot"], 2)
        self.assertEqual(out["generation"], 4)
        self.assertNotIn("canonical_path", out)      # never leak a path


# ── recovery helpers + guards ─────────────────────────────────────────────────────────────────────────────
@override_settings(BETA_RUNTIMES_ENABLED=False)
class RecoveryHelperTests(TestCase):
    def test_resolve_runtime(self):
        rt = _beta_runtime(1, uuid=CZ_UUID)
        self.assertEqual(recovery.resolve_runtime(runtime_uuid=CZ_UUID).pk, rt.pk)
        self.assertEqual(recovery.resolve_runtime(account_runtime_id=rt.pk).pk, rt.pk)
        with self.assertRaises(recovery.ReclaimError):
            recovery.resolve_runtime(runtime_uuid=CZ_UUID, account_runtime_id=rt.pk)   # ambiguous
        with self.assertRaises(recovery.ReclaimError):
            recovery.resolve_runtime(account_runtime_id=999999)                        # missing

    def test_assert_beta_refuses_production(self):
        rt = _beta_runtime(2)
        AccountRuntime.objects.filter(pk=rt.pk).update(cohort=AccountRuntime.Cohort.PRODUCTION)
        rt.refresh_from_db()
        with self.assertRaises(recovery.ReclaimError) as c:
            recovery.assert_beta(rt)
        self.assertEqual(c.exception.reason_code, "not_a_beta_runtime")

    def test_assert_probe_matches_guards(self):
        rt = _beta_runtime(3, uuid=CZ_UUID)
        base = {"runtime_uuid": CZ_UUID, "slot": 2, "generation": 4, "running": False}
        recovery.assert_probe_matches(rt, base, expect_slot=2, expect_generation=4)     # ok
        with self.assertRaises(recovery.ReclaimError):
            recovery.assert_probe_matches(rt, {**base, "runtime_uuid": "other"})
        with self.assertRaises(recovery.ReclaimError):
            recovery.assert_probe_matches(rt, base, expect_generation=5)
        with self.assertRaises(recovery.ReclaimError):
            recovery.assert_probe_matches(rt, {**base, "running": True})
        recovery.assert_probe_matches(rt, {**base, "running": True}, allow_running=True)  # override ok

    def test_recover_to_provisionable_exactly_one_job_and_idempotent(self):
        rt = _beta_runtime(4, state=RuntimeState.REMOVED, uuid=CZ_UUID)
        # a retained FAILED PROVISION job (history)
        old = ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION,
                                             status=ProvisioningJob.Status.FAILED, attempt=3)
        job = recovery.recover_to_provisionable(rt)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.NOT_PROVISIONED)
        self.assertEqual(ProvisioningJob.objects.filter(runtime=rt, op="PROVISION",
                         status__in=["QUEUED", "RUNNING"]).count(), 1)
        self.assertEqual(ProvisioningJob.objects.get(pk=old.pk).status, "FAILED")   # retained
        # idempotent: a re-run returns the same active job, still exactly one
        again = recovery.recover_to_provisionable(rt.__class__.objects.get(pk=rt.pk), require_removed=False)
        self.assertEqual(again.pk, job.pk)
        self.assertEqual(ProvisioningJob.objects.filter(runtime=rt, op="PROVISION",
                         status__in=["QUEUED", "RUNNING"]).count(), 1)

    def test_recover_requires_removed_unless_forced(self):
        rt = _beta_runtime(5, state=RuntimeState.FAILED, uuid=CZ_UUID)
        with self.assertRaises(recovery.ReclaimError) as c:
            recovery.recover_to_provisionable(rt)
        self.assertEqual(c.exception.reason_code, "not_removed")
        job = recovery.recover_to_provisionable(rt, require_removed=False)   # force accepts FAILED
        self.assertEqual(job.status, "QUEUED")


# ── reclaim command ───────────────────────────────────────────────────────────────────────────────────────
@override_settings(BETA_RUNTIMES_ENABLED=False)
class ReclaimCommandTests(TestCase):
    def _run(self, **kw):
        out = StringIO()
        call_command("reclaim_beta_runtime", stdout=out, stderr=StringIO(), **kw)
        return out.getvalue()

    def test_dry_run_default_makes_no_mutation_and_no_agent_call(self):
        rt = _beta_runtime(1, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="FAILED", attempt=3)
        calls = []
        with _agent(ops=("MATERIALISE", "START", "VERIFY", "STOP", "TOMBSTONE", "RELEASE")):
            # even if a transport existed, dry-run must not call it
            out = self._run(runtime_uuid=CZ_UUID)
        self.assertIn("DRY-RUN", out)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.FAILED)          # unchanged

    def test_cohort_guard_refuses_production(self):
        rt = _beta_runtime(2, uuid=CZ_UUID)
        AccountRuntime.objects.filter(pk=rt.pk).update(cohort=AccountRuntime.Cohort.PRODUCTION)
        with self.assertRaises(CommandError) as c:
            self._run(runtime_uuid=CZ_UUID)
        self.assertIn("not_a_beta_runtime", str(c.exception))

    def test_active_job_guard(self):
        rt = _beta_runtime(3, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="QUEUED")
        with self.assertRaises(CommandError) as c:
            self._run(runtime_uuid=CZ_UUID)
        self.assertIn("active_job_present", str(c.exception))

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_not_dark_guard(self):
        rt = _beta_runtime(4, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="FAILED", attempt=3)
        with self.assertRaises(CommandError) as c:
            self._run(runtime_uuid=CZ_UUID)
        self.assertIn("provisioner_not_dark", str(c.exception))

    def test_apply_happy_path_stop_tombstone_release_then_removed(self):
        rt = _beta_runtime(5, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="FAILED", attempt=3)
        with _agent():
            out = self._run(runtime_uuid=CZ_UUID, expect_slot=2, expect_generation=4, apply=True)
        self.assertIn("SLOT_RECLAIMED", out)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.REMOVED)
        self.assertFalse(rt.quarantined)
        # reclaim NEVER creates a ProvisioningJob (queue stays out of it)
        self.assertEqual(ProvisioningJob.objects.filter(runtime=rt, status__in=["QUEUED", "RUNNING"]).count(), 0)

    def test_apply_already_released_is_idempotent(self):
        rt = _beta_runtime(6, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="FAILED", attempt=3)
        with _agent(script={"VERIFY": {"outcome": "denied", "reason_code": "runtime_not_assigned"}}):
            out = self._run(runtime_uuid=CZ_UUID, apply=True)
        self.assertIn("SLOT_ALREADY_RELEASED", out)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.REMOVED)

    def test_apply_generation_mismatch_fails_closed(self):
        rt = _beta_runtime(7, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="FAILED", attempt=3)
        with _agent(), self.assertRaises(CommandError) as c:
            self._run(runtime_uuid=CZ_UUID, expect_generation=5, apply=True)  # probe reports gen 4
        self.assertIn("generation_mismatch", str(c.exception))
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.FAILED)                       # not touched

    def test_apply_running_process_fails_closed(self):
        rt = _beta_runtime(8, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="FAILED", attempt=3)
        with _agent(script={"VERIFY": {"outcome": "ok", "running": True, "slot": 2, "generation": 4,
                                       "runtime_uuid": CZ_UUID}}), self.assertRaises(CommandError) as c:
            self._run(runtime_uuid=CZ_UUID, apply=True)
        self.assertIn("runtime_process_present", str(c.exception))

    def test_apply_partial_teardown_quarantines_and_does_not_remove(self):
        rt = _beta_runtime(9, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="FAILED", attempt=3)
        # TOMBSTONE returns a PROVEN-partial integrity refusal -> non-retryable -> fail closed
        script = {"TOMBSTONE": {"outcome": "denied", "reason_code": "slot_integrity_mismatch"}}
        with _agent(script=script), self.assertRaises(CommandError) as c:
            self._run(runtime_uuid=CZ_UUID, expect_slot=2, expect_generation=4, apply=True)
        self.assertIn("slot_integrity_mismatch", str(c.exception))
        rt.refresh_from_db()
        self.assertTrue(rt.quarantined)                     # quarantined
        self.assertNotEqual(rt.state, RuntimeState.REMOVED)  # NEVER removed on a failed reclaim


# ── recover command ───────────────────────────────────────────────────────────────────────────────────────
@override_settings(BETA_RUNTIMES_ENABLED=False)
class RecoverCommandTests(TestCase):
    def _run(self, **kw):
        out = StringIO()
        call_command("recover_beta_runtime", stdout=out, stderr=StringIO(), **kw)
        return out.getvalue()

    def test_dry_run_default_creates_no_job(self):
        rt = _beta_runtime(1, state=RuntimeState.REMOVED, uuid=CZ_UUID)
        out = self._run(runtime_uuid=CZ_UUID)
        self.assertIn("DRY-RUN", out)
        self.assertEqual(ProvisioningJob.objects.filter(runtime=rt).count(), 0)

    def test_apply_creates_exactly_one_job_and_keeps_dark(self):
        rt = _beta_runtime(2, state=RuntimeState.REMOVED, uuid=CZ_UUID)
        ProvisioningJob.objects.create(runtime=rt, op="PROVISION", status="FAILED", attempt=3)  # history
        out = self._run(runtime_uuid=CZ_UUID, apply=True)
        self.assertIn("EXACTLY_ONE_PROVISION_JOB", out)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.NOT_PROVISIONED)
        self.assertEqual(ProvisioningJob.objects.filter(runtime=rt, status__in=["QUEUED", "RUNNING"]).count(), 1)
        # DARK preserved: the worker does nothing with the new job
        self.assertEqual(beta_worker.process_one(), "disabled")

    def test_apply_idempotent(self):
        rt = _beta_runtime(3, state=RuntimeState.REMOVED, uuid=CZ_UUID)
        self._run(runtime_uuid=CZ_UUID, apply=True)
        self._run(runtime_uuid=CZ_UUID, apply=True)
        self.assertEqual(ProvisioningJob.objects.filter(runtime=rt, op="PROVISION",
                         status__in=["QUEUED", "RUNNING"]).count(), 1)

    def test_apply_requires_removed_unless_forced(self):
        rt = _beta_runtime(4, state=RuntimeState.FAILED, uuid=CZ_UUID)
        with self.assertRaises(CommandError) as c:
            self._run(runtime_uuid=CZ_UUID, apply=True)
        self.assertIn("not_removed", str(c.exception))
        out = self._run(runtime_uuid=CZ_UUID, apply=True, force_from_failed=True)
        self.assertIn("EXACTLY_ONE_PROVISION_JOB", out)
