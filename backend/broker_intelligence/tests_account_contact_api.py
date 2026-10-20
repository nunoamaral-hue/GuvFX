"""MULTI_ACCOUNT §A/§C/§4 — the account-level broker-email CONTACT HTTP API (DARK).

Proves: DARK 404 when the flag is off; owner-scoping is the cross-user firewall (foreign account/contact => 404);
create is idempotent and lets ONE (email, broker) bind to MANY of the member's accounts; a DIFFERENT member's
registered address is refused generically (no existence leak); verify reuses the fail-closed contact_verification
writer (VERIFIED only on composed proof, else UNRESOLVED) and NEVER auto-verifies.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from broker_intelligence.models import (BrokerEmailIdentity, ConnectedMailbox, TradingAccountBrokerContact)
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "96%04d" % _n


def _acct(user, broker="TradersWay"):
    return TradingAccount.objects.create(user=user, name="A", broker_name=broker, account_number=_uniq(),
                                         is_demo=False, is_active=True)


def _mb(user, pmid=None, status=ConnectedMailbox.Status.CONNECTED):
    pmid = pmid or ("mb%s@gmail.com" % _uniq())
    return ConnectedMailbox.objects.create(user=user, provider=ConnectedMailbox.Provider.GMAIL,
                                           provider_mailbox_id=pmid, primary_email=pmid, status=status)


class AccountContactApiDark(TestCase):
    def test_all_routes_404_when_flag_off(self):
        u = User.objects.create_user(username="d%s" % _uniq(), email="d@x.invalid", password="x")
        a = _acct(u)
        c = APIClient(); c.force_authenticate(u)
        base = f"/api/broker-intelligence/accounts/{a.id}/contacts/"
        self.assertEqual(c.get(base).status_code, 404)
        self.assertEqual(c.post(base + "create/", {"email": "x@gmail.com", "broker": "TradersWay"}).status_code, 404)
        self.assertEqual(c.post(base + "1/verify/").status_code, 404)


@override_settings(BROKER_ACCOUNT_CONTACT_ENABLED="1")
class AccountContactApi(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="u%s" % _uniq(), email="u@x.invalid", password="x")
        self.a = _acct(self.u)
        self.c = APIClient(); self.c.force_authenticate(self.u)

    def _base(self, acct=None):
        return f"/api/broker-intelligence/accounts/{(acct or self.a).id}/contacts/"

    def test_create_then_list_masked_and_pending(self):
        r = self.c.post(self._base() + "create/", {"email": "trader@gmail.com", "broker": "TradersWay"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["contact"]["status"], "PENDING")      # NEVER auto-verified
        self.assertNotIn("trader@gmail.com", r.content.decode())        # masked, never the raw address
        lst = self.c.get(self._base())
        self.assertEqual(lst.status_code, 200)
        self.assertEqual(len(lst.json()["contacts"]), 1)

    def test_create_is_idempotent(self):
        payload = {"email": "trader@gmail.com", "broker": "TradersWay"}
        r1 = self.c.post(self._base() + "create/", payload)
        r2 = self.c.post(self._base() + "create/", payload)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)                           # second is a no-op (same pair)
        self.assertEqual(TradingAccountBrokerContact.objects.filter(trading_account=self.a).count(), 1)

    def test_one_email_binds_to_many_owned_accounts(self):
        a2 = _acct(self.u)
        payload = {"email": "shared@gmail.com", "broker": "TradersWay"}
        self.assertEqual(self.c.post(self._base() + "create/", payload).status_code, 201)
        self.assertEqual(self.c.post(self._base(a2) + "create/", payload).status_code, 201)
        self.assertEqual(TradingAccountBrokerContact.objects.filter(
            broker_email_identity__email="shared@gmail.com").count(), 2)           # EMAIL 1 -> MANY ACCOUNTS
        self.assertEqual(BrokerEmailIdentity.objects.filter(email="shared@gmail.com").count(), 1)  # one registry row

    def test_foreign_account_is_404(self):
        other = User.objects.create_user(username="o%s" % _uniq(), email="o@x.invalid", password="x")
        foreign = _acct(other)
        self.assertEqual(self.c.get(self._base(foreign)).status_code, 404)        # owner firewall (no leak)
        self.assertEqual(self.c.post(self._base(foreign) + "create/",
                                     {"email": "x@gmail.com", "broker": "TradersWay"}).status_code, 404)

    def test_cross_user_registered_address_refused_generically(self):
        other = User.objects.create_user(username="o%s" % _uniq(), email="o@x.invalid", password="x")
        BrokerEmailIdentity.objects.create(email="owned@gmail.com", broker_name="TradersWay", user=other)
        r = self.c.post(self._base() + "create/", {"email": "owned@gmail.com", "broker": "TradersWay"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"], "cannot_associate")                   # no existence leak
        self.assertFalse(TradingAccountBrokerContact.objects.filter(trading_account=self.a).exists())

    def test_verify_success_on_composed_proof(self):
        mb = _mb(self.u)
        self.c.post(self._base() + "create/",
                    {"email": "trader@gmail.com", "broker": "TradersWay", "connected_mailbox_id": mb.id})
        contact = TradingAccountBrokerContact.objects.get(trading_account=self.a)
        r = self.c.post(self._base() + f"{contact.id}/verify/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["verified"])
        self.assertEqual(r.json()["status"], "VERIFIED")
        contact.refresh_from_db()
        self.assertEqual(contact.status, TradingAccountBrokerContact.Status.VERIFIED)

    def test_verify_fail_closed_without_mailbox(self):
        self.c.post(self._base() + "create/", {"email": "trader@gmail.com", "broker": "TradersWay"})  # no mailbox
        contact = TradingAccountBrokerContact.objects.get(trading_account=self.a)
        r = self.c.post(self._base() + f"{contact.id}/verify/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["verified"])
        self.assertEqual(r.json()["status"], "UNRESOLVED")                        # fail-closed, never VERIFIED
        self.assertEqual(r.json()["reason"], "no_connected_mailbox")

    def test_verify_broker_mismatch_fail_closed(self):
        mb = _mb(self.u)
        self.c.post(self._base() + "create/",
                    {"email": "trader@gmail.com", "broker": "IS6", "connected_mailbox_id": mb.id})  # account is TradersWay
        contact = TradingAccountBrokerContact.objects.get(trading_account=self.a)
        r = self.c.post(self._base() + f"{contact.id}/verify/")
        self.assertFalse(r.json()["verified"])
        self.assertEqual(r.json()["reason"], "broker_mismatch")

    def test_verify_foreign_contact_is_404(self):
        other = User.objects.create_user(username="o%s" % _uniq(), email="o@x.invalid", password="x")
        oa = _acct(other)
        oi = BrokerEmailIdentity.objects.create(email="f@gmail.com", broker_name="TradersWay", user=other)
        oc = TradingAccountBrokerContact.objects.create(trading_account=oa, broker_email_identity=oi)
        # try to verify another member's contact via this member's account path
        r = self.c.post(self._base() + f"{oc.id}/verify/")
        self.assertEqual(r.status_code, 404)
