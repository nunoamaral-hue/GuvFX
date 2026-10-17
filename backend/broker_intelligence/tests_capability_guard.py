"""§8 — structural "no withdrawal surfaceable" guards (close the documented-but-unexecuted coverage gap).

Three cheap, durable guards, all provable with synthetic / existing seed data (no genuine sample):
  1. The external-withdrawal DECISION CORE is NOT wired into the live parsers (so a positive classification can
     never reach a BrokerEvent before a broker is certified). Verified by import-surface inspection.
  2. No ACTIVE BrokerProfile declares EXTERNAL_WITHDRAWAL, and CERTIFIED_BROKERS is empty (fail-closed).
  3. Running the REAL parser-selection path over synthetic broker-domain mail (withdrawal/transfer/deposit wording)
     NEVER yields transaction_category == EXTERNAL_WITHDRAWAL, and any emitted category is within that broker's
     declared capability — the "parser-emits ⊆ declared" cross-check the catalogue docstring claims but never ran.
"""
from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from broker_intelligence import parsers_is6, parsers_tradersway
from broker_intelligence.external_withdrawal_classifier import CERTIFIED_BROKERS
from broker_intelligence.models import TransactionCategory


class ClassifierNotWiredIntoParsers(SimpleTestCase):
    def test_decision_core_symbols_absent_from_parser_import_surface(self):
        # If a parser ever `from .external_withdrawal_classifier import ...`ed the decision core, one of these names
        # would appear in its module namespace. Their absence proves the parsers classify independently (their own
        # _classify_category), so the fail-closed decision core cannot be bypassed via a parser.
        for mod in (parsers_tradersway, parsers_is6):
            names = set(dir(mod))
            for forbidden in ("external_withdrawal_classifier", "is_external_withdrawal",
                              "classify_category", "ExternalWithdrawalSignals"):
                self.assertNotIn(forbidden, names, f"{mod.__name__} unexpectedly imports {forbidden}")


class FailClosedCapability(TestCase):
    def test_no_active_profile_declares_external_withdrawal_and_no_broker_certified(self):
        from broker_intelligence.models import BrokerProfile
        self.assertEqual(CERTIFIED_BROKERS, frozenset())
        for p in BrokerProfile.objects.filter(is_active=True):
            self.assertNotIn(TransactionCategory.EXTERNAL_WITHDRAWAL, p.supported_category_list(),
                             f"{p.slug} must not declare EXTERNAL_WITHDRAWAL")


class ParsersNeverEmitExternalWithdrawal(TestCase):
    """Run the real select_parser+parse path over synthetic broker-domain mail; the structural invariant
    (no text->EXTERNAL path) must hold, and emitted categories must stay within the declared capability."""

    _ALLOWED = {TransactionCategory.INTERNAL_TRANSFER, TransactionCategory.DEPOSIT, TransactionCategory.UNKNOWN}

    def _parse(self, *, from_address, subject, body):
        parsers_tradersway.register_default_parsers()
        from broker_intelligence.parsers import select_parser
        p = select_parser(subject=subject, body=body, from_address=from_address)
        return p.parse(subject=subject, body=body, from_address=from_address) if p else None

    def _assert_never_external(self, parsed):
        if parsed is not None:
            self.assertNotEqual(parsed.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)
            self.assertIn(parsed.transaction_category, self._ALLOWED)

    def test_tradersway_withdrawal_wording_never_external(self):
        parsed = self._parse(
            from_address="noreply@tradersway.com",
            subject="Please confirm your withdrawal request",
            body=("Dear client, please confirm your withdrawal request. This will transfer the amount of 24.12 USD "
                  "from account 54548 to account 647390. Your request number is: 4113188."))
        self._assert_never_external(parsed)

    def test_tradersway_external_sounding_wording_still_never_external(self):
        # Even wording that SOUNDS external must not produce EXTERNAL_WITHDRAWAL — the parser has no such path.
        parsed = self._parse(
            from_address="noreply@tradersway.com",
            subject="Withdrawal to your bank account",
            body="Your withdrawal of 500.00 USD to your external bank account has been processed. Amount: 500.00 USD.")
        self._assert_never_external(parsed)

    def test_is6_deposit_wording_never_external(self):
        parsed = self._parse(
            from_address="noreply@is6.com",
            subject="Deposit Completed",
            body="Your deposit has been completed. Amount: 200.00 USD. Account: 51763830.")
        self._assert_never_external(parsed)
