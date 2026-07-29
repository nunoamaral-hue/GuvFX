"""TB-3 (Trusted Beta) — durable per-account credential-validation state + account_status surfacing."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.beta import grant_beta_entitlement
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


class AddPathRecordsIntentTests(TestCase):
    """ADR-0021 canonical creation contract — adding a broker account records CUSTOMER INTENT ONLY: the
    account is created (``mt5_instance=None``; ``validation_status`` deferred), with NO immediate broker
    login and NO shared-instance requirement. A re-add is idempotent. Broker-login validation is proven at
    the runtime-provisioning stage (``terminal_provisioning`` tests, behind
    ``PROVISIONING_REQUIRE_BROKER_LOGIN``), never here.

    Supersedes the removed ``AddPathSetsValidatedTests``, which asserted the RETIRED immediate-login
    stamping of VALIDATED / CONNECTION_FAILED / TECHNICAL_ERROR at add-time (a shared-instance path that
    ``409``'d every dedicated-runtime customer). The account-status rendering of ``validation_status`` —
    now set by provisioning, not by add — is still covered by ``AccountStatusValidationStageTests`` above.
    """
    def setUp(self):
        self.user = User.objects.create_user(username="a", email="a@x.invalid", password="x")
        grant_beta_entitlement(self.user)   # capacity to create accounts (the per-user cap now applies)
        self.factory = APIRequestFactory()

    def _post(self, number, **extra):
        body = {"name": "A", "account_number": number, "password": "pw",
                "broker_name": "DemoBroker", "is_demo": True, **extra}
        req = self.factory.post("/api/trading/accounts/add-with-mt5-login/", body, format="json")
        force_authenticate(req, user=self.user)
        return AddAccountWithMt5LoginView.as_view()(req)

    def test_add_records_intent_not_validated(self):
        resp = self._post("700901")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(resp.data["ok"])
        self.assertTrue(resp.data["valid"])    # "intent recorded" — NOT "broker login verified"
        self.assertTrue(resp.data["created"])
        acct = TradingAccount.objects.get(user=self.user, account_number="700901")
        # Records intent only — NOT validated at add-time; broker login is deferred to provisioning.
        self.assertEqual(acct.validation_status, VS.NEVER)
        self.assertIsNone(acct.mt5_instance)    # never the legacy shared-instance binding
        self.assertFalse(acct.is_active)

    def test_readd_same_account_is_idempotent(self):
        first = self._post("700904")
        self.assertEqual(first.status_code, 201, first.data)
        again = self._post("700904")
        self.assertEqual(again.status_code, 200)   # existing account returned, never a duplicate
        self.assertFalse(again.data["created"])
        self.assertEqual(
            TradingAccount.objects.filter(user=self.user, account_number="700904").count(), 1)

    def test_missing_password_is_400(self):
        req = self.factory.post("/api/trading/accounts/add-with-mt5-login/", {
            "name": "A", "account_number": "700908", "broker_name": "DemoBroker", "is_demo": True},
            format="json")
        force_authenticate(req, user=self.user)
        resp = AddAccountWithMt5LoginView.as_view()(req)
        self.assertEqual(resp.status_code, 400)
