"""CVM-Inc-2 — broker-record creation wires the NEW beta path (reservation + ProvisioningJob).

When an ADMITTED controlled-beta tester creates a broker-account record via the public accounts API, the
backend allocates its owned beta runtime (new AccountRuntime path), reserves a pool slot, and enqueues a
PROVISION job — gated by BETA_RUNTIMES_ENABLED (off → deferred, record still created). Never the legacy
shared MT5 instance; non-admitted users (even entitled ones) and Nuno's estate are unaffected.
"""
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from billing.beta import grant_beta_entitlement
from billing.models import BetaTester
from onboarding.services import get_or_create_onboarding_state
from trading.models import TradingAccount
from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState

U = get_user_model()

_ACCT_PAYLOAD = {"name": "My Demo", "account_number": "500100", "broker_name": "DemoBroker",
                 "is_demo": True, "password": "demopass"}


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _admitted_beta_user(email="bt@example.invalid"):
    # Allowlist + register, then "visit the dashboard" (onboarding state) which admits + grants entitlement
    # — mirroring the journey order (a broker account can only be created once entitled).
    BetaTester.objects.create(email=email)
    u = U.objects.create_user(username=email, email=email, password="x")
    get_or_create_onboarding_state(u)
    return u


class BetaProvisioningWiringTests(TestCase):
    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_admitted_beta_create_reserves_runtime_and_enqueues_provision(self):
        u = _admitted_beta_user()
        resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        acct = TradingAccount.objects.get(user=u)
        self.assertIsNone(acct.mt5_instance)              # NOT bound to the legacy shared instance
        rt = AccountRuntime.objects.get(trading_account=acct)
        self.assertEqual(rt.cohort, AccountRuntime.Cohort.BETA)
        self.assertEqual(rt.state, RuntimeState.QUEUED)   # reserved a pool slot
        self.assertTrue(
            ProvisioningJob.objects.filter(runtime=rt, op=ProvisioningJob.Op.PROVISION).exists())

    def test_admitted_beta_create_flag_off_defers_no_job(self):
        # BETA_RUNTIMES_ENABLED default OFF: record is still created, but no reservation and no job.
        u = _admitted_beta_user()
        resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        acct = TradingAccount.objects.get(user=u)
        self.assertEqual(ProvisioningJob.objects.count(), 0)
        rt = AccountRuntime.objects.get(trading_account=acct)
        # the durable BETA runtime exists but was NEVER reserved (no slot held) while the flag is OFF.
        self.assertEqual(rt.state, RuntimeState.NOT_PROVISIONED)

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_admitted_beta_routes_to_beta_even_when_unleased_instance_available(self):
        # Admission routing must beat incidental lease state: a spare unleased Windows instance must NOT
        # pull an admitted beta tester onto the legacy shared path — mt5_instance stays None.
        from mt5.models import Mt5Instance
        Mt5Instance.objects.create(hostname="spare-box", platform="windows",
                                   windows_username="spareu", is_leased=False)
        u = _admitted_beta_user()
        resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        acct = TradingAccount.objects.get(user=u)
        self.assertIsNone(acct.mt5_instance)   # NO legacy binding despite an available instance
        self.assertTrue(AccountRuntime.objects.filter(
            trading_account=acct, cohort=AccountRuntime.Cohort.BETA).exists())

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_second_account_blocked_by_per_user_cap_but_still_created(self):
        # Per-user cap (1) must BLOCK the 2nd runtime without ERRORING the 2nd broker-record create.
        u = _admitted_beta_user()
        c = _client(u)
        r1 = c.post("/api/trading/accounts/",
                    {**_ACCT_PAYLOAD, "account_number": "500100"}, format="json")
        self.assertEqual(r1.status_code, 201)
        r2 = c.post("/api/trading/accounts/",
                    {**_ACCT_PAYLOAD, "name": "Second", "account_number": "500200"}, format="json")
        self.assertEqual(r2.status_code, 201)                       # 2nd record still created
        accts = TradingAccount.objects.filter(user=u).order_by("id")
        self.assertEqual(accts.count(), 2)
        self.assertEqual(                                          # only the 1st got a PROVISION job
            ProvisioningJob.objects.filter(op=ProvisioningJob.Op.PROVISION).count(), 1)
        rt2 = AccountRuntime.objects.get(trading_account=accts[1])
        self.assertEqual(rt2.state, RuntimeState.BLOCKED)          # 2nd blocked by the per-user cap

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_entitled_non_admitted_user_creates_no_beta_runtime(self):
        # Entitlement alone (NOT on the admission allowlist) must NOT trigger beta provisioning — the
        # wiring is gated on ADMISSION, not merely on the beta plan.
        u = U.objects.create_user(username="ent@example.invalid", email="ent@example.invalid", password="x")
        grant_beta_entitlement(u)   # can create accounts, but is NOT allowlisted
        resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(AccountRuntime.objects.count(), 0)
        self.assertEqual(ProvisioningJob.objects.count(), 0)

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_staff_create_unaffected(self):
        boss = U.objects.create_user(username="boss@example.invalid",
                                     email="boss@example.invalid", password="x", is_staff=True)
        resp = _client(boss).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(AccountRuntime.objects.count(), 0)   # staff path never triggers beta provisioning
        self.assertEqual(ProvisioningJob.objects.count(), 0)
