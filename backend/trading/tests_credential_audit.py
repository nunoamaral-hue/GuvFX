"""Phase 3 (P3-B) — customer-credential audit trail.

Proves the redacted, secret-safe audit for a customer broker credential: correct event types +
severities, no plaintext ever recorded, last-4-only redaction, intake (CREATED) + rotation
(ROTATED) wiring on the serializer, the re-encryption command's key-rotation audit, and fail-open.
"""
import json
import os
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from core.audit import CREDENTIAL_ACTIONS, log_customer_credential_event
from core.models import AuditEvent
from trading.models import TradingAccount
from trading.serializers import TradingAccountSerializer

U = get_user_model()

_EXPLICIT = {"GUVFX_FERNET_KEY": Fernet.generate_key().decode(), "DJANGO_SECRET_KEY": "unit-test-secret"}


def _acct(user, number="12345678", pw_enc="x"):
    return TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name="DemoBroker",
        is_demo=True, password_enc=pw_enc)


def _dump(ev):
    return json.dumps(ev.metadata or {})


class CustomerCredentialAuditHelperTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="au", email="au@x.invalid", password="x")

    def test_vocabulary_includes_full_lifecycle(self):
        for a in ("CREATED", "VERIFIED", "ACCESSED", "ROTATED", "REVOKED", "DESTROYED"):
            self.assertIn(a, CREDENTIAL_ACTIONS)

    def test_access_event_is_redacted_and_carries_purpose(self):
        acct = _acct(self.user, number="99887766")
        log_customer_credential_event("ACCESSED", account=acct, actor="op", purpose="send-to-agent")
        ev = AuditEvent.objects.get(event_type="CREDENTIAL_ACCESSED", entity_id=str(acct.id))
        self.assertEqual(ev.entity_type, "TradingAccount")
        self.assertEqual(ev.severity, "INFO")
        self.assertEqual(ev.metadata["account_number_suffix"], "****7766")
        self.assertEqual(ev.metadata["purpose"], "send-to-agent")
        self.assertNotIn("99887766", _dump(ev))          # full number never recorded

    def test_revoked_and_destroyed_are_warn(self):
        acct = _acct(self.user)
        log_customer_credential_event("REVOKED", account=acct)
        log_customer_credential_event("DESTROYED", account=acct)
        self.assertEqual(
            AuditEvent.objects.get(event_type="CREDENTIAL_REVOKED", entity_id=str(acct.id)).severity, "WARN")
        self.assertEqual(
            AuditEvent.objects.get(event_type="CREDENTIAL_DESTROYED", entity_id=str(acct.id)).severity, "WARN")

    def test_sanitizer_scrubs_a_mistakenly_passed_secret(self):
        acct = _acct(self.user)
        # A caller must never pass a secret, but if one slips through as detail it is redacted.
        log_customer_credential_event("ACCESSED", account=acct, password="hunter2", token="abc")
        ev = AuditEvent.objects.get(event_type="CREDENTIAL_ACCESSED", entity_id=str(acct.id))
        self.assertNotIn("hunter2", _dump(ev))
        self.assertNotIn("abc", _dump(ev))
        self.assertEqual(ev.metadata["password"], "[REDACTED]")

    def test_fail_open_on_none_account(self):
        # Must never raise even with a missing account.
        log_customer_credential_event("ACCESSED", account=None, purpose="x")
        self.assertTrue(AuditEvent.objects.filter(
            event_type="CREDENTIAL_ACCESSED", entity_id="unknown").exists())

    def test_fail_open_on_reserved_kwarg_collision(self):
        # A caller passing a reserved dispatch kwarg as detail must NOT raise (fail-open) and must
        # not corrupt the redacted reference — entity_id stays the account id, not the caller value.
        acct = _acct(self.user, number="55554444")
        log_customer_credential_event(
            "ACCESSED", account=acct, entity_id="attacker", entity_type="Spoof",
            actor="op", request=None)
        ev = AuditEvent.objects.get(event_type="CREDENTIAL_ACCESSED", entity_id=str(acct.id))
        self.assertEqual(ev.entity_type, "TradingAccount")

    def test_caller_detail_cannot_override_redaction(self):
        # Redaction must win: a caller cannot substitute the masked suffix with the full number.
        acct = _acct(self.user, number="12349999")
        log_customer_credential_event(
            "ACCESSED", account=acct, account_number_suffix="12349999", account_id="spoof")
        ev = AuditEvent.objects.get(event_type="CREDENTIAL_ACCESSED", entity_id=str(acct.id))
        self.assertEqual(ev.metadata["account_number_suffix"], "****9999")
        self.assertEqual(ev.metadata["account_id"], acct.id)
        self.assertNotIn("12349999", _dump(ev))

    def test_short_account_number_is_fully_masked(self):
        # A number of <=4 chars must be masked fully, not shown in whole by a bare [-4:] slice.
        acct = _acct(self.user, number="99")
        log_customer_credential_event("ACCESSED", account=acct)
        ev = AuditEvent.objects.get(event_type="CREDENTIAL_ACCESSED", entity_id=str(acct.id))
        self.assertEqual(ev.metadata["account_number_suffix"], "****")
        self.assertNotIn("99", ev.metadata["account_number_suffix"])


class IntakeAuditWiringTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="iu", email="iu@x.invalid", password="x")

    def test_create_with_password_emits_created_and_no_plaintext(self):
        with mock.patch.dict(os.environ, _EXPLICIT):
            ser = TradingAccountSerializer(data={
                "name": "Acct", "account_number": "10203040", "broker_name": "DemoBroker",
                "is_demo": True, "password": "hunter2"})
            self.assertTrue(ser.is_valid(), ser.errors)
            acct = ser.save(user=self.user)
        ev = AuditEvent.objects.get(event_type="CREDENTIAL_CREATED", entity_id=str(acct.id))
        self.assertEqual(ev.metadata["purpose"], "intake")
        self.assertEqual(ev.metadata["account_number_suffix"], "****3040")
        self.assertNotIn("hunter2", _dump(ev))

    def test_update_with_new_password_emits_rotated(self):
        with mock.patch.dict(os.environ, _EXPLICIT):
            acct = _acct(self.user)
            ser = TradingAccountSerializer(acct, data={"password": "newpw"}, partial=True)
            self.assertTrue(ser.is_valid(), ser.errors)
            ser.save()
        self.assertTrue(AuditEvent.objects.filter(
            event_type="CREDENTIAL_ROTATED", entity_id=str(acct.id)).exists())

    def test_update_without_password_emits_no_credential_event(self):
        with mock.patch.dict(os.environ, _EXPLICIT):
            acct = _acct(self.user)
            ser = TradingAccountSerializer(acct, data={"name": "Renamed"}, partial=True)
            self.assertTrue(ser.is_valid(), ser.errors)
            ser.save()
        self.assertFalse(AuditEvent.objects.filter(
            entity_type="TradingAccount", entity_id=str(acct.id)).exists())


class ReencryptCommandAuditTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="ru", email="ru@x.invalid", password="x")

    def test_command_emits_key_rotation_audit(self):
        with mock.patch.dict(os.environ, {"GUVFX_FERNET_KEY": "", "GUVFX_FERNET_KEYS": "",
                                          "DJANGO_SECRET_KEY": "unit-test-secret"}):
            from trading.crypto import encrypt_password
            _acct(self.user, pw_enc=encrypt_password("pw"))
        with mock.patch.dict(os.environ, _EXPLICIT):
            call_command("reencrypt_customer_credentials")
        ev = AuditEvent.objects.get(
            event_type="CREDENTIAL_ROTATED", entity_type="CustomerCredentialKeyMaterial")
        self.assertEqual(ev.metadata["reencrypted"], 1)
        self.assertEqual(ev.metadata["failed"], 0)

    def test_dry_run_emits_no_rotation_audit(self):
        with mock.patch.dict(os.environ, {"GUVFX_FERNET_KEY": "", "GUVFX_FERNET_KEYS": "",
                                          "DJANGO_SECRET_KEY": "unit-test-secret"}):
            from trading.crypto import encrypt_password
            _acct(self.user, pw_enc=encrypt_password("pw"))
        with mock.patch.dict(os.environ, _EXPLICIT):
            call_command("reencrypt_customer_credentials", "--dry-run")
        self.assertFalse(AuditEvent.objects.filter(
            event_type="CREDENTIAL_ROTATED", entity_type="CustomerCredentialKeyMaterial").exists())
