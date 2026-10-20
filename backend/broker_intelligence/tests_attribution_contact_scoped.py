"""MULTI_ACCOUNT §5 — contact-scoped transaction-account attribution (DARK).

Proves resolve_transaction_account(..., identity=): flag OFF is byte-identical (number-only); flag ON narrows the
broker-stated-number match to the identity's VERIFIED-bound account(s) — resolving a shared email to the ONE bound
account, leaving an un-bound number match UNRESOLVED (never assigning by inbox/identity ownership alone), and falling
back to number-only when no identity is attributed.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from broker_intelligence.models import BrokerEmailIdentity, ConnectedMailbox, TradingAccountBrokerContact
from broker_intelligence.resolver import resolve_transaction_account
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "97%04d" % _n


ON = override_settings(BROKER_ATTRIBUTION_CONTACT_SCOPED_ENABLED="1")


class ContactScopedAttribution(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="cs%s" % _uniq(), email="cs@x.invalid", password="x")

    def _acct(self, number, broker="TradersWay"):
        return TradingAccount.objects.create(user=self.u, name="A", broker_name=broker, account_number=number,
                                             is_demo=False, is_active=True)

    def _identity(self, email="id@gmail.com", broker="TradersWay"):
        return BrokerEmailIdentity.objects.create(email=email, broker_name=broker, user=self.u)

    def _verified_binding(self, account, identity):
        # A VERIFIED binding must satisfy the migration-0015 trigger: non-null mailbox owned by the account owner +
        # identity owned by the account owner.
        mb = ConnectedMailbox.objects.create(user=self.u, provider=ConnectedMailbox.Provider.GMAIL,
                                             provider_mailbox_id="m%s@gmail.com" % _uniq(),
                                             primary_email="m@x.invalid", status=ConnectedMailbox.Status.CONNECTED)
        return TradingAccountBrokerContact.objects.create(
            trading_account=account, broker_email_identity=identity, connected_mailbox=mb,
            status=TradingAccountBrokerContact.Status.VERIFIED)

    def test_flag_off_is_number_only(self):
        a = self._acct("111")
        idn = self._identity()
        self.assertEqual(resolve_transaction_account(self.u, "TradersWay", ["111"], identity=idn), a)   # binding ignored

    @ON
    def test_flag_on_resolves_the_bound_in_number_account(self):
        a = self._acct("111")
        idn = self._identity()
        self._verified_binding(a, idn)
        self.assertEqual(resolve_transaction_account(self.u, "TradersWay", ["111"], identity=idn), a)

    @ON
    def test_binding_narrows_an_otherwise_ambiguous_number_match(self):
        a1 = self._acct("111")
        self._acct("222")                 # a2: the OTHER stated number also matches an owned account
        idn = self._identity()
        self._verified_binding(a1, idn)   # only a1 is VERIFIED-bound to this identity
        self.assertEqual(resolve_transaction_account(self.u, "TradersWay", ["111", "222"], identity=idn), a1)

    def test_ambiguous_number_match_without_flag_is_unresolved(self):
        a1 = self._acct("111")
        self._acct("222")
        idn = self._identity()
        self._verified_binding(a1, idn)
        self.assertIsNone(resolve_transaction_account(self.u, "TradersWay", ["111", "222"], identity=idn))  # flag OFF

    @ON
    def test_number_match_with_no_binding_is_unresolved(self):
        self._acct("111")
        idn = self._identity()            # no VERIFIED binding for this identity
        self.assertIsNone(resolve_transaction_account(self.u, "TradersWay", ["111"], identity=idn))

    @ON
    def test_no_identity_falls_back_to_number_only(self):
        a = self._acct("111")
        self.assertEqual(resolve_transaction_account(self.u, "TradersWay", ["111"], identity=None), a)

    @ON
    def test_binding_never_substitutes_for_a_number_match(self):
        # A VERIFIED binding to an account whose number the broker did NOT state must NOT be resolved — the binding
        # only narrows a number match, it never assigns an account absent a stated-number match.
        a_bound = self._acct("111")
        idn = self._identity()
        self._verified_binding(a_bound, idn)
        self.assertIsNone(resolve_transaction_account(self.u, "TradersWay", ["999"], identity=idn))   # 999 matches nothing
