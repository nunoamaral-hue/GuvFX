"""GFX-BETA-PHASE0 Increment 5 — atomic broker-account cap, raw-agent-error sanitisation,
and the read-only admin beta-estate view (never exposes decrypted credentials).

These are additive Phase-0 hardening tests. They must not touch Nuno's staff-bypassed flow:
staff users are exempt from the cap (verified below)."""
from unittest import mock

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from trading.models import TradingAccount
from mt5.models import Mt5Instance
from billing.models import UserSubscriptionState

U = get_user_model()

LIST_URL = "/api/trading/accounts/"


def _set_plan(user, plan):
    UserSubscriptionState.objects.update_or_create(
        user=user, defaults={"current_plan": plan, "plan_status": "active", "viewer_mode": False})


class BrokerAccountCapTests(TestCase):
    """The maximum broker-account count is enforced atomically in the backend, not only the frontend."""

    def setUp(self):
        self.client = APIClient()

    def _make_user(self, email, plan=None, staff=False):
        u = U.objects.create_user(username=email, email=email, password="x", is_staff=staff)
        if plan:
            _set_plan(u, plan)
        return u

    def test_cap_blocks_create_past_plan_limit(self):
        # starter_trial → max_trading_accounts == 1. Pre-seed one, the second POST must be rejected.
        user = self._make_user("cap@x.invalid", plan="starter_trial")
        TradingAccount.objects.create(user=user, name="A0", account_number="1", broker_name="B", is_demo=True)
        self.client.force_authenticate(user)
        resp = self.client.post(LIST_URL, {"name": "A1", "account_number": "2", "broker_name": "B"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("limit reached", str(resp.content).lower())
        self.assertIn("maximum 1", str(resp.content))
        self.assertEqual(TradingAccount.objects.filter(user=user).count(), 1)  # nothing created

    def test_viewer_with_no_state_cannot_create_any(self):
        # No subscription state → viewer defaults → max 0 → even the first create is fail-closed.
        user = self._make_user("viewer@x.invalid")
        self.client.force_authenticate(user)
        resp = self.client.post(LIST_URL, {"name": "V1", "account_number": "9", "broker_name": "B"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("maximum 0", str(resp.content))
        self.assertEqual(TradingAccount.objects.filter(user=user).count(), 0)

    def test_beta_limit_is_ten(self):
        # beta plan → cap 10. Message reflects the effective (min(10, plan)) limit.
        user = self._make_user("beta@x.invalid", plan="beta")
        for i in range(10):
            TradingAccount.objects.create(
                user=user, name=f"A{i}", account_number=str(100 + i), broker_name="B", is_demo=True)
        self.client.force_authenticate(user)
        resp = self.client.post(LIST_URL, {"name": "A10", "account_number": "999", "broker_name": "B"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("maximum 10", str(resp.content))
        self.assertEqual(TradingAccount.objects.filter(user=user).count(), 10)

    def test_staff_user_is_exempt_from_cap(self):
        # Nuno's production account is staff — it must remain untouched by the cap.
        staff = self._make_user("staff@x.invalid", staff=True)
        for i in range(12):
            TradingAccount.objects.create(
                user=staff, name=f"S{i}", account_number=str(200 + i), broker_name="B", is_demo=True)
        self.client.force_authenticate(staff)
        resp = self.client.post(LIST_URL, {"name": "S12", "account_number": "999", "broker_name": "B"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(TradingAccount.objects.filter(user=staff).count(), 13)

    def test_under_limit_create_succeeds(self):
        user = self._make_user("ok@x.invalid", plan="beta")
        self.client.force_authenticate(user)
        resp = self.client.post(LIST_URL, {"name": "OK1", "account_number": "5", "broker_name": "B"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(TradingAccount.objects.filter(user=user).count(), 1)

    def test_create_takes_a_row_lock_on_the_user(self):
        # Deterministic proof (no threads) that the cap check is serialised: a non-staff create must
        # issue a `SELECT ... FOR UPDATE` on the user row so concurrent creates cannot both pass the count.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        user = self._make_user("lock@x.invalid", plan="beta")
        self.client.force_authenticate(user)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.post(
                LIST_URL, {"name": "L1", "account_number": "7", "broker_name": "B"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        sqls = [q["sql"].upper() for q in ctx.captured_queries]
        self.assertTrue(
            any("FOR UPDATE" in s and "USERS_USER" in s for s in sqls),
            msg=f"expected a SELECT ... FOR UPDATE on users_user; got: {sqls}")


class TestMt5RawErrorSanitisationTests(TestCase):
    """The test-mt5 endpoint must never echo a raw agent exception (which can leak internal host/URL)."""

    def setUp(self):
        self.client = APIClient()
        self.user = U.objects.create_user(username="s@x.invalid", email="s@x.invalid", password="x")
        _set_plan(self.user, "beta")
        self.inst = Mt5Instance.objects.create(hostname="WIN-TEST-1", windows_username="guvfx_u_test")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="777", broker_name="B",
            is_demo=True, mt5_instance=self.inst)

    def test_agent_exception_detail_is_sanitised(self):
        secret = "http://10.50.0.2:8787 INTERNAL_BOOM_TRACE"
        env = {"WINDOWS_AGENT_BASE": "http://10.50.0.2:8787", "WINDOWS_AGENT_TOKEN": "tkn"}
        self.client.force_authenticate(self.user)
        with mock.patch.dict("os.environ", env), mock.patch(
                "urllib.request.urlopen", side_effect=Exception(secret)):
            resp = self.client.post(f"{LIST_URL}{self.acct.id}/test-mt5/", {}, format="json")
        self.assertEqual(resp.status_code, 502, resp.content)
        body = str(resp.content)
        self.assertNotIn("INTERNAL_BOOM_TRACE", body)
        self.assertNotIn("10.50.0.2", body)
        self.assertIn("could not be reached", body)

    def test_connection_endpoint_does_not_echo_raw_agent_detail(self):
        # The test-connection ("/test/") passthrough must strip the agent's free-text `detail`, which on
        # failure carries str(e) or internal env-var names — only the safe structured fields are echoed.
        secret = "boom-at-http://10.50.0.2:8787 SECRET_TRACE"
        env = {"WINDOWS_AGENT_BASE": "http://10.50.0.2:8787", "WINDOWS_AGENT_TOKEN": "tkn"}
        self.client.force_authenticate(self.user)
        with mock.patch.dict("os.environ", env), mock.patch(
                "urllib.request.urlopen", side_effect=Exception(secret)):
            resp = self.client.post(f"{LIST_URL}{self.acct.id}/test/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data["reason"], "request_failed")   # safe structured code is preserved
        body = str(data)
        self.assertNotIn("SECRET_TRACE", body)
        self.assertNotIn("10.50.0.2", body)
        self.assertNotIn("detail", data["agent"])            # raw detail stripped from the echo

    def test_connection_endpoint_does_not_leak_env_var_names(self):
        # Unconfigured-agent branch: the agent `detail` names WINDOWS_AGENT_BASE/TOKEN — must not reach the client.
        self.client.force_authenticate(self.user)
        with mock.patch.dict("os.environ", {"WINDOWS_AGENT_BASE": "", "WINDOWS_AGENT_TOKEN": ""}):
            resp = self.client.post(f"{LIST_URL}{self.acct.id}/test/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = str(resp.json())
        self.assertNotIn("WINDOWS_AGENT_BASE", body)
        self.assertNotIn("WINDOWS_AGENT_TOKEN", body)


class AdminBetaEstateViewTests(TestCase):
    """Read-only per-user estate for operators. Staff-gated; never exposes decrypted credentials."""

    URL = "/api/admin/beta-estate/"

    def setUp(self):
        self.client = APIClient()
        self.beta = U.objects.create_user(username="b@x.invalid", email="b@x.invalid", password="x")
        _set_plan(self.beta, "beta")
        # A broker account with a (would-be-secret) stored password: it must never appear in the response.
        self.acct = TradingAccount.objects.create(
            user=self.beta, name="A", account_number="4242", broker_name="B",
            is_demo=True, is_active=True, password_enc="ENC_SUPER_SECRET_BLOB")

    def test_requires_admin_role(self):
        self.client.force_authenticate(self.beta)  # ordinary user
        self.assertEqual(self.client.get(self.URL).status_code, 403)

    def test_superuser_sees_estate_without_credentials(self):
        admin = U.objects.create_user(
            username="a@x.invalid", email="a@x.invalid", password="x",
            is_staff=True, is_superuser=True)
        self.client.force_authenticate(admin)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data["ok"])
        body = str(data)
        # non-secret account number is fine to surface…
        self.assertIn("4242", body)
        # …but the encrypted credential blob and password fields must NEVER be present.
        self.assertNotIn("ENC_SUPER_SECRET_BLOB", body)
        self.assertNotIn("password", body)
        self.assertNotIn("password_enc", body)
        # The estate lists the beta user with a runtime_state (durable), defaulting to NOT_PROVISIONED.
        user_row = next(r for r in data["users"] if r["email"] == "b@x.invalid")
        self.assertTrue(user_row["is_beta"])
        self.assertEqual(user_row["accounts"][0]["runtime_state"], "NOT_PROVISIONED")

    def test_staff_users_are_excluded_from_estate(self):
        # The estate is the *customer* estate — staff/Nuno are not listed.
        admin = U.objects.create_user(
            username="a2@x.invalid", email="a2@x.invalid", password="x",
            is_staff=True, is_superuser=True)
        self.client.force_authenticate(admin)
        resp = self.client.get(self.URL)
        emails = [r["email"] for r in resp.json()["users"]]
        self.assertNotIn("a2@x.invalid", emails)
        self.assertIn("b@x.invalid", emails)
