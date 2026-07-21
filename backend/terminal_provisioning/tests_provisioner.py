"""GFX-BETA-HEADLESS Increment 2 — provisioning executor (driver) tests.

Covers: the PROVISION happy path (materialise→configure→start→verify→RUNNING); verify-before-RUNNING
(control 8) incl. broker-identity mismatch fail-closed; idempotency/resumability; bounded retries;
non-retryable bad creds; enqueue-only + single-flight lease; PRODUCTION refusal (control 14); and that
the driver never leaks the broker password.
"""
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from trading.models import TradingAccount
from trading.crypto import encrypt_password
from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState, RuntimeEvent
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning import provisioner as prov
from terminal_provisioning.provisioner import (
    FakeProvisioner, ProvisionStepError, advance_provisioning_job, enqueue_op)

U = get_user_model()
ENABLED = override_settings(BETA_RUNTIMES_ENABLED=True)


def _acct(n=1, password="brokerpw123"):
    user = U.objects.create_user(username=f"u{n}", email=f"u{n}@x.invalid", password="x")
    return TradingAccount.objects.create(
        user=user, name=f"A{n}", account_number=str(1000 + n), broker_name="DemoBroker",
        is_demo=True, password_enc=encrypt_password(password))


@ENABLED
class ProvisionHappyPathTests(TestCase):
    def test_full_provision_reaches_running_only_after_verify(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        p = FakeProvisioner()  # verify reports the expected identity by default
        job = advance_provisioning_job(job, p)

        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        # the ordered lifecycle actually ran
        names = [c[0] for c in p.calls]
        self.assertEqual(names, ["materialise", "configure", "start", "verify"])

    def test_password_is_passed_to_configure_but_never_persisted_or_logged(self):
        acct = _acct(2, password="s3cret-broker-pw")
        rt = cap.get_or_create_beta_runtime(acct)
        p = FakeProvisioner()
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        # configure received a non-empty password (bool True) but the plaintext is nowhere in the
        # durable evidence (RuntimeEvent detail/reason) or the runtime's sanitised fields.
        cfg = next(c for c in p.calls if c[0] == "configure")
        self.assertTrue(cfg[3])  # password bool was True
        for ev in RuntimeEvent.objects.filter(runtime=rt):
            self.assertNotIn("s3cret-broker-pw", ev.detail)
            self.assertNotIn("s3cret-broker-pw", ev.reason_code)
        rt.refresh_from_db()
        self.assertNotIn("s3cret-broker-pw", rt.last_error)
        self.assertNotIn("s3cret-broker-pw", rt.last_failure_reason)


@ENABLED
class VerifyBeforeRunningTests(TestCase):
    def test_wrong_broker_identity_fails_closed_not_running(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        # verify says running+logged_in but to the WRONG login — must NOT reach RUNNING (control 8/5).
        p = FakeProvisioner(verify_result={"running": True, "logged_in": True,
                                           "login": "999999", "server": "OtherBroker"})
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.FAILED)   # non-retryable identity mismatch
        self.assertEqual(rt.last_failure_reason, "broker_identity_mismatch")
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)

    def test_not_logged_in_is_non_retryable_failed(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        p = FakeProvisioner(verify_result={"running": True, "logged_in": False,
                                           "login": None, "server": None})
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.FAILED)
        self.assertEqual(rt.last_failure_reason, "broker_login_failed")


@ENABLED
class RetryAndResumeTests(TestCase):
    def test_retryable_materialise_failure_requeues_then_resumes_to_running(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        # First pass: materialise blows up (retryable). Runtime stays in its resumable state.
        p_fail = FakeProvisioner(fail_on={"materialise": ProvisionStepError("materialise_failed")})
        job = advance_provisioning_job(job, p_fail)
        rt.refresh_from_db()
        self.assertEqual(job.status, ProvisioningJob.Status.QUEUED)   # re-queued for retry
        self.assertEqual(job.attempt, 1)
        self.assertEqual(rt.state, RuntimeState.PROVISIONING)   # resumable, not RUNNING
        self.assertEqual(rt.last_failure_reason, "materialise_failed")
        # Second pass with a healthy provisioner resumes from PROVISIONING and reaches RUNNING.
        p_ok = FakeProvisioner()
        job = advance_provisioning_job(job, p_ok)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)

    def test_retries_exhaust_to_failed(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        p = FakeProvisioner(fail_on={"materialise": ProvisionStepError("materialise_failed")})
        for _ in range(prov.MAX_ATTEMPTS):
            job = advance_provisioning_job(job, p)
        rt.refresh_from_db()
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)
        self.assertEqual(rt.state, RuntimeState.FAILED)

    def test_idempotent_second_advance_of_done_job_is_noop(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        p2 = FakeProvisioner()
        job2 = advance_provisioning_job(job, p2)
        self.assertEqual(job2.status, ProvisioningJob.Status.DONE)
        self.assertEqual(p2.calls, [])   # a DONE job does no further provisioner work


@ENABLED
class LeaseAndLifecycleTests(TestCase):
    def test_live_lease_prevents_double_claim(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        # Simulate a live lease held by another worker.
        from django.utils import timezone
        job.status = ProvisioningJob.Status.RUNNING
        job.lease_expires_at = timezone.now() + timezone.timedelta(seconds=300)
        job.save(update_fields=["status", "lease_expires_at"])
        p = FakeProvisioner()
        advance_provisioning_job(job, p)
        self.assertEqual(p.calls, [])   # did not steal the live lease

    def test_stop_then_deprovision(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        rt.refresh_from_db(); self.assertEqual(rt.state, RuntimeState.RUNNING)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.STOP), FakeProvisioner())
        rt.refresh_from_db(); self.assertEqual(rt.state, RuntimeState.STOPPED)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.DEPROVISION), FakeProvisioner())
        rt.refresh_from_db(); self.assertEqual(rt.state, RuntimeState.REMOVED)


