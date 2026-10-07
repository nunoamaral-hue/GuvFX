"""Stream D5 — the hosted Add-Account path must honour the explicit D2 ``account_type`` end-to-end.

Reproduces the Account-44 defect: 'Add Account -> Live -> hosted account creation' MUST create a genuinely LIVE
account (``is_demo=False`` + a LIVE-classified broker server), not a Demo account. Inverse: 'Add Account ->
Demo' remains Demo (byte-identical). Guards: missing/invalid type fails validation; LIVE is gated fail-closed by
``HOSTED_LIVE_ONBOARDING_ENABLED``; a demo/live server disagreement fails closed. No monitoring/execution is
enabled; no LiveExecutionAuthorization; no MT5_ALLOW_LIVE; no order.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from hosted_workspace import provisioning as P
from trading.account_policy import Environment, account_environment
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

# Subsystem visible + admission open. HOSTED_LIVE_ONBOARDING_ENABLED is applied per-test (default OFF).
BASE = dict(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,
            CLOSED_BETA_OPEN_ACCESS_ENABLED=True)
LIVE_ON = dict(BASE, HOSTED_LIVE_ONBOARDING_ENABLED=True)

ADD_URL = "/api/hosted-workspace/accounts/add/"
JOURNEY_URL = "/api/hosted-workspace/onboarding/journey/"


def _client(n):
    u = User.objects.create_user(username=f"d5_{n}", email=f"d5_{n}@x.invalid", password="x")
    c = APIClient()
    c.force_authenticate(u)
    return u, c


@override_settings(**BASE)
class HostedLiveOnboardingTests(TestCase):
    # ---- the Account-44 reproduction ----
    @override_settings(**LIVE_ON)
    def test_live_selection_creates_a_genuinely_live_account(self):
        _u, c = _client("live")
        r = c.post(ADD_URL, {"broker_name": "TradersWay", "expected_login": "55442",
                             "expected_server": "TradersWay-Live", "account_type": "live"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        acct = TradingAccount.objects.get(pk=r.data["trading_account_id"])
        self.assertFalse(acct.is_demo, "Add Account -> Live must persist is_demo=False (Account-44 defect)")
        self.assertEqual(acct.broker_server.environment, BrokerServer.LIVE)
        self.assertEqual(account_environment(acct), Environment.LIVE)   # classifies cleanly as LIVE

    # ---- the inverse: demo stays demo, byte-identical ----
    def test_demo_selection_creates_demo_account(self):
        _u, c = _client("demo")
        r = c.post(ADD_URL, {"broker_name": "Taurex", "expected_login": "830227146",
                             "expected_server": "Taurex-Demo", "account_type": "demo"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        acct = TradingAccount.objects.get(pk=r.data["trading_account_id"])
        self.assertTrue(acct.is_demo)
        self.assertEqual(acct.broker_server.environment, BrokerServer.DEMO)
        self.assertEqual(account_environment(acct), Environment.DEMO)

    # ---- LIVE is fail-closed when the dedicated onboarding gate is OFF (BASE has it off) ----
    def test_live_rejected_when_onboarding_flag_off(self):
        _u, c = _client("liveoff")
        before = TradingAccount.objects.count()
        r = c.post(ADD_URL, {"broker_name": "TradersWay", "expected_login": "55442",
                             "expected_server": "TradersWay-Live", "account_type": "live"}, format="json")
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(r.data["reason"], P.REQ_LIVE_ONBOARDING_DISABLED)
        self.assertEqual(TradingAccount.objects.count(), before)        # never silently created

    # ---- missing / invalid account_type fails validation ----
    def test_missing_account_type_rejected(self):
        _u, c = _client("missing")
        r = c.post(ADD_URL, {"broker_name": "B", "expected_login": "1", "expected_server": "S"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["reason"], "account_type_required")

    def test_invalid_account_type_rejected(self):
        _u, c = _client("invalid")
        r = c.post(ADD_URL, {"broker_name": "B", "expected_login": "1", "expected_server": "S",
                             "account_type": "paper"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["reason"], "account_type_required")

    # ---- demo/live disagreement with an EXISTING server fails closed ----
    @override_settings(**LIVE_ON)
    def test_live_on_a_demo_server_fails_closed(self):
        BrokerServer.objects.create(server_name="Shared-Demo-Srv", environment=BrokerServer.DEMO)
        _u, c = _client("mismatch")
        before = TradingAccount.objects.count()
        r = c.post(ADD_URL, {"broker_name": "B", "expected_login": "9",
                             "expected_server": "Shared-Demo-Srv", "account_type": "live"}, format="json")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["reason"], P.REQ_ENV_MISMATCH)
        self.assertEqual(TradingAccount.objects.count(), before)        # never overrides a server's environment

    # ---- the journey projection tells the client whether LIVE onboarding may be offered ----
    def test_journey_exposes_live_onboarding_unavailable_when_off(self):
        _u, c = _client("projoff")
        r = c.get(JOURNEY_URL)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data.get("live_onboarding_available"), False)

    @override_settings(**LIVE_ON)
    def test_journey_exposes_live_onboarding_available_when_on(self):
        _u, c = _client("projon")
        r = c.get(JOURNEY_URL)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data.get("live_onboarding_available"), True)


# Multi-account (Phase-C enforcement armed) + LIVE onboarding on — reproduces support@'s real path (Account-45).
MULTI = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1",
             CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True, HOSTED_LIVE_ONBOARDING_ENABLED=True)


@override_settings(**MULTI)
class MultiAccountLiveOnboardingTests(TestCase):
    """D5.1 — a MULTI-account hosted user (the Account-45 defect) must still get the LIVE option: the journey's
    ``multiple_accounts`` chooser response now carries ``live_onboarding_available`` (previously only on the
    single-account projection), and an explicit Live add creates a genuinely LIVE account."""

    def setUp(self):
        from billing.models import UserSubscriptionState
        from trading.account_entitlement import grant_concurrent_enforcement
        self.user = User.objects.create_user(username="d5multi", email="d5multi@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(user=self.user, defaults=dict(
            current_plan=UserSubscriptionState.Plan.BETA,
            plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False))
        grant_concurrent_enforcement(self.user)
        self.c = APIClient(); self.c.force_authenticate(self.user)

    def _add(self, login, server, account_type):
        return self.c.post(ADD_URL, {"broker_name": "B", "expected_login": login,
                                     "expected_server": server, "account_type": account_type}, format="json")

    def test_multi_account_user_can_add_a_live_account(self):
        # existing MULTIPLE accounts (so the journey returns the multi-account chooser)
        self.assertEqual(self._add("md1", "IS6-Demo", "demo").status_code, 201)
        self.assertEqual(self._add("md2", "PepperstoneUK-Demo", "demo").status_code, 201)
        j = self.c.get(JOURNEY_URL)
        self.assertEqual(j.status_code, 200, j.data)
        self.assertEqual(j.data.get("status"), "multiple_accounts")
        self.assertTrue(j.data.get("live_onboarding_available"), j.data)   # the Account-45 fix
        # Add Account -> Live -> genuinely LIVE (not DEMO)
        r = self._add("55442", "TradersWay-Live", "live")
        self.assertEqual(r.status_code, 201, r.data)
        acct = TradingAccount.objects.get(pk=r.data["trading_account_id"])
        self.assertFalse(acct.is_demo)
        self.assertEqual(acct.broker_server.environment, BrokerServer.LIVE)
        self.assertEqual(account_environment(acct), Environment.LIVE)

    def test_multi_account_live_unavailable_when_flag_off(self):
        with override_settings(HOSTED_LIVE_ONBOARDING_ENABLED=False):
            self.assertEqual(self._add("md1", "IS6-Demo", "demo").status_code, 201)
            self.assertEqual(self._add("md2", "PepperstoneUK-Demo", "demo").status_code, 201)
            j = self.c.get(JOURNEY_URL)
            self.assertEqual(j.data.get("status"), "multiple_accounts")
            self.assertFalse(j.data.get("live_onboarding_available"))


@override_settings(**LIVE_ON)
class HostedLiveOnboardingServiceTests(TestCase):
    """Service-level guards on ``request_hosted_workspace`` (defence in depth below the view)."""

    def test_service_rejects_live_when_onboarding_disabled(self):
        u = User.objects.create_user(username="svc1", email="svc1@x.invalid", password="x")
        with override_settings(HOSTED_LIVE_ONBOARDING_ENABLED=False):
            res = P.request_hosted_workspace(u, expected_login="1", expected_server="X-Live",
                                             broker_name="X", is_demo=False)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_LIVE_ONBOARDING_DISABLED)

    def test_service_live_sets_live_server_env(self):
        u = User.objects.create_user(username="svc2", email="svc2@x.invalid", password="x")
        res = P.request_hosted_workspace(u, expected_login="2", expected_server="X2-Live",
                                         broker_name="X", is_demo=False)
        self.assertTrue(res.ok, res.reason)
        acct = res.workspace.trading_account
        self.assertFalse(acct.is_demo)
        self.assertEqual(acct.broker_server.environment, BrokerServer.LIVE)

    def test_live_request_never_returns_an_existing_demo_workspace(self):
        # Adversarial-review hardening (drift class): the idempotent-return path must NOT silently hand a LIVE
        # request an existing DEMO workspace — it fails closed. (One-workspace-per-user / enforcement-OFF branch.)
        u = User.objects.create_user(username="svc3", email="svc3@x.invalid", password="x")
        first = P.request_hosted_workspace(u, expected_login="10", expected_server="D-Demo",
                                           broker_name="B", is_demo=True)
        self.assertTrue(first.ok, first.reason)
        self.assertTrue(first.workspace.trading_account.is_demo)
        res = P.request_hosted_workspace(u, expected_login="20", expected_server="D-Live",
                                         broker_name="B", is_demo=False)
        self.assertFalse(res.ok)                       # never silently returns the demo workspace
        self.assertEqual(res.reason, P.REQ_ENV_MISMATCH)

    def test_demo_rerequest_is_still_idempotent(self):
        # The guard must NOT break same-environment idempotency: a demo re-request returns the demo workspace.
        u = User.objects.create_user(username="svc4", email="svc4@x.invalid", password="x")
        a = P.request_hosted_workspace(u, expected_login="30", expected_server="D-Demo", broker_name="B", is_demo=True)
        b = P.request_hosted_workspace(u, expected_login="30", expected_server="D-Demo", broker_name="B", is_demo=True)
        self.assertTrue(a.ok and b.ok)
        self.assertFalse(b.created)
        self.assertEqual(a.workspace.pk, b.workspace.pk)
