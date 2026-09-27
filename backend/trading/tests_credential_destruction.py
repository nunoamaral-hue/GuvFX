"""Phase 3 (P3-D) — verified customer-credential destruction (secure clear + audit evidence)."""
import json
import os
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import AuditEvent
from trading.credential_lifecycle import destroy_customer_credential
from trading.crypto import encrypt_password
from trading.models import TradingAccount
from trading.views import TradingAccountViewSet

U = get_user_model()
_KEY = {"GUVFX_FERNET_KEY": Fernet.generate_key().decode(), "DJANGO_SECRET_KEY": "unit-test-secret"}


def _acct(user, number="11223344", pw="brokerpw"):
    with mock.patch.dict(os.environ, _KEY):
        return TradingAccount.objects.create(
            user=user, name="A", account_number=number, broker_name="DemoBroker",
            is_demo=True, is_active=True, password_enc=(encrypt_password(pw) if pw else ""))


class DestroyServiceTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="du", email="du@x.invalid", password="x")

    def test_destroy_clears_and_audits_with_evidence(self):
        acct = _acct(self.user, number="99887766")
        ev = destroy_customer_credential(acct, actor="operator")
        acct.refresh_from_db()
        self.assertEqual(acct.password_enc, "")
        self.assertEqual(acct.broker_password, "")
        self.assertTrue(ev["had_credential"])
        self.assertEqual(ev["method"], "secure-clear")
        self.assertIn("password_enc", ev["cleared_fields"])
        audit = AuditEvent.objects.get(event_type="CREDENTIAL_DESTROYED", entity_id=str(acct.id))
        self.assertEqual(audit.severity, "WARN")
        self.assertTrue(audit.metadata["had_credential"])
        self.assertEqual(audit.metadata["account_number_suffix"], "****7766")
        self.assertNotIn("brokerpw", json.dumps(audit.metadata or {}))

    def test_destroy_is_idempotent(self):
        acct = _acct(self.user)
        destroy_customer_credential(acct, actor="operator")
        ev2 = destroy_customer_credential(acct, actor="operator")   # second call: nothing left
        self.assertFalse(ev2["had_credential"])
        self.assertEqual(ev2["cleared_fields"], [])
        # both destruction actions are recorded (append-only)
        self.assertEqual(
            AuditEvent.objects.filter(event_type="CREDENTIAL_DESTROYED", entity_id=str(acct.id)).count(), 2)

    def test_destroy_on_empty_credential_records_no_credential(self):
        acct = _acct(self.user, pw="")
        ev = destroy_customer_credential(acct, actor="operator")
        self.assertFalse(ev["had_credential"])


class PerformDestroyWiringTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="pd", email="pd@x.invalid", password="x")
        self.factory = APIRequestFactory()

    def test_delete_account_via_api_is_blocked_405(self):
        # History-safety: the hard-delete API path is now CLOSED (destroy -> 405). A DELETE must not remove the
        # row, must not destroy the credential (destroy short-circuits before perform_destroy), and must not
        # write a CREDENTIAL_DESTROYED audit. Member removal goes through the history-retaining tombstone, which
        # DOES destroy the credential (covered in tests_account_removal + tests_broker_connectivity).
        acct = _acct(self.user, number="55446633")
        acct_id = acct.id
        original_ct = acct.password_enc
        req = self.factory.delete(f"/api/accounts/{acct_id}/")
        force_authenticate(req, user=self.user)
        resp = TradingAccountViewSet.as_view({"delete": "destroy"})(req, pk=acct_id)
        self.assertEqual(resp.status_code, 405)                                # hard-delete refused
        self.assertTrue(TradingAccount.objects.filter(id=acct_id).exists())    # row RETAINED
        acct.refresh_from_db()
        self.assertEqual(acct.password_enc, original_ct)                       # credential untouched
        self.assertEqual(AuditEvent.objects.filter(
            event_type="CREDENTIAL_DESTROYED", entity_id=str(acct_id)).count(), 0)

    def test_delete_blocked_even_for_provisioned_account(self):
        # A fully-provisioned account (PROTECTed AccountProvisioning) is also refused at 405 — the closed API
        # path never reaches the ORM, so provisioning + credential + row all survive intact.
        from terminal_provisioning.models import AccountProvisioning
        acct = _acct(self.user, number="77665544")
        original_ct = acct.password_enc
        AccountProvisioning.objects.create(
            trading_account=acct, windows_username="guvfx_u_prot", runtime_root="C:/GuvFX/accounts/prot")
        req = self.factory.delete(f"/api/accounts/{acct.id}/")
        force_authenticate(req, user=self.user)
        resp = TradingAccountViewSet.as_view({"delete": "destroy"})(req, pk=acct.id)
        self.assertEqual(resp.status_code, 405)
        acct.refresh_from_db()
        self.assertTrue(TradingAccount.objects.filter(id=acct.id).exists())
        self.assertEqual(acct.password_enc, original_ct)
        self.assertEqual(AuditEvent.objects.filter(
            event_type="CREDENTIAL_DESTROYED", entity_id=str(acct.id)).count(), 0)


class DestroyCommandTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="cd", email="cd@x.invalid", password="x")

    def test_command_destroys_named_account(self):
        acct = _acct(self.user)
        call_command("destroy_customer_credential", "--account-id", str(acct.id))
        acct.refresh_from_db()
        self.assertEqual(acct.password_enc, "")
        self.assertTrue(AuditEvent.objects.filter(
            event_type="CREDENTIAL_DESTROYED", entity_id=str(acct.id)).exists())

    def test_command_errors_on_missing_account(self):
        with self.assertRaises(CommandError):
            call_command("destroy_customer_credential", "--account-id", "99999999")
