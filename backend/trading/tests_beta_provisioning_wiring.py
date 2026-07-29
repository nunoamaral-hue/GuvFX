"""ADR-0021 — broker-record creation wires the DEFAULT dedicated-runtime path (reservation + Job).

Under ADR-0021 dedicated-runtime provisioning is the permanent, default customer execution model: ANY
non-staff customer who creates a broker-account record gets an owned runtime (new AccountRuntime path)
reserved and a PROVISION job enqueued — gated ONLY by BETA_RUNTIMES_ENABLED (off → deferred, record still
created). There is NO per-user admission gate. Never the legacy shared MT5 instance; Nuno's staff estate
is unaffected. Creation is idempotent at BOTH layers (canonical lookup + DB constraints + winner
recovery): a duplicate submission yields exactly one account, one reservation, and one active job.
"""
import threading

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from billing.beta import grant_beta_entitlement
from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState
from trading.models import TradingAccount

U = get_user_model()

_ACCT_PAYLOAD = {"name": "My Demo", "account_number": "500100", "broker_name": "DemoBroker",
                 "is_demo": True, "password": "demopass"}


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _beta_customer(email="cust@example.invalid"):
    """A plain entitled customer — NO admission allowlist (ADR-0021 removed it as an eligibility gate)."""
    u = U.objects.create_user(username=email, email=email, password="x")
    grant_beta_entitlement(u)
    return u


