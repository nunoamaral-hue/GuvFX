"""§6/§8 — extra external-withdrawal decision-core invariants (CLEARLY-LABELLED SYNTHETIC; pure, no DB).

Extends tests_external_classifier.py with the override->UNKNOWN assertions at the classify_category layer, the
broker-name normalization edge cases on the certification gate, a distinctly-labelled unrelated/non-transactional
case (§8 item-10), a direct unit test of the positive-signal predicate, and the combined-override truth-table row.
Synthetic signal objects only; certifies no broker's positive classifier.
"""
from django.test import SimpleTestCase

from broker_intelligence.external_withdrawal_classifier import (ExternalWithdrawalSignals, classify_category,
                                                                is_external_withdrawal)
from broker_intelligence.models import TransactionCategory

_SYNTH_CERTIFIED = frozenset({"SyntheticBrokerCo"})


def _sig(**kw):
    return ExternalWithdrawalSignals(**kw)


class OverrideClassifyCategory(SimpleTestCase):
    def test_internal_transfer_override_is_unknown_category(self):
        s = _sig(external_destination=True, explicit_external_type=True, internal_transfer=True)
        self.assertEqual(classify_category(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED),
                         TransactionCategory.UNKNOWN)

    def test_deposit_override_is_unknown_category(self):
        s = _sig(external_payment_rail="bank_wire", deposit=True)
        self.assertEqual(classify_category(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED),
                         TransactionCategory.UNKNOWN)

    def test_both_overrides_together_is_unknown(self):
        s = _sig(external_destination=True, internal_transfer=True, deposit=True)
        self.assertFalse(is_external_withdrawal(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED))
        self.assertEqual(classify_category(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED),
                         TransactionCategory.UNKNOWN)


class BrokerNameNormalization(SimpleTestCase):
    def test_blank_and_none_broker_fail_closed(self):
        s = _sig(external_destination=True)
        for broker in ("", "   ", None):
            self.assertFalse(is_external_withdrawal(s, broker=broker, certified_brokers=_SYNTH_CERTIFIED),
                             repr(broker))
            self.assertEqual(classify_category(s, broker=broker, certified_brokers=_SYNTH_CERTIFIED),
                             TransactionCategory.UNKNOWN)

    def test_surrounding_whitespace_is_stripped_before_match(self):
        s = _sig(external_destination=True)
        self.assertTrue(is_external_withdrawal(s, broker="  SyntheticBrokerCo  ", certified_brokers=_SYNTH_CERTIFIED))


class UnrelatedNonTransactional(SimpleTestCase):
    def test_unrelated_message_mentioning_withdrawal_is_unknown(self):
        # §8 item-10: a marketing/non-transactional email yields NO positive signal — distinct from the generic
        # empty-signal case only in intent. It must classify UNKNOWN even for a certified broker.
        s = _sig()   # a parser found no structured external evidence in an unrelated message
        self.assertEqual(classify_category(s, broker="SyntheticBrokerCo", certified_brokers=_SYNTH_CERTIFIED),
                         TransactionCategory.UNKNOWN)


class PositiveSignalPredicate(SimpleTestCase):
    def test_each_positive_field_sets_predicate(self):
        self.assertTrue(_sig(external_destination=True).has_positive_external_evidence())
        self.assertTrue(_sig(external_payment_rail="crypto").has_positive_external_evidence())
        self.assertTrue(_sig(explicit_external_type=True).has_positive_external_evidence())

    def test_overrides_do_not_contribute_to_predicate(self):
        self.assertFalse(_sig().has_positive_external_evidence())
        self.assertFalse(_sig(internal_transfer=True, deposit=True).has_positive_external_evidence())
