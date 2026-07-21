"""GFX-BETA-HEADLESS Increment 3 — Provisioning Verification Report tests."""
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from trading.models import TradingAccount
from trading.crypto import encrypt_password
from terminal_provisioning.models import (
    AccountRuntime, ProvisioningJob, ProvisioningVerificationReport, RuntimeState)
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.provisioner import FakeProvisioner, advance_provisioning_job, enqueue_op
from terminal_provisioning.verification import build_verification_report

U = get_user_model()
ENABLED = override_settings(BETA_RUNTIMES_ENABLED=True)


def _acct(n=1):
    user = U.objects.create_user(username=f"u{n}", email=f"u{n}@x.invalid", password="x")
    return TradingAccount.objects.create(
        user=user, name=f"A{n}", account_number=str(2000 + n), broker_name="DemoBroker",
        is_demo=True, password_enc=encrypt_password("pw"))


@ENABLED
class VerificationReportTests(TestCase):
    def test_report_generated_on_verified_running_with_full_evidence(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        # verify() payload ALSO smuggles secret-looking keys — the allowlist must strip them.
        p = FakeProvisioner(verify_result={"running": True, "logged_in": True,
                                            "login": str(acct.account_number), "server": "Demo-Srv",
                                            "pid": 9876, "session": 1,
                                            "password": "hunter2", "broker_password": "x",
                                            "journal": "connected to 1.2.3.4:443", "extra": 1})
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)

        rep = ProvisioningVerificationReport.objects.get(runtime=rt)
        # secret / free-text / unknown keys are NOT persisted; only the structured allowlist is.
        for leaked in ("password", "broker_password", "journal", "extra"):
            self.assertNotIn(leaked, rep.evidence)
        self.assertNotIn("hunter2", str(rep.evidence))
        self.assertTrue(rep.verified)
        self.assertTrue(rep.broker_login_verified)
        # runtime identity + ownership + broker identity + process/session all captured
        self.assertEqual(rep.runtime_uuid, rt.runtime_uuid)
        self.assertEqual(rep.runtime_root, rt.runtime_root)
        self.assertEqual(rep.owner_user_id, acct.user.id)
        self.assertEqual(rep.owner_email, acct.user.email)
        self.assertEqual(rep.trading_account_id, acct.id)
        self.assertEqual(rep.broker_login, str(acct.account_number))
        self.assertEqual(rep.broker_server, "Demo-Srv")
        self.assertEqual(rep.process_pid, 9876)
        self.assertEqual(rep.windows_session, 1)
        self.assertIsNotNone(rep.provisioning_duration_ms)
        self.assertGreaterEqual(rep.provisioning_duration_ms, 0)
        self.assertIsNotNone(rep.heartbeat_at)
        # evidence carries the verify payload keys and NO password field
        self.assertIn("running", rep.evidence)
        self.assertIn("session", rep.evidence)
        self.assertNotIn("password", rep.evidence)

    def test_report_is_immutable(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        rep = ProvisioningVerificationReport.objects.get(runtime=rt)
        rep.verified = False
        with self.assertRaises(ValueError):
            rep.save()

    def test_no_report_when_not_verified(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        # broker login fails → runtime FAILED, no report produced
        p = FakeProvisioner(verify_result={"running": True, "logged_in": False,
                                            "login": None, "server": None})
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.FAILED)
        self.assertEqual(ProvisioningVerificationReport.objects.filter(runtime=rt).count(), 0)

    def test_generator_refuses_production_runtime(self):
        acct = _acct(1)
        prod = AccountRuntime.objects.create(
            trading_account=acct, cohort=AccountRuntime.Cohort.PRODUCTION)
        with self.assertRaises(ValueError):
            build_verification_report(prod, {"running": True, "logged_in": True})

    def test_generator_refuses_when_not_running_or_not_logged_in(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        with self.assertRaises(ValueError):
            build_verification_report(rt, {"running": True, "logged_in": False})
        with self.assertRaises(ValueError):
            build_verification_report(rt, {"running": False, "logged_in": True})

    def test_report_is_immutable_to_delete(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        rep = ProvisioningVerificationReport.objects.get(runtime=rt)
        with self.assertRaises(ValueError):
            rep.delete()

    def test_no_duplicate_report_on_re_advance(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        advance_provisioning_job(job, FakeProvisioner())
        # a second advance of the DONE job, and a fresh advance of the already-RUNNING runtime,
        # must NOT create a second report.
        advance_provisioning_job(job, FakeProvisioner())
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        self.assertEqual(ProvisioningVerificationReport.objects.filter(runtime=rt).count(), 1)