class BetaProvisioningWiringTests(TestCase):
    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_customer_create_reserves_runtime_and_enqueues_provision(self):
        u = _beta_customer()
        resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        acct = TradingAccount.objects.get(user=u)
        self.assertIsNone(acct.mt5_instance)              # NOT bound to the legacy shared instance
        rt = AccountRuntime.objects.get(trading_account=acct)
        self.assertEqual(rt.cohort, AccountRuntime.Cohort.BETA)
        self.assertEqual(rt.state, RuntimeState.QUEUED)   # reserved a pool slot
        self.assertTrue(
            ProvisioningJob.objects.filter(runtime=rt, op=ProvisioningJob.Op.PROVISION).exists())

    def test_create_flag_off_defers_no_job(self):
        # BETA_RUNTIMES_ENABLED default OFF: record is still created, but no reservation and no job.
        u = _beta_customer()
        resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        acct = TradingAccount.objects.get(user=u)
        self.assertEqual(ProvisioningJob.objects.count(), 0)
        rt = AccountRuntime.objects.get(trading_account=acct)
        self.assertEqual(rt.state, RuntimeState.NOT_PROVISIONED)   # runtime exists but never reserved

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_routes_to_dedicated_runtime_even_when_unleased_instance_available(self):
        # A spare unleased Windows instance must NOT pull a customer onto the legacy shared path.
        from mt5.models import Mt5Instance
        Mt5Instance.objects.create(hostname="spare-box", platform="windows",
                                   windows_username="spareu", is_leased=False)
        u = _beta_customer()
        resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        acct = TradingAccount.objects.get(user=u)
        self.assertIsNone(acct.mt5_instance)   # NO legacy binding despite an available instance
        self.assertTrue(AccountRuntime.objects.filter(
            trading_account=acct, cohort=AccountRuntime.Cohort.BETA).exists())

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_second_account_blocked_by_per_user_cap_but_still_created(self):
        # Per-user runtime cap (1) must BLOCK the 2nd runtime without ERRORING the 2nd broker-record create.
        u = _beta_customer()
        c = _client(u)
        r1 = c.post("/api/trading/accounts/", {**_ACCT_PAYLOAD, "account_number": "500100"}, format="json")
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
    def test_any_entitled_customer_gets_dedicated_runtime_no_admission(self):
        # ADR-0021: entitlement alone is sufficient — there is NO admission allowlist gate any more.
        u = _beta_customer("plain@example.invalid")
        resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        acct = TradingAccount.objects.get(user=u)
        self.assertTrue(AccountRuntime.objects.filter(
            trading_account=acct, cohort=AccountRuntime.Cohort.BETA).exists())
        self.assertEqual(ProvisioningJob.objects.filter(op=ProvisioningJob.Op.PROVISION).count(), 1)

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_duplicate_submission_is_idempotent(self):
        # The SAME (user, account_number, broker) submitted twice → exactly one account / runtime / job.
        u = _beta_customer()
        c = _client(u)
        r1 = c.post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        r2 = c.post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertIn(r2.status_code, (200, 201))                  # idempotent, never a duplicate/500
        self.assertEqual(TradingAccount.objects.filter(user=u).count(), 1)
        acct = TradingAccount.objects.get(user=u)
        self.assertEqual(AccountRuntime.objects.filter(trading_account=acct).count(), 1)
        self.assertEqual(ProvisioningJob.objects.filter(op=ProvisioningJob.Op.PROVISION).count(), 1)

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_whitespace_variant_is_idempotent(self):
        # A resubmission whose account_number/broker_name only differ by surrounding whitespace is the
        # SAME account after canonical normalisation — no duplicate.
        u = _beta_customer()
        c = _client(u)
        c.post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        r2 = c.post("/api/trading/accounts/",
                    {**_ACCT_PAYLOAD, "account_number": "  500100 ", "broker_name": " DemoBroker "},
                    format="json")
        self.assertIn(r2.status_code, (200, 201))
        self.assertEqual(TradingAccount.objects.filter(user=u).count(), 1)

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_recovers_winner_on_integrityerror(self):
        # Directly exercise the IntegrityError WINNER RECOVERY branch, INDEPENDENT of the cap row-lock:
        # a race where the canonical lookup MISSES but the DB already holds the row, so serializer.save()
        # raises IntegrityError (partial-unique constraint). Recovery must return the winner idempotently
        # — never a duplicate, never a 500. This proves idempotency does not depend on the lock.
        from unittest import mock
        u = _beta_customer()
        winner = TradingAccount.objects.create(
            user=u, name="W", account_number="500100", broker_name="DemoBroker",
            is_demo=True, is_active=False)
        # fast-path lookup + under-lock re-check both MISS; the recovery lookup finds the winner.
        with mock.patch("trading.views._find_existing_account", side_effect=[None, None, winner]):
            resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        self.assertEqual(TradingAccount.objects.filter(user=u, account_number="500100").count(), 1)

    def test_missing_broker_identity_is_400(self):
        # No broker_server and no broker_name → a clean 400 (mirrors the DB CheckConstraint), not a 500.
        u = _beta_customer()
        resp = _client(u).post("/api/trading/accounts/",
                               {"name": "X", "account_number": "999", "is_demo": True, "password": "p"},
                               format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(TradingAccount.objects.filter(user=u).count(), 0)

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_staff_create_unaffected(self):
        boss = U.objects.create_user(username="boss@example.invalid",
                                     email="boss@example.invalid", password="x", is_staff=True)
        resp = _client(boss).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(AccountRuntime.objects.count(), 0)   # staff path never triggers beta provisioning
        self.assertEqual(ProvisioningJob.objects.count(), 0)


@override_settings(BETA_RUNTIMES_ENABLED=True)
class ConcurrentCreateTests(TransactionTestCase):
    """A genuine (real-thread, real-commit) concurrent-submission test. Requires TransactionTestCase so
    each thread's committed writes are visible across connections."""
    reset_sequences = True

    def test_concurrent_identical_submissions_create_exactly_one(self):
        u = _beta_customer()
        n = 4
        start = threading.Barrier(n)
        statuses = []
        lock = threading.Lock()

        def submit():
            try:
                start.wait()   # release all threads together to maximise contention
                resp = _client(u).post("/api/trading/accounts/", _ACCT_PAYLOAD, format="json")
                with lock:
                    statuses.append(resp.status_code)
            finally:
                connections.close_all()   # don't leak the thread's DB connection into teardown

        threads = [threading.Thread(target=submit) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No request errored (winner recovery returns the winner, never a 500 / duplicate).
        self.assertTrue(all(s in (200, 201) for s in statuses), statuses)
        # EXACTLY ONE of each: account, runtime, active provisioning job.
        self.assertEqual(TradingAccount.objects.filter(user=u).count(), 1)
        acct = TradingAccount.objects.get(user=u)
        self.assertEqual(AccountRuntime.objects.filter(trading_account=acct).count(), 1)
        self.assertEqual(
            ProvisioningJob.objects.filter(
                runtime__trading_account=acct, op=ProvisioningJob.Op.PROVISION,
                status__in=[ProvisioningJob.Status.QUEUED, ProvisioningJob.Status.RUNNING]).count(),
            1)
