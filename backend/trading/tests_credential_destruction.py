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

    def test_delete_account_destroys_and_audits_credential(self):
        acct = _acct(self.user, number="55446633")
        acct_id = acct.id
        req = self.factory.delete(f"/api/accounts/{acct_id}/")
        force_authenticate(req, user=self.user)
        resp = TradingAccountViewSet.as_view({"delete": "destroy"})(req, pk=acct_id)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(TradingAccount.objects.filter(id=acct_id).exists())   # row gone
        # the destruction evidence survives the row deletion (append-only audit)
        audit = AuditEvent.objects.get(event_type="CREDENTIAL_DESTROYED", entity_id=str(acct_id))
        self.assertEqual(audit.metadata["account_number_suffix"], "****6633")

    def test_refused_delete_rolls_back_destruction_and_audit(self):
        # The load-bearing safety claim: deleting an account with a PROTECTed AccountProvisioning is
        # refused, and the credential clear + its DESTROYED audit roll back with it (no partial
        # destruction). Locks in behaviour a future edit could silently break.
        from django.db.models import ProtectedError
        from terminal_provisioning.models import AccountProvisioning

        acct = _acct(self.user, number="77665544")
        original_ct = acct.password_enc
        AccountProvisioning.objects.create(
            trading_account=acct, windows_username="guvfx_u_prot", runtime_root="C:/GuvFX/accounts/prot")
        req = self.factory.delete(f"/api/accounts/{acct.id}/")
        force_authenticate(req, user=self.user)
        with self.assertRaises(ProtectedError):
            TradingAccountViewSet.as_view({"delete": "destroy"})(req, pk=acct.id)
        acct.refresh_from_db()
        self.assertTrue(TradingAccount.objects.filter(id=acct.id).exists())   # account survived
        self.assertEqual(acct.password_enc, original_ct)                       # clear rolled back
        self.assertEqual(AuditEvent.objects.filter(                           # no durable audit row
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
