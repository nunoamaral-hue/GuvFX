"""MULTI_ACCOUNT_PUBLIC_METRICS §A/§C — TradingAccountBrokerContact association model (DARK, additive).

Proves the Sponsor's many-to-many requirements that the legacy `BrokerEmailIdentity (email, broker_name)` unique
constraint blocked: ONE (email, broker) serving MANY accounts; ONE inbox serving MANY accounts/brokers; credentials
not duplicated per account; per-binding verification; history retained (RETIRE, never delete) on disconnect; the
audit is append-only. Additive + non-destructive: the legacy single-FK bindings on BrokerEmailIdentity are untouched.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from broker_intelligence.models import (BrokerEmailIdentity, ConnectedMailbox, TradingAccountBrokerContact,
                                        TradingAccountBrokerContactAudit)
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "92%04d" % _n


def _acct(user, **kw):
    login = _uniq()
    d = dict(user=user, name="A", broker_name="TradersWay", account_number=login, is_demo=False, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


def _identity(email, broker="TradersWay", **kw):
    return BrokerEmailIdentity.objects.create(email=email, broker_name=broker, **kw)


def _mailbox(user, pmid):
    return ConnectedMailbox.objects.create(user=user, provider=ConnectedMailbox.Provider.GMAIL,
                                           provider_mailbox_id=pmid, primary_email=pmid,
                                           status=ConnectedMailbox.Status.CONNECTED)


class AccountBrokerContactModel(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="abc", email="abc@x.invalid", password="x")

    def test_one_email_at_one_broker_serves_many_accounts(self):
        # The core requirement blocked by the (email, broker_name) unique on BrokerEmailIdentity: one registry row,
        # many account bindings.
        idn = _identity("shared@googlemail.com", broker="TradersWay")
        a1, a2, a3 = _acct(self.u), _acct(self.u), _acct(self.u)
        for a in (a1, a2, a3):
            TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn)
        self.assertEqual(idn.account_contacts.count(), 3)                      # EMAIL 1 -> MANY ACCOUNTS
        self.assertEqual(BrokerEmailIdentity.objects.filter(email="shared@googlemail.com").count(), 1)  # one registry row

    def test_one_inbox_serves_many_accounts_without_duplicating_credentials(self):
        mb = _mailbox(self.u, "inbox@gmail.com")   # ONE mailbox holds the credential ONCE
        i_tw = _identity("shared@googlemail.com", broker="TradersWay")
        i_is6 = _identity("shared@googlemail.com", broker="IS6")        # same email, different broker => distinct row
        a1, a2 = _acct(self.u), _acct(self.u, broker_name="IS6")
        TradingAccountBrokerContact.objects.create(trading_account=a1, broker_email_identity=i_tw, connected_mailbox=mb)
        TradingAccountBrokerContact.objects.create(trading_account=a2, broker_email_identity=i_is6, connected_mailbox=mb)
        self.assertEqual(mb.account_contacts.count(), 2)                 # ONE INBOX -> MANY ACCOUNTS/BROKERS
        self.assertEqual(ConnectedMailbox.objects.filter(user=self.u).count(), 1)   # credential not duplicated

    def test_duplicate_account_identity_pair_is_rejected(self):
        idn = _identity("dup@gmail.com")
        a = _acct(self.u)
        TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TradingAccountBrokerContact.objects.create(trading_account=a, broker_email_identity=idn)

    def test_per_binding_verification_default_pending(self):
        idn = _identity("v@gmail.com")
        c = TradingAccountBrokerContact.objects.create(trading_account=_acct(self.u), broker_email_identity=idn)
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.PENDING)   # mailbox control alone != verified

    def test_retire_preserves_binding_and_is_not_a_delete(self):
        idn = _identity("r@gmail.com")
        c = TradingAccountBrokerContact.objects.create(trading_account=_acct(self.u), broker_email_identity=idn)
        c.status = TradingAccountBrokerContact.Status.RETIRED
        c.save(update_fields=["status", "updated_at"])
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.RETIRED)
        self.assertTrue(TradingAccountBrokerContact.objects.filter(pk=c.pk).exists())   # retained, never deleted

    def test_legacy_identity_bindings_untouched(self):
        # Non-destructive: a legacy BrokerEmailIdentity.trading_account binding still exists alongside the new table.
        a = _acct(self.u)
        idn = _identity("legacy@gmail.com", user=self.u, trading_account=a,
                        status=BrokerEmailIdentity.Status.VERIFIED)
        self.assertEqual(idn.trading_account_id, a.id)                  # legacy single-FK binding intact
        self.assertEqual(idn.account_contacts.count(), 0)               # new table is additive, not a migration of it


class AccountBrokerContactAudit(TestCase):
    def test_audit_is_append_only(self):
        u = User.objects.create_user(username="aa", email="aa@x.invalid", password="x")
        idn = _identity("a@gmail.com")
        c = TradingAccountBrokerContact.objects.create(trading_account=_acct(u), broker_email_identity=idn)
        row = TradingAccountBrokerContactAudit.objects.create(
            contact=c, action=TradingAccountBrokerContactAudit.Action.BIND, subject_user=u,
            outcome="OK", status_after="PENDING", proofs={"account_owned": True})
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            row.delete()
