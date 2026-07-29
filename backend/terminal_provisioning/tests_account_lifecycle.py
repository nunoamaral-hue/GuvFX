"""ADR-0021 — explicit customer-facing account lifecycle surfaced by build_account_status:
Account received → Provisioning runtime → Connecting to broker → Validated / Connection failed → Retry.

The lifecycle is derived from the durable AccountRuntime + validation state; the "Connecting to broker"
phase exists only when broker-login is required (``PROVISIONING_REQUIRE_BROKER_LOGIN``)."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from terminal_provisioning.account_status import build_account_status
from terminal_provisioning.models import AccountRuntime, RuntimeState
from trading.models import TradingAccount

U = get_user_model()


def _acct(user, number="LC1", **kw):
    return TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name="DemoBroker",
        is_demo=True, is_active=False, **kw)


def _runtime(acct, state):
    return AccountRuntime.objects.create(
        trading_account=acct, cohort=AccountRuntime.Cohort.BETA, state=state)


class AccountLifecycleTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="lc", email="lc@x.invalid", password="x")

    def _lc(self, account):
        return build_account_status(account)["lifecycle"]

    def _keys(self, lc):
        return [s["key"] for s in lc["steps"]]

    def test_no_runtime_is_account_received(self):
        lc = self._lc(_acct(self.user))
        self.assertEqual(lc["phase"], "account_received")
        self.assertFalse(lc["retryable"])
        self.assertEqual(lc["steps"][0]["status"], "current")

    def test_provisioning_runtime(self):
        acct = _acct(self.user)
        _runtime(acct, RuntimeState.PROVISIONING)
        self.assertEqual(self._lc(acct)["phase"], "provisioning_runtime")

    @override_settings(PROVISIONING_REQUIRE_BROKER_LOGIN=False)
    def test_running_without_broker_login_required_is_validated(self):
        acct = _acct(self.user)
        _runtime(acct, RuntimeState.RUNNING)
        lc = self._lc(acct)
        self.assertEqual(lc["phase"], "validated")
        self.assertNotIn("connecting_broker", self._keys(lc))   # phase absent when not required
        self.assertTrue(all(s["status"] == "done" for s in lc["steps"]))

    @override_settings(PROVISIONING_REQUIRE_BROKER_LOGIN=True)
    def test_running_with_broker_login_required_is_connecting(self):
        acct = _acct(self.user)   # validation_status default NEVER
        _runtime(acct, RuntimeState.RUNNING)
        lc = self._lc(acct)
        self.assertEqual(lc["phase"], "connecting_broker")
        self.assertIn("connecting_broker", self._keys(lc))

    @override_settings(PROVISIONING_REQUIRE_BROKER_LOGIN=True)
    def test_running_with_validated_credentials_is_validated(self):
        acct = _acct(self.user, validation_status=TradingAccount.ValidationStatus.VALIDATED)
        _runtime(acct, RuntimeState.RUNNING)
        self.assertEqual(self._lc(acct)["phase"], "validated")

    def test_failed_runtime_is_connection_failed_and_retryable(self):
        acct = _acct(self.user)
        _runtime(acct, RuntimeState.FAILED)
        lc = self._lc(acct)
        self.assertEqual(lc["phase"], "connection_failed")
        self.assertTrue(lc["retryable"])
        self.assertTrue(any(s["status"] == "failed" for s in lc["steps"]))

    def test_connection_failed_validation_is_retryable(self):
        acct = _acct(self.user, validation_status=TradingAccount.ValidationStatus.CONNECTION_FAILED)
        _runtime(acct, RuntimeState.RUNNING)
        lc = self._lc(acct)
        self.assertEqual(lc["phase"], "connection_failed")
        self.assertTrue(lc["retryable"])