class ProductionRefusalTests(TestCase):
    def test_enqueue_refuses_production_runtime(self):
        acct = _acct(1)
        prod = AccountRuntime.objects.create(
            trading_account=acct, cohort=AccountRuntime.Cohort.PRODUCTION)
        with self.assertRaises(ValueError):
            enqueue_op(prod, ProvisioningJob.Op.PROVISION)

    def test_advance_refuses_a_production_job_and_does_not_touch_it(self):
        # Even if a job is somehow created against a PRODUCTION runtime (bypassing enqueue_op),
        # advancing it must fail the JOB cleanly and never transition the production runtime.
        acct = _acct(1)
        prod = AccountRuntime.objects.create(
            trading_account=acct, cohort=AccountRuntime.Cohort.PRODUCTION,
            state=RuntimeState.RUNNING)
        job = ProvisioningJob.objects.create(runtime=prod, op=ProvisioningJob.Op.PROVISION)
        p = FakeProvisioner()
        job = advance_provisioning_job(job, p)
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)
        self.assertEqual(job.last_error, "invalid_runtime")
        prod.refresh_from_db()
        self.assertEqual(prod.state, RuntimeState.RUNNING)   # untouched
        self.assertEqual(p.calls, [])


@ENABLED
class DriverDenialAndEdgeTests(TestCase):
    def test_capacity_denial_while_advancing_fails_job_not_wedged_or_done(self):
        # Fill the pool, then advance a 6th runtime's PROVISION job → CapacityError in the driver must
        # FAIL the job (not mark DONE, not wedge it RUNNING) and leave the runtime BLOCKED (not RUNNING).
        for i in range(cap.BETA_MAX_ACTIVE_RUNTIMES):
            cap.reserve_beta_slot(_acct(i))
        sixth = _acct(99)
        rt = cap.get_or_create_beta_runtime(sixth)
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)
        self.assertEqual(job.last_error, "beta_pool_full")
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.BLOCKED)   # truthful, not RUNNING, not DONE

    @override_settings(BETA_RUNTIMES_ENABLED=False)
    def test_kill_switch_off_fails_job_cleanly(self):
        rt = AccountRuntime.objects.create(
            trading_account=_acct(1), cohort=AccountRuntime.Cohort.BETA)
        job = ProvisioningJob.objects.create(runtime=rt, op=ProvisioningJob.Op.PROVISION)
        job = advance_provisioning_job(job, FakeProvisioner())
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)
        self.assertEqual(job.last_error, "beta_runtimes_disabled")

    def test_verify_not_running_is_retryable(self):
        rt = cap.get_or_create_beta_runtime(_acct(1))
        p = FakeProvisioner(verify_result={"running": False, "logged_in": False,
                                            "login": None, "server": None})
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        rt.refresh_from_db()
        self.assertEqual(job.status, ProvisioningJob.Status.QUEUED)   # retryable → re-queued
        self.assertEqual(rt.state, RuntimeState.AUTHENTICATING)       # resumable at verify
        self.assertEqual(rt.last_failure_reason, "terminal_not_running")

    def test_start_op_reaches_running_via_shared_verify(self):
        rt = cap.get_or_create_beta_runtime(_acct(1))
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.STOP), FakeProvisioner())
        rt.refresh_from_db(); self.assertEqual(rt.state, RuntimeState.STOPPED)
        # START must materialise → verify → RUNNING (not strand in AUTHENTICATING)
        p = FakeProvisioner()
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.START), p)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertIn("verify", [c[0] for c in p.calls])

    def test_server_mismatch_with_broker_server_fails_closed(self):
        from trading.models import BrokerServer
        bs = BrokerServer.objects.create(broker_display_name="Demo", server_name="MetaQuotes-Demo")
        acct = _acct(1)
        acct.broker_server = bs
        acct.save(update_fields=["broker_server"])
        rt = cap.get_or_create_beta_runtime(acct)
        # verify reports the right login but the WRONG server → identity mismatch, fail closed.
        p = FakeProvisioner(verify_result={"running": True, "logged_in": True,
                                            "login": str(acct.account_number), "server": "Evil-Server"})
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.FAILED)
        self.assertEqual(rt.last_failure_reason, "broker_identity_mismatch")
