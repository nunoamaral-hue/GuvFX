"""Tests for the provider-driven broker-validation abstraction (trading.brokers)."""
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from trading.models import BrokerServer, TradingAccount
from trading.crypto import encrypt_password
from trading.brokers import (
    BrokerValidationResult, FailClosedValidator, broker_family,
    get_broker_validator, get_validator_for_family)
from trading.brokers.mt5 import Mt5BrokerValidator

U = get_user_model()


def _acct(account_number="12345", broker_name="DemoBroker", broker_server=None):
    user = U.objects.create_user(username=f"u-{account_number}-{broker_name}",
                                 email=f"{account_number}@x.invalid", password="x")
    return TradingAccount.objects.create(
        user=user, name="A", account_number=account_number, broker_name=broker_name,
        broker_server=broker_server, is_demo=True, password_enc=encrypt_password("pw"))


class Mt5ValidatorTests(TestCase):
    def test_valid_record_ok_with_normalised_values(self):
        r = Mt5BrokerValidator().validate_account_record(_acct("100200", "DemoBroker"))
        self.assertTrue(r.ok)
        self.assertEqual(r.reason_code, "ok")
        self.assertEqual(r.normalized_login, "100200")
        self.assertEqual(r.normalized_server, "DemoBroker")

    def test_non_numeric_login_rejected(self):
        r = Mt5BrokerValidator().validate_account_record(_acct("not-a-number"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, "invalid_login")

    def test_non_positive_login_rejected(self):
        r = Mt5BrokerValidator().validate_account_record(_acct("0"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, "invalid_login")

    def test_missing_server_rejected(self):
        r = Mt5BrokerValidator().validate_account_record(_acct("100200", broker_name=""))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, "missing_server")

    def test_broker_server_name_takes_precedence(self):
        bs = BrokerServer.objects.create(broker_display_name="Demo", server_name="MetaQuotes-Demo")
        r = Mt5BrokerValidator().validate_account_record(
            _acct("100200", broker_name="ignored-free-text", broker_server=bs))
        self.assertTrue(r.ok)
        self.assertEqual(r.normalized_server, "MetaQuotes-Demo")


class RegistryTests(TestCase):
    def test_account_resolves_to_mt5_provider(self):
        v = get_broker_validator(_acct())
        self.assertEqual(v.key, "mt5")

    def test_family_defaults_to_mt5(self):
        self.assertEqual(broker_family(_acct()), "mt5")

    def test_unregistered_family_is_fail_closed(self):
        v = get_validator_for_family("no-such-broker")
        self.assertIsInstance(v, FailClosedValidator)
        r = v.validate_account_record(_acct())
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, "unsupported_broker")

    def test_result_is_frozen(self):
        r = BrokerValidationResult(True)
        with self.assertRaises(Exception):
            r.ok = False


@override_settings(BETA_RUNTIMES_ENABLED=True)
class ReservationConsumesAbstractionTests(TestCase):
    """The beta reservation path consumes the abstraction fail-closed: a malformed broker record is
    BLOCKED and never consumes a pool slot."""
    def test_malformed_record_blocks_and_denies_reservation(self):
        from terminal_provisioning import beta_capacity as cap
        from terminal_provisioning.models import RuntimeState
        acct = _acct("not-a-number")
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(acct)
        self.assertEqual(ctx.exception.reason_code, "broker_record_invalid")
        rt = cap.get_or_create_beta_runtime(acct)
        self.assertEqual(rt.state, RuntimeState.BLOCKED)
        # ...and it never counted against the pool.
        self.assertEqual(cap.active_beta_runtime_count(), 0)

    def test_valid_record_reserves_normally(self):
        from terminal_provisioning import beta_capacity as cap
        from terminal_provisioning.models import RuntimeState
        rt = cap.reserve_beta_slot(_acct("100200"))
        self.assertEqual(rt.state, RuntimeState.QUEUED)
