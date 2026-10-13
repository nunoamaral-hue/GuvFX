"""§7 — external-withdrawal classifier invariants, pinned with CLEARLY-LABELLED SYNTHETIC fixtures.

These fixtures are SYNTHETIC — hand-constructed signal objects, NOT parsed from any real broker email. They exist to
pin the decision INVARIANTS; they do NOT certify any broker's positive classifier (that requires genuine evidence,
§7/§8). The production default (empty CERTIFIED_BROKERS) means no broker can positively classify until certified.
"""
from django.test import SimpleTestCase

from broker_intelligence.external_withdrawal_classifier import (CERTIFIED_BROKERS, ExternalWithdrawalSignals,
                                                                classify_category, is_external_withdrawal)
from broker_intelligence.models import TransactionCategory

# A SYNTHETIC certified-broker set, used ONLY to exercise the positive path in these tests. Production ships empty.
_SYNTH_CERTIFIED = frozenset({"SyntheticBrokerCo"})


def _sig(**kw):
    return ExternalWithdrawalSignals(**kw)


class ProductionFailsClosedTests(SimpleTestCase):
    def test_no_broker_is_certified_by_default(self):
        # Fail-closed: shipping default certifies NOBODY, so no positive classification is possible in prod.
        self.assertEqual(CERTIFIED_BROKERS, frozenset())

    def test_positive_signal_but_uncertified_broker_is_unknown(self):
        # Even with strong positive external evidence, an UNCERTIFIED broker never yields EXTERNAL_WITHDRAWAL.
        s = _sig(external_destination=True, explicit_external_type=True)
        self.assertFalse(is_external_withdrawal(s, broker="SyntheticBrokerCo"))        # default empty certified set
        self.assertEqual(classify_category(s, broker="SyntheticBrokerCo"), TransactionCategory.UNKNOWN)


class InvariantTests(SimpleTestCase):
    def test_internal_transfer_is_never_external(self):
        # Even a certified broker + an external signal: an internal-transfer override wins (money stayed inside).
        s = _sig(external_destination=True, explicit_external_type=True, internal_transfer=True)
        self.assertFalse(is_external_withdrawal(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED))

    def test_deposit_is_never_a_withdrawal(self):
        s = _sig(external_payment_rail="bank_wire", deposit=True)
        self.assertFalse(is_external_withdrawal(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED))

    def test_no_positive_signal_is_unknown(self):
        # No structured positive evidence at all → UNKNOWN (ambiguous, never guessed) — this is the "the word
        # 'withdrawal' in the subject alone" case: it produces NO signal, so it can never classify external.
        s = _sig()
        self.assertFalse(is_external_withdrawal(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED))
        self.assertEqual(classify_category(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED),
                         TransactionCategory.UNKNOWN)


class CertifiedPositivePathTests(SimpleTestCase):
    """The positive path — reachable ONLY with an explicit (synthetic) certified-broker set."""

    def test_external_destination_certified_is_external(self):
        s = _sig(external_destination=True)
        self.assertTrue(is_external_withdrawal(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED))
        self.assertEqual(classify_category(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED),
                         TransactionCategory.EXTERNAL_WITHDRAWAL)

    def test_external_payment_rail_certified_is_external(self):
        s = _sig(external_payment_rail="card_refund")
        self.assertTrue(is_external_withdrawal(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED))

    def test_explicit_external_type_certified_is_external(self):
        s = _sig(explicit_external_type=True)
        self.assertTrue(is_external_withdrawal(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED))

    def test_a_different_certified_broker_does_not_authorise_this_one(self):
        s = _sig(external_destination=True)
        self.assertFalse(is_external_withdrawal(s, broker="SomeOtherBroker", certified_brokers=_SYNTH_CERTIFIED))
