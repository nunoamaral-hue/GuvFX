"""ADR-0021 PR A — busy-worker heartbeat lifecycle (Correction 1).

Proves the durable liveness heartbeat is refreshed at every worker lifecycle point and, in particular,
that a long-running (multi-step) provisioning job keeps a fresh PROCESSING heartbeat — so a busy worker
NEVER produces a false ``provisioner_unhealthy`` for a NEW reservation.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from billing import beta
from billing.models import BetaTester
from trading.crypto import encrypt_password
from trading.models import TradingAccount

from terminal_provisioning import beta_capacity as cap
from terminal_provisioning import beta_worker
from terminal_provisioning.mgmt_client import ManagementChannelError
from terminal_provisioning.models import ProvisionerHeartbeat, ProvisioningJob, RuntimeState
from terminal_provisioning.provisioner import FakeProvisioner, advance_provisioning_job, enqueue_op

U = get_user_model()
HB = ProvisionerHeartbeat


def _acct(n=1):
    email = f"hb{n}@x.invalid"
    user = U.objects.create_user(username=f"hb{n}", email=email, password="x")
    BetaTester.objects.create(email=email)
    return TradingAccount.objects.create(
        user=user, name=f"A{n}", account_number=str(7000 + n), broker_name="DemoBroker",
        is_demo=True, password_enc=encrypt_password("pw"))


def _stale(seconds=999):
    HB.objects.filter(pk=HB.SINGLETON_ID).update(updated_at=timezone.now() - timedelta(seconds=seconds))


class _AgingProvisioner(FakeProvisioner):
    """Simulates a genuinely long stage: each step lets the heartbeat go stale BEFORE returning, so the
    per-step refresh is the only thing that can keep the worker reported healthy."""
    def materialise(self, runtime):
        _stale(); return super().materialise(runtime)

    def configure(self, runtime, *, login, server, password):
        _stale(); return super().configure(runtime, login=login, server=server, password=password)

    def start(self, runtime):
        _stale(); return super().start(runtime)

    def verify(self, runtime):
        _stale(); return super().verify(runtime)


class _NegotiationFailsClient:
    def assert_compatible(self):
        raise ManagementChannelError("agent_unreachable")


@override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000,
                   BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS=120)
class BusyWorkerHeartbeatTests(TestCase):
    def test_heartbeat_fires_after_every_provisioning_step(self):
        rt = cap.get_or_create_beta_runtime(_acct(1))
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        calls = []
        advance_provisioning_job(job, FakeProvisioner(), heartbeat=lambda: calls.append(1))
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        # materialise → configure → start → verify each refreshed liveness mid-run
        self.assertGreaterEqual(len(calls), 4)

    def test_busy_worker_end_state_is_processing_and_healthy(self):
        rt = cap.reserve_beta_slot(_acct(2))
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        status = beta_worker.process_one(lambda job: FakeProvisioner(), negotiate=False)
        self.assertEqual(status, "advanced")
        hb = HB.objects.get(pk=HB.SINGLETON_ID)
        self.assertEqual(hb.status, HB.Status.PROCESSING)
        self.assertEqual(hb.last_job_id, ProvisioningJob.objects.get(runtime=rt).id)
        self.assertTrue(beta.provisioning_service_healthy())          # busy ≠ unhealthy

    def test_long_running_job_never_reads_stale(self):
        # A multi-step job where every stage lets the heartbeat age past TTL; only the per-step refresh
        # keeps it healthy. If the refresh were missing, the final state would be stale ⇒ unhealthy.
        rt = cap.reserve_beta_slot(_acct(3))
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        prov = _AgingProvisioner()
        status = beta_worker.process_one(lambda job: prov, negotiate=False)
        self.assertEqual(status, "advanced")
        self.assertEqual([c[0] for c in prov.calls], ["materialise", "configure", "start", "verify"])
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertTrue(beta._provisioner_heartbeat_fresh())          # refreshed back to fresh
        self.assertTrue(beta.provisioning_service_healthy())

    def test_negotiation_failure_marks_degraded_and_unhealthy(self):
        rt = cap.reserve_beta_slot(_acct(4))
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        status = beta_worker.process_one(lambda job: _NegotiationFailsClient(), negotiate=True)
        self.assertEqual(status, "negotiation_failed")
        self.assertEqual(HB.objects.get(pk=HB.SINGLETON_ID).status, HB.Status.DEGRADED)
        self.assertFalse(beta.provisioning_service_healthy())         # degraded ⇒ fail closed

    def test_worker_error_marks_error_and_unhealthy(self):
        rt = cap.reserve_beta_slot(_acct(5))
        ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        with mock.patch("terminal_provisioning.beta_worker.advance_provisioning_job",
                        side_effect=RuntimeError("boom")):
            status = beta_worker.process_one(lambda job: FakeProvisioner(), negotiate=False)
        self.assertEqual(status, "error")
        self.assertEqual(HB.objects.get(pk=HB.SINGLETON_ID).status, HB.Status.ERROR)
        self.assertFalse(beta.provisioning_service_healthy())         # error ⇒ fail closed

    def test_idle_poll_marks_idle_ready(self):
        # No jobs queued: the worker still refreshes an IDLE_READY heartbeat (proves liveness while idle).
        self.assertEqual(beta_worker.process_one(lambda job: FakeProvisioner()), "no_job")
        hb = HB.objects.get(pk=HB.SINGLETON_ID)
        self.assertEqual(hb.status, HB.Status.IDLE_READY)
        self.assertTrue(beta.provisioning_service_healthy())

    def test_disabled_worker_still_heartbeats_idle(self):
        with override_settings(BETA_RUNTIMES_ENABLED=False):
            self.assertEqual(beta_worker.process_one(), "disabled")
        # kill switch is off, so it is NOT healthy for reservation, but the heartbeat row exists & is idle
        self.assertEqual(HB.objects.get(pk=HB.SINGLETON_ID).status, HB.Status.IDLE_READY)
