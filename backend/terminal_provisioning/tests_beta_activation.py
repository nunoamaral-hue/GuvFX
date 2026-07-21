"""CVM-Inc-3 sub-increment A — narrow activation gate + runtime-ready semantics."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from billing.models import BetaTester
from trading.models import TradingAccount
from trading.crypto import encrypt_password
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.beta_activation import (
    ActivationDenied, assert_beta_activation_allowed, broker_connected, runtime_ready)
from terminal_provisioning.models import (
    AccountRuntime, ProvisioningJob, ProvisioningVerificationReport, RuntimeState)
from terminal_provisioning.provisioner import FakeProvisioner, advance_provisioning_job, enqueue_op

U = get_user_model()
ENABLED = override_settings(BETA_RUNTIMES_ENABLED=True)


def _acct(n=1, admitted=True, email=None):
    email = email or f"bt{n}@example.invalid"
    if admitted:
        BetaTester.objects.create(email=email)
    user = U.objects.create_user(username=email, email=email, password="x")
    return TradingAccount.objects.create(
        user=user, name=f"A{n}", account_number=str(700000 + n), broker_name="DemoBroker",
        is_demo=True, password_enc=encrypt_password("pw"))


@ENABLED
class ActivationGateTests(TestCase):
    def _reserved(self, admitted=True, n=1):
        acct = _acct(n, admitted=admitted)
        return cap.reserve_beta_slot(acct)  # → QUEUED (HELD), canonical path

    def test_all_conditions_pass_for_admitted_reserved_runtime(self):
        rt = self._reserved(admitted=True)
        assert_beta_activation_allowed(rt)  # no raise

    @override_settings(BETA_RUNTIMES_ENABLED=False)
    def test_denies_when_global_flag_off(self):
        # build the runtime with the flag on, then check the gate with it off
        with override_settings(BETA_RUNTIMES_ENABLED=True):
            rt = self._reserved()
        with self.assertRaises(ActivationDenied) as ctx:
            assert_beta_activation_allowed(rt)
        self.assertEqual(ctx.exception.reason_code, "beta_runtimes_disabled")

    def test_denies_non_admitted_user_even_with_flag_on(self):
        # A reserved BETA runtime whose owner is NOT admitted must be refused launch (control 2).
        rt = self._reserved(admitted=False)
        with self.assertRaises(ActivationDenied) as ctx:
            assert_beta_activation_allowed(rt)
        self.assertEqual(ctx.exception.reason_code, "user_not_admitted")

    def test_denies_production_cohort(self):
        acct = _acct(1)
        prod = AccountRuntime.objects.create(trading_account=acct, cohort=AccountRuntime.Cohort.PRODUCTION,
                                             state=RuntimeState.RUNNING)
        with self.assertRaises(ActivationDenied) as ctx:
            assert_beta_activation_allowed(prod)
        self.assertEqual(ctx.exception.reason_code, "not_a_beta_runtime")

    def test_denies_when_not_reserved(self):
        acct = _acct(1)
        rt = cap.get_or_create_beta_runtime(acct)  # NOT_PROVISIONED (no slot)
        with self.assertRaises(ActivationDenied) as ctx:
            assert_beta_activation_allowed(rt)
        self.assertEqual(ctx.exception.reason_code, "slot_not_reserved")

    def test_denies_noncanonical_path(self):
        rt = self._reserved()
        rt.runtime_root = r"C:\Users\attacker\evil"
        rt.save(update_fields=["runtime_root"])
        with self.assertRaises(ActivationDenied) as ctx:
            assert_beta_activation_allowed(rt)
        self.assertEqual(ctx.exception.reason_code, "noncanonical_runtime_path")


@ENABLED
class ActivationGateInProvisionerTests(TestCase):
    def test_non_admitted_reserved_runtime_cannot_launch(self):
        # Reserve a BETA runtime for a NON-admitted account, then advance PROVISION → the gate denies
        # BEFORE any box work; job FAILED, no materialise/start called.
        acct = _acct(1, admitted=False)
        rt = cap.reserve_beta_slot(acct)
        p = FakeProvisioner()
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        self.assertEqual(job.status, ProvisioningJob.Status.FAILED)
        self.assertEqual(job.last_error, "user_not_admitted")
        self.assertEqual(p.calls, [])   # NO box side-effect (materialise/start) occurred
        rt.refresh_from_db()
        self.assertNotEqual(rt.state, RuntimeState.RUNNING)

    def test_admitted_runtime_launches_through_gate(self):
        acct = _acct(1, admitted=True)
        rt = cap.reserve_beta_slot(acct)
        p = FakeProvisioner()
        job = advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), p)
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertIn("materialise", [c[0] for c in p.calls])


@ENABLED
class RuntimeReadySemanticsTests(TestCase):
    def _running_with_report(self):
        acct = _acct(1)
        rt = cap.reserve_beta_slot(acct)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        rt.refresh_from_db()
        return rt

    def test_runtime_ready_true_when_running_fresh_with_report(self):
        rt = self._running_with_report()
        self.assertTrue(runtime_ready(rt))
        # broker_connected is FALSE in the broker-independent phase (no login verified).
        self.assertFalse(broker_connected(rt))
        self.assertFalse(ProvisioningVerificationReport.objects.get(runtime=rt).broker_login_verified)

    def test_runtime_ready_false_when_heartbeat_stale(self):
        rt = self._running_with_report()
        rt.last_heartbeat_at = timezone.now() - timedelta(hours=1)
        rt.save(update_fields=["last_heartbeat_at"])
        self.assertFalse(runtime_ready(rt))

    def test_runtime_ready_false_without_report(self):
        acct = _acct(1)
        rt = cap.reserve_beta_slot(acct)
        # Force RUNNING + heartbeat but NO report → not ready.
        from terminal_provisioning.runtime_state import record_transition
        rt = record_transition(rt, RuntimeState.STARTING, reason_code="t")
        rt = record_transition(rt, RuntimeState.AUTHENTICATING, reason_code="t")
        rt = record_transition(rt, RuntimeState.RUNNING, reason_code="t")
        rt.last_heartbeat_at = timezone.now()
        rt.save(update_fields=["last_heartbeat_at"])
        self.assertFalse(runtime_ready(rt))


@ENABLED
class AccountConnectedBetaSemanticsTests(TestCase):
    def _admitted_user_with_account(self):
        acct = _acct(1, admitted=True)
        from onboarding.services import complete_step, get_or_create_onboarding_state
        get_or_create_onboarding_state(acct.user)  # admit + entitle + email_verified
        complete_step(acct.user, step="plan_selected")   # journey prerequisites for account_connected
        complete_step(acct.user, step="risk_accepted")
        return acct

    def test_account_connected_refused_until_runtime_ready(self):
        from onboarding.services import complete_step, OnboardingStepError
        acct = self._admitted_user_with_account()
        cap.get_or_create_beta_runtime(acct)  # exists but NOT ready
        with self.assertRaises(OnboardingStepError):
            complete_step(acct.user, step="account_connected")

    def test_account_connected_marks_on_runtime_ready_without_legacy_binding(self):
        from onboarding.services import complete_step
        acct = self._admitted_user_with_account()
        rt = cap.reserve_beta_slot(acct)
        advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
        state = complete_step(acct.user, step="account_connected")
        self.assertTrue(state.account_connected)
        acct.refresh_from_db()
        self.assertIsNone(acct.mt5_instance)   # NEVER bound to the legacy shared instance
        rt.refresh_from_db()
        self.assertTrue(runtime_ready(rt))
        self.assertFalse(broker_connected(rt))  # not presented as broker-connected
