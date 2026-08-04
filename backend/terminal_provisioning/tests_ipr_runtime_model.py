"""IPR Area B — Canonical runtime-model regression tests.

Proves the beta-customer contradiction repairs: a dedicated-runtime account that is ``runtime_ready``
with NO legacy ``mt5_instance`` must never be told "no terminal". Covers the canonical helpers
(``account_runtime_ready`` / ``account_terminal_identity``), the truthful serializer signal (C6), the
account-action gates (C1/C2/C3), the readiness endpoint (C7) and the terminal-access views (C4/C5).

All beta capability stays DARK: these tests set only the runtime flag needed to build a ready runtime;
no execution flag is enabled and no order is ever placed.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.beta_activation import (
    account_runtime_ready, account_terminal_identity, runtime_ready)
from terminal_provisioning.models import AccountRuntime, ProvisioningJob, RuntimeState
from terminal_provisioning.provisioner import FakeProvisioner, advance_provisioning_job, enqueue_op
from trading.crypto import encrypt_password
from trading.models import TradingAccount
from trading.serializers import TradingAccountSerializer

U = get_user_model()
ENABLED = override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)


def _user(name):
    return U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")


def _acct(user, number="900001", *, is_demo=True, is_active=True):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, broker_name="DemoBroker",
        is_demo=is_demo, is_active=is_active, password_enc=encrypt_password("pw"))


def _ready_runtime(account) -> AccountRuntime:
    """Reserve a beta slot and advance provisioning to RUNNING → the canonical ready runtime
    (RUNNING + fresh heartbeat + verification report), exactly as the arm-gate tests build it."""
    rt = cap.reserve_beta_slot(account)
    advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
    rt.refresh_from_db()
    return rt


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@ENABLED
class HelperTests(TestCase):
    def test_account_runtime_ready_true_for_ready_beta_runtime(self):
        acct = _acct(_user("h1"))
        rt = _ready_runtime(acct)
        self.assertTrue(runtime_ready(rt))
        self.assertTrue(account_runtime_ready(acct))
        # Contradiction guard: ready runtime AND no legacy instance co-exist.
        self.assertIsNone(acct.mt5_instance)

    def test_account_runtime_ready_false_without_runtime(self):
        self.assertFalse(account_runtime_ready(_acct(_user("h2"))))

    def test_account_runtime_ready_false_when_stale(self):
        acct = _acct(_user("h3"))
        rt = _ready_runtime(acct)
        rt.last_heartbeat_at = timezone.now() - timedelta(hours=1)
        rt.save(update_fields=["last_heartbeat_at"])
        self.assertFalse(account_runtime_ready(acct))

    def test_account_runtime_ready_false_for_production_cohort(self):
        acct = _acct(_user("h4"))
        AccountRuntime.objects.create(
            trading_account=acct, cohort=AccountRuntime.Cohort.PRODUCTION, state=RuntimeState.RUNNING)
        # PRODUCTION cohort is out of beta readiness by design.
        self.assertFalse(account_runtime_ready(acct))

    def test_account_runtime_ready_none_account(self):
        self.assertFalse(account_runtime_ready(None))

    def test_terminal_identity_returns_running_runtime_bridge_identity(self):
        # A RUNNING beta runtime resolves to its own dedicated bridge_identity (never the legacy
        # windows_username). In production DARK no beta runtime reaches RUNNING, so this branch never
        # fires live; here the provisioner double drives it to RUNNING and stamps the identity.
        acct = _acct(_user("h5"))
        rt = _ready_runtime(acct)
        self.assertEqual(account_terminal_identity(acct), rt.bridge_identity)
        self.assertTrue(rt.bridge_identity)  # dedicated identity, not the legacy instance

    def test_terminal_identity_blank_bridge_resolves_none(self):
        # If a RUNNING beta runtime carries no bridge_identity (blank), the resolver returns None so it
        # routes nothing — the fail-closed DARK posture.
        acct = _acct(_user("h6"))
        rt = _ready_runtime(acct)
        rt.bridge_identity = ""
        rt.save(update_fields=["bridge_identity"])
        self.assertIsNone(account_terminal_identity(acct))

    def test_terminal_identity_none_without_runtime_or_instance(self):
        self.assertIsNone(account_terminal_identity(_acct(_user("h7"))))
        self.assertIsNone(account_terminal_identity(None))


@ENABLED
class SerializerSignalTests(TestCase):
    def test_serializer_reports_runtime_ready_true(self):
        acct = _acct(_user("s1"))
        _ready_runtime(acct)
        acct.refresh_from_db()
        data = TradingAccountSerializer(acct).data
        self.assertTrue(data["runtime_ready"])
        self.assertEqual(data["runtime_state"], RuntimeState.RUNNING)
        self.assertIsNone(data["mt5_instance"])  # the legacy field stays null for beta

    def test_serializer_runtime_ready_false_without_runtime(self):
        data = TradingAccountSerializer(_acct(_user("s2"))).data
        self.assertFalse(data["runtime_ready"])
        self.assertIsNone(data["runtime_state"])


@ENABLED
class AccountActionGateTests(TestCase):
    """C1/C2/C3 — a ready-runtime beta account gets a truthful 200, never the legacy 'not connected'
    409, on the account-action endpoints."""

    def setUp(self):
        self.user = _user("a1")
        self.acct = _acct(self.user)
        _ready_runtime(self.acct)
        self.acct.refresh_from_db()
        self.c = _client(self.user)

    def test_test_mt5_returns_beta_ready_200(self):
        r = self.c.post(f"/api/trading/accounts/{self.acct.id}/test-mt5/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["reason"], "broker_login_deferred")

    def test_test_connection_returns_beta_ready_200(self):
        r = self.c.post(f"/api/trading/accounts/{self.acct.id}/test/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["reason"], "broker_login_deferred")

    def test_set_active_flip_allowed_for_beta_runtime(self):
        r = self.c.post(f"/api/trading/accounts/{self.acct.id}/set-active/",
                        {"is_active": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.acct.refresh_from_db()
        self.assertTrue(self.acct.is_active)

    def test_legacy_account_without_runtime_still_409(self):
        # A non-beta account with neither instance nor runtime keeps the legacy 'not connected' 409.
        acct2 = _acct(self.user, number="900099")
        r = self.c.post(f"/api/trading/accounts/{acct2.id}/test-mt5/", {}, format="json")
        self.assertEqual(r.status_code, 409)


@ENABLED
class ReadinessEndpointTests(TestCase):
    """C7 — the readiness check counts a ready AccountRuntime as satisfying the terminal dimension,
    so a beta account with no TerminalNode is not falsely reported not-ready."""

    def test_terminal_dimension_satisfied_by_runtime(self):
        from onboarding.services import check_onboarding_permits_execution
        user = _user("r1")
        acct = _acct(user)
        _ready_runtime(acct)
        acct.refresh_from_db()
        result = check_onboarding_permits_execution(user)
        self.assertTrue(result["readiness_checks"]["terminal_node_valid"])

    def test_terminal_dimension_false_without_runtime(self):
        from onboarding.services import check_onboarding_permits_execution
        user = _user("r2")
        _acct(user)  # active account, but no runtime and no terminal node
        result = check_onboarding_permits_execution(user)
        self.assertFalse(result["readiness_checks"]["terminal_node_valid"])


@ENABLED
class TerminalAccessViewTests(TestCase):
    """C4/C5 — beta accounts get a customer-safe, non-error explanation from the shared-terminal
    viewer endpoints, never the internal 'not bound to an MT5 instance' 409."""

    def setUp(self):
        self.user = _user("t1")
        self.acct = _acct(self.user)
        _ready_runtime(self.acct)
        self.acct.refresh_from_db()
        self.c = _client(self.user)

    def test_desktop_link_beta_safe_200(self):
        r = self.c.post("/api/mt5/desktop-link/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertFalse(body["available"])
        self.assertNotIn("instance", body["detail"].lower())

    def test_launch_apply_beta_safe_200(self):
        r = self.c.post("/api/mt5/launch-apply/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["available"])
