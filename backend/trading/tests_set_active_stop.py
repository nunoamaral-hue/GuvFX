"""WAYOND account-scoped strategy management — Start/Stop trading semantics (set_active) + switch_enforced.

Sponsor decisions verified here:
  * STOP TRADING (deactivation) must ALWAYS succeed on a hosted account, even when the runtime is
    unhealthy — a customer can always make an account ineligible for new automated dispatch. START may
    be blocked when the runtime isn't ready; STOP must not.
  * ZERO active accounts is a valid state.
  * set_active only toggles automated-trading eligibility (is_active) — it never arms execution.
  * entitlement-summary exposes ``switch_enforced`` (= backend will actually apply STANDARD/CONCURRENT
    switch semantics) so the UI can gate the STANDARD "the other account stops trading" confirm on the
    real backend behaviour, not on account_mode alone.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from django.test import TestCase

from billing.models import UserSubscriptionState
from trading.models import TradingAccount

User = get_user_model()


def _user(name):
    u = User.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.create(
        user=u, current_plan=UserSubscriptionState.Plan.PRO,
        plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


def _hosted_acct(user, number, *, active=True):
    # hosted / dedicated-runtime account: mt5_instance is None (the runtime IS the terminal)
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, is_demo=True, is_active=active,
        broker_name="DemoBroker", mt5_instance=None)


def _url(acc):
    return f"/api/trading/accounts/{acc.id}/set-active/"


READY = "trading.views._account_runtime_ready"


class StopTradingTests(TestCase):
    def setUp(self):
        self.user = _user("stopper")
        self.acc = _hosted_acct(self.user, "H1", active=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_stop_succeeds_even_when_runtime_not_ready(self):
        # THE Phase-9 fix: STOP must not be blocked by an unhealthy runtime.
        with mock.patch(READY, return_value=False):
            r = self.client.post(_url(self.acc), {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.acc.refresh_from_db()
        self.assertFalse(self.acc.is_active)

    def test_start_blocked_when_runtime_not_ready(self):
        self.acc.is_active = False
        self.acc.save(update_fields=["is_active"])
        with mock.patch(READY, return_value=False):
            r = self.client.post(_url(self.acc), {"is_active": True}, format="json")
        self.assertEqual(r.status_code, 409, r.content)
        self.acc.refresh_from_db()
        self.assertFalse(self.acc.is_active)  # still stopped

    def test_start_succeeds_when_runtime_ready(self):
        self.acc.is_active = False
        self.acc.save(update_fields=["is_active"])
        with mock.patch(READY, return_value=True):
            r = self.client.post(_url(self.acc), {"is_active": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.acc.refresh_from_db()
        self.assertTrue(self.acc.is_active)

    def test_zero_active_is_valid(self):
        # the only active account may be stopped → zero active is allowed (no last-active guard on hosted)
        with mock.patch(READY, return_value=True):
            r = self.client.post(_url(self.acc), {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(TradingAccount.objects.filter(user=self.user, is_active=True).count(), 0)


class SwitchEnforcedFieldTests(TestCase):
    def setUp(self):
        self.user = _user("summary")
        _hosted_acct(self.user, "S1")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = "/api/trading/accounts/entitlement-summary/"

    def test_switch_enforced_false_by_default(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("switch_enforced", r.data)
        self.assertFalse(r.data["switch_enforced"])  # empty allowlist → not enforced

    def test_switch_enforced_true_after_grant(self):
        from trading.account_entitlement import grant_concurrent_enforcement
        grant_concurrent_enforcement(self.user)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data["switch_enforced"])
