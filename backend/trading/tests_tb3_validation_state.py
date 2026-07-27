"""TB-3 (Trusted Beta) — durable per-account credential-validation state + account_status surfacing."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from mt5.models import Mt5Instance
from terminal_provisioning.account_status import build_account_status
from trading.models import TradingAccount
from trading.views_account_add import AddAccountWithMt5LoginView

User = get_user_model()
VS = TradingAccount.ValidationStatus


def _acct(user, number="700900", **kw):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, broker_name="DemoBroker", is_demo=True,
        is_active=True, **kw)


def _stage(status, key):
    return next(s for s in status["stages"] if s["key"] == key)


class AccountStatusValidationStageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="v", email="v@x.invalid", password="x")

    def test_validated_is_healthy(self):
        st = build_account_status(_acct(self.user, validation_status=VS.VALIDATED))
        self.assertEqual(_stage(st, "credentials_validated")["state"], "HEALTHY")

    def test_stored_but_never_is_warning(self):
        st = build_account_status(_acct(self.user, validation_status=VS.NEVER))
        self.assertEqual(_stage(st, "credentials_validated")["state"], "WARNING")

    def test_connection_failed_is_failed_and_escalates_overall(self):
        st = build_account_status(_acct(self.user, validation_status=VS.CONNECTION_FAILED))
        self.assertEqual(_stage(st, "credentials_validated")["state"], "FAILED")
        self.assertEqual(st["overall"], "FAILED")

    def test_technical_error_is_failed(self):
        st = build_account_status(_acct(self.user, validation_status=VS.TECHNICAL_ERROR))
        self.assertEqual(_stage(st, "credentials_validated")["state"], "FAILED")
        self.assertEqual(st["overall"], "FAILED")

    def test_no_credentials_is_not_configured(self):
        acct = _acct(self.user)
        acct.account_number = ""
        acct.save(update_fields=["account_number"])
        st = build_account_status(acct)
        self.assertEqual(_stage(st, "credentials_validated")["state"], "NOT_CONFIGURED")

    def test_default_never_does_not_escalate_overall(self):
        # A normal stored-but-unvalidated account must NOT read FAILED overall (regression guard).
        st = build_account_status(_acct(self.user))
        self.assertNotEqual(st["overall"], "FAILED")


class AddPathSetsValidatedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="a", email="a@x.invalid", password="x")
        self.inst = Mt5Instance.objects.create(hostname="host-tb3", windows_username="guvfx_u_x")
        self.factory = APIRequestFactory()

    def test_created_account_is_validated(self):
        with mock.patch("trading.views_account_add._get_user_mt5_instance", return_value=self.inst), \
             mock.patch("trading.views_account_add._windows_agent_post_json",
                        return_value={"ok": True, "valid": True, "currency": "USD"}):
            req = self.factory.post("/api/trading/accounts/add-with-mt5-login/", {
                "name": "A", "account_number": "700901", "password": "pw",
                "broker_name": "DemoBroker", "is_demo": True}, format="json")
            force_authenticate(req, user=self.user)
            resp = AddAccountWithMt5LoginView.as_view()(req)
        self.assertEqual(resp.status_code, 201, resp.data)
        acct = TradingAccount.objects.get(user=self.user, account_number="700901")
        self.assertEqual(acct.validation_status, VS.VALIDATED)
        self.assertIsNotNone(acct.validated_at)

    def _post(self, agent, number):
        with mock.patch("trading.views_account_add._get_user_mt5_instance", return_value=self.inst), \
             mock.patch("trading.views_account_add._windows_agent_post_json", return_value=agent):
            req = self.factory.post("/api/trading/accounts/add-with-mt5-login/", {
                "name": "A", "account_number": number, "password": "pw",
                "broker_name": "DemoBroker", "is_demo": True}, format="json")
            force_authenticate(req, user=self.user)
            return AddAccountWithMt5LoginView.as_view()(req)

    def test_relink_of_existing_account_stamps_validated(self):
        # M2: re-adding an existing account with a valid login must stamp the EXISTING row VALIDATED,
        # not leave it showing "stored but not validated".
        existing = _acct(self.user, number="700904", validation_status=VS.NEVER)
        resp = self._post({"ok": True, "valid": True, "currency": "USD"}, "700904")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("reason"), "already_linked")
        existing.refresh_from_db()
        self.assertEqual(existing.validation_status, VS.VALIDATED)
        self.assertIsNotNone(existing.validated_at)

    def test_readd_invalid_login_stamps_connection_failed(self):
        # M1: a re-add whose login is now invalid stamps the existing account CONNECTION_FAILED.
        existing = _acct(self.user, number="700905", validation_status=VS.VALIDATED)
        resp = self._post({"ok": True, "valid": False, "reason": "invalid_password"}, "700905")
        self.assertEqual(resp.status_code, 200)
        existing.refresh_from_db()
        self.assertEqual(existing.validation_status, VS.CONNECTION_FAILED)

    def test_readd_agent_error_stamps_technical_error(self):
        existing = _acct(self.user, number="700906", validation_status=VS.VALIDATED)
        resp = self._post({"ok": False, "detail": "agent down"}, "700906")
        self.assertEqual(resp.status_code, 502)
        existing.refresh_from_db()
        self.assertEqual(existing.validation_status, VS.TECHNICAL_ERROR)

    def test_first_time_invalid_stamps_nothing(self):
        # No existing row → the failure-path qs.update is a harmless no-op (no stray account).
        resp = self._post({"ok": True, "valid": False, "reason": "invalid"}, "700907")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(TradingAccount.objects.filter(account_number="700907").exists())

    def test_invalid_credentials_create_no_account(self):
        # Fail-safe: an invalid login never creates an account (so no stray NEVER/unvalidated row);
        # the customer gets a clear transient state, not an admin queue.
        with mock.patch("trading.views_account_add._get_user_mt5_instance", return_value=self.inst), \
             mock.patch("trading.views_account_add._windows_agent_post_json",
                        return_value={"ok": True, "valid": False, "reason": "invalid_password"}):
            req = self.factory.post("/api/trading/accounts/add-with-mt5-login/", {
                "name": "A", "account_number": "700902", "password": "bad",
                "broker_name": "DemoBroker", "is_demo": True}, format="json")
            force_authenticate(req, user=self.user)
            resp = AddAccountWithMt5LoginView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("valid"), False)
        self.assertFalse(TradingAccount.objects.filter(account_number="700902").exists())
