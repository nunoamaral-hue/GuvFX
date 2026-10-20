"""MULTI_ACCOUNT §2 — routing-evidence requirement for a VERIFIED broker-email binding (DARK).

Proves that with BROKER_ROUTING_EVIDENCE_REQUIRED off, a same-owner CONNECTED mailbox still verifies (byte-identical);
with it on, a binding needs independently-supportable routing proof: the bound mailbox IS the broker-registered
address (direct), or an AUTHENTICATED broker delivery was observed through it (observed_delivery). A mere same-owner
mailbox that is neither is 'routing_unproven' (fail-closed). The migration-0015 ownership trigger is unaffected.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import contact_verification
from broker_intelligence.models import (BrokerEmailIdentity, BrokerEvent, BrokerMessageObservation, ConnectedMailbox,
                                         Provenance, TradingAccountBrokerContact, TransactionCategory)
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "98%04d" % _n


ON = override_settings(BROKER_ROUTING_EVIDENCE_REQUIRED="1")


class RoutingEvidence(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="re%s" % _uniq(), email="re@x.invalid", password="x")
        self.a = TradingAccount.objects.create(user=self.u, name="A", broker_name="TradersWay",
                                               account_number=_uniq(), is_demo=False, is_active=True)

    def _mb(self, email):
        return ConnectedMailbox.objects.create(user=self.u, provider=ConnectedMailbox.Provider.GMAIL,
                                               provider_mailbox_id=email, primary_email=email,
                                               status=ConnectedMailbox.Status.CONNECTED)

    def _contact(self, *, identity_email, mailbox):
        idn = BrokerEmailIdentity.objects.create(email=identity_email, broker_name="TradersWay", user=self.u)
        return TradingAccountBrokerContact.objects.create(trading_account=self.a, broker_email_identity=idn,
                                                         connected_mailbox=mailbox)

    def test_flag_off_same_owner_mailbox_still_verifies(self):
        # Byte-identical: a CONNECTED owner mailbox that is NOT the broker-registered address still passes (OFF).
        c = self._contact(identity_email="broker@gmail.com", mailbox=self._mb("personal@gmail.com"))
        self.assertEqual(contact_verification.check_consistency(c), (True, "ok"))

    @ON
    def test_direct_address_verifies(self):
        # k1: the bound mailbox IS the broker-registered address.
        mb = self._mb("broker@gmail.com")
        c = self._contact(identity_email="broker@gmail.com", mailbox=mb)
        ok, reason = contact_verification.check_consistency(c)
        self.assertTrue(ok, reason)
        contact_verification.verify_contact(c, actor=self.u)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.VERIFIED)   # end-to-end VERIFIED

    @ON
    def test_direct_address_canonicalises_googlemail_and_dots(self):
        # The direct match is gmail-canonical (dots/+tag/googlemail unified), so a legitimate spelling difference still
        # proves routing.
        mb = self._mb("broker.name@googlemail.com")
        c = self._contact(identity_email="brokername@gmail.com", mailbox=mb)
        self.assertEqual(contact_verification.check_consistency(c), (True, "ok"))

    @ON
    def test_non_matching_mailbox_without_observation_is_routing_unproven(self):
        # A same-owner mailbox that is neither the registered address nor has an observed delivery => fail-closed.
        c = self._contact(identity_email="broker@gmail.com", mailbox=self._mb("personal@gmail.com"))
        self.assertEqual(contact_verification.check_consistency(c), (False, "routing_unproven"))
        contact_verification.verify_contact(c, actor=self.u)
        c.refresh_from_db()
        self.assertEqual(c.status, TradingAccountBrokerContact.Status.UNRESOLVED)   # never VERIFIED without routing

    @ON
    def test_observed_authenticated_delivery_proves_routing(self):
        # k2: the connected mailbox differs from the registered address (forwarding/alias), but an AUTHENTICATED
        # (REAL) broker delivery was observed through it => routing proven.
        mb = self._mb("personal@gmail.com")
        c = self._contact(identity_email="broker@gmail.com", mailbox=mb)
        ev = BrokerEvent.objects.create(owner=self.u, provenance=Provenance.REAL, event_type="WITHDRAWAL_REQUESTED",
                                        received_at=timezone.now(),
                                        transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL)
        BrokerMessageObservation.objects.create(owner=self.u, connected_mailbox=mb, provider="GMAIL",
                                                provider_message_id="pm-%s" % _uniq(), event=ev)
        self.assertEqual(contact_verification.check_consistency(c), (True, "ok"))

    @ON
    def test_observed_delivery_must_be_authenticated_real(self):
        # A SYNTHETIC observation does not prove routing (only authenticated REAL broker mail counts).
        mb = self._mb("personal@gmail.com")
        c = self._contact(identity_email="broker@gmail.com", mailbox=mb)
        ev = BrokerEvent.objects.create(owner=self.u, provenance=Provenance.SYNTHETIC,
                                        event_type="WITHDRAWAL_REQUESTED", received_at=timezone.now(),
                                        transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL)
        BrokerMessageObservation.objects.create(owner=self.u, connected_mailbox=mb, provider="GMAIL",
                                                provider_message_id="pm-%s" % _uniq(), event=ev)
        self.assertEqual(contact_verification.check_consistency(c), (False, "routing_unproven"))

    @ON
    def test_observation_through_a_different_mailbox_does_not_count(self):
        # An authenticated delivery observed through a DIFFERENT mailbox must not prove routing for THIS binding.
        mb_bound = self._mb("personal@gmail.com")
        mb_other = self._mb("other@gmail.com")
        c = self._contact(identity_email="broker@gmail.com", mailbox=mb_bound)
        ev = BrokerEvent.objects.create(owner=self.u, provenance=Provenance.REAL, event_type="WITHDRAWAL_REQUESTED",
                                        received_at=timezone.now(),
                                        transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL)
        BrokerMessageObservation.objects.create(owner=self.u, connected_mailbox=mb_other, provider="GMAIL",
                                                provider_message_id="pm-%s" % _uniq(), event=ev)
        self.assertEqual(contact_verification.check_consistency(c), (False, "routing_unproven"))
