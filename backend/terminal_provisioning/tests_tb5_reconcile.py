"""TB-5 (Trusted Beta) — reconcile_beta_provisioning re-enqueues stuck NOT_PROVISIONED runtimes.

Dark unless BETA_RUNTIMES_ENABLED; enqueue-only; idempotent; only admitted, non-quarantined beta
runtimes; never touches production.
"""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from billing.models import BetaTester
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState
from terminal_provisioning.provisioner import enqueue_op
from trading.models import TradingAccount

User = get_user_model()
ENABLED = override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)
ACTIVE = [ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING]


def _acct(n=1, *, admitted=True):
    email = f"tb5-{n}@x.invalid"
    user = User.objects.create_user(username=f"tb5-{n}", email=email, password="x")
    if admitted:
        BetaTester.objects.create(email=email)
    return TradingAccount.objects.create(
        user=user, name=f"A{n}", account_number=str(730000 + n), broker_name="DemoBroker",
        is_demo=True, is_active=True)


def _stuck_runtime(account):
    """A NOT_PROVISIONED beta runtime (as left when the flag was OFF at account-create time)."""
    rt = cap.get_or_create_beta_runtime(account)
    assert rt.state == RuntimeState.NOT_PROVISIONED
    return rt


def _prov_jobs(rt):
    return ProvisioningJob.objects.filter(runtime=rt, op=ProvisioningJob.Op.PROVISION)


class ReconcileBetaProvisioningTests(TestCase):
    def test_noop_when_flag_off(self):
        # DEFAULT: BETA_RUNTIMES_ENABLED off → nothing is re-enqueued.
        with override_settings(BETA_RUNTIMES_ENABLED=True):   # enable only to CREATE the stuck runtime
            rt = _stuck_runtime(_acct(1))
        with override_settings(BETA_RUNTIMES_ENABLED=False):
            call_command("reconcile_beta_provisioning")
        self.assertFalse(_prov_jobs(rt).exists())
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.NOT_PROVISIONED)

    @ENABLED
    def test_reenqueues_stuck_runtime(self):
        rt = _stuck_runtime(_acct(2))
        call_command("reconcile_beta_provisioning")
        self.assertTrue(_prov_jobs(rt).filter(status__in=ACTIVE).exists())   # a PROVISION job now exists
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.QUEUED)                      # slot reserved

    @ENABLED
    def test_idempotent_when_active_job_exists(self):
        rt = _stuck_runtime(_acct(3))
        enqueue_op(rt, ProvisioningJob.Op.PROVISION)   # already has an active QUEUED job
        call_command("reconcile_beta_provisioning")
        self.assertEqual(_prov_jobs(rt).count(), 1)    # not duplicated

    @ENABLED
    def test_reconciles_any_owner_regardless_of_admission(self):
        # ADR-0021: admission is no longer an eligibility gate — a stuck runtime for ANY owner is
        # re-enqueued (only null-owner / quarantined / production are excluded).
        with override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000):
            rt = _stuck_runtime(_acct(4, admitted=False))
            call_command("reconcile_beta_provisioning")
        self.assertTrue(_prov_jobs(rt).filter(status__in=ACTIVE).exists())
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.QUEUED)

    @ENABLED
    def test_skips_quarantined_runtime(self):
        rt = _stuck_runtime(_acct(5))
        rt.quarantined = True
        rt.save(update_fields=["quarantined"])
        call_command("reconcile_beta_provisioning")
        self.assertFalse(_prov_jobs(rt).exists())

    @ENABLED
    def test_leaves_running_runtime_alone(self):
        # A runtime already provisioned/QUEUED is not re-driven (state filter is NOT_PROVISIONED only).
        rt = _stuck_runtime(_acct(7))
        cap.reserve_beta_slot(rt.trading_account)   # → QUEUED (no longer NOT_PROVISIONED)
        before = _prov_jobs(rt).count()
        call_command("reconcile_beta_provisioning")
        self.assertEqual(_prov_jobs(rt).count(), before)   # untouched

    @ENABLED
    def test_fail_soft_one_error_does_not_block_others(self):
        from unittest import mock
        rt_ok = _stuck_runtime(_acct(8))
        rt_bad = _stuck_runtime(_acct(9))
        real = cap.reserve_beta_slot

        def _side(account):
            if account.id == rt_bad.trading_account_id:
                raise RuntimeError("boom")
            return real(account)

        from django.core.management.base import CommandError
        with mock.patch("terminal_provisioning.beta_capacity.reserve_beta_slot", side_effect=_side):
            with self.assertRaises(CommandError):   # non-zero exit because errors>0
                call_command("reconcile_beta_provisioning")
        self.assertTrue(_prov_jobs(rt_ok).exists())    # the good runtime still got its job
        self.assertFalse(_prov_jobs(rt_bad).exists())

    @ENABLED
    def test_limit_bounds_the_pass(self):
        rts = [_stuck_runtime(_acct(20 + i)) for i in range(3)]
        call_command("reconcile_beta_provisioning", "--limit", "1")
        enqueued = sum(1 for rt in rts if _prov_jobs(rt).exists())
        self.assertEqual(enqueued, 1)

    @ENABLED
    def test_never_touches_production_runtime(self):
        acct = _acct(6)
        prod = AccountRuntime.objects.create(
            trading_account=acct, cohort=AccountRuntime.Cohort.PRODUCTION,
            state=RuntimeState.NOT_PROVISIONED)
        call_command("reconcile_beta_provisioning")
        self.assertFalse(_prov_jobs(prod).exists())   # production is never in scope
