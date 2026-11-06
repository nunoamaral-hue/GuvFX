"""Stream B (ADR-0079) — adapter unit tests for the Pepperstone / Dominion Markets / Vantage withdrawal parsers.

Every fixture is SYNTHETIC and PII-FREE: no real name, email address, account number, reference number, message id or
IP address. Addresses use the brokers' public brand domains only (the local-parts are invented). These tests exercise
the parser contract in isolation; the end-to-end lifecycle is in ``tests_stream_b_scenario``.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from broker_intelligence import parsers
from broker_intelligence.parsers_stream_b import DOMINION, PEPPERSTONE, VANTAGE
from broker_intelligence.parsers_tradersway import register_default_parsers

# ---------------------------------------------------------------------------------------------------------------
# Synthetic Pepperstone "GBP 15, card, confirmation-required" lifecycle (reused by tests_stream_b_scenario).
# The confirmation link is a NON-FOLLOWABLE placeholder token; no account clause appears (correlation keys on the
# reference + the test-owned trading account, so the fixtures need not carry anything resembling an account number).
# ---------------------------------------------------------------------------------------------------------------
PS_FROM = "no-reply@pepperstone.com"
PS_REF = "PS-SYNTH-0001"

PS_CONFIRMATION_REQUIRED = dict(
    from_address=PS_FROM,
    subject="Action required: confirm your withdrawal",
    body=("Hello,\n\nPlease confirm your withdrawal request.\n"
          "Amount: GBP 15.00\nWithdrawal method: Visa card\nReference: %s\n\n"
          "Confirm: https://pepperstone.com/confirm?token=SYNTH_PLACEHOLDER_DO_NOT_FOLLOW\n" % PS_REF))
PS_CONFIRMED = dict(
    from_address=PS_FROM,
    subject="Your withdrawal is confirmed",
    body=("Your withdrawal request has been confirmed.\n"
          "Amount: GBP 15.00\nWithdrawal method: Visa card\nReference: %s\n" % PS_REF))
PS_PROCESSING = dict(
    from_address=PS_FROM,
    subject="Your withdrawal is being processed",
    body=("Your withdrawal is being processed.\nAmount: GBP 15.00\nReference: %s\n" % PS_REF))
PS_COMPLETED = dict(
    from_address=PS_FROM,
    subject="Your withdrawal is complete",
    body=("Your withdrawal has been completed.\nFunds have been sent to your Visa card.\n"
          "Amount: GBP 15.00\nReference: %s\n" % PS_REF))
PS_EXPIRED = dict(
    from_address=PS_FROM,
    subject="Your withdrawal confirmation has expired",
    body=("Your withdrawal confirmation has expired. No funds were sent.\n"
          "Amount: GBP 15.00\nReference: %s\n" % PS_REF))


class PepperstoneClassification(TestCase):
    def test_confirmation_required_is_external_but_not_completed(self):
        p = PEPPERSTONE.parse(**PS_CONFIRMATION_REQUIRED)
        self.assertIsNotNone(p)
        self.assertEqual(p.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")  # NOT requested, NOT completed
        self.assertEqual(p.transaction_category, "EXTERNAL_WITHDRAWAL")      # line-anchored "Withdrawal method: Visa card"
        self.assertEqual(p.amount, Decimal("15.00"))
        self.assertEqual(p.currency, "GBP")
        self.assertEqual(p.broker, "Pepperstone")
        self.assertEqual(p.broker_reference_id, PS_REF)

    def test_confirmation_url_and_token_never_surface_in_any_field(self):
        p = PEPPERSTONE.parse(**PS_CONFIRMATION_REQUIRED)
        blob = "|".join(str(getattr(p, f)) for f in (
            "broker_reference_id", "source_account", "destination_account", "broker", "currency"))
        self.assertNotIn("http", blob.lower())
        self.assertNotIn("token", blob.lower())
        self.assertNotIn("SYNTH_PLACEHOLDER", blob)

    def test_confirmed_processing_completed_expired_lifecycle(self):
        self.assertEqual(PEPPERSTONE.parse(**PS_CONFIRMED).event_type, "WITHDRAWAL_CONFIRMED")
        self.assertEqual(PEPPERSTONE.parse(**PS_PROCESSING).event_type, "WITHDRAWAL_PROCESSING")
        self.assertEqual(PEPPERSTONE.parse(**PS_COMPLETED).event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(PEPPERSTONE.parse(**PS_EXPIRED).event_type, "WITHDRAWAL_EXPIRED")

    def test_confirmation_phrasing_with_the_word_complete_is_not_a_completion(self):
        # ADR-0079 review (MEDIUM): the COMPLETED rule must not let an optional reference token swallow arbitrary
        # words ("...withdrawal request to complete...") and mis-fire as a completion on the FIRST email.
        for body in (
                "Please confirm your withdrawal request to complete your cash-out.\nAmount: GBP 15.00\nReference: PS-A\n",
                "Please confirm your withdrawal request. Confirm to complete the withdrawal.\nReference: PS-B\n"):
            p = PEPPERSTONE.parse(from_address=PS_FROM, subject="Action required: confirm your withdrawal", body=body)
            self.assertIsNotNone(p, body)
            self.assertEqual(p.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED", body)   # never WITHDRAWAL_COMPLETED

    def test_completion_email_is_not_claimed_external_from_its_own_text(self):
        # The completion carries no line-anchored rail field, so externality is NOT re-asserted from it (UNKNOWN).
        # Externality is held by the confirmation-required event; the completion only advances by reference.
        p = PEPPERSTONE.parse(**PS_COMPLETED)
        self.assertEqual(p.transaction_category, "UNKNOWN")


class NoProseToExternal(TestCase):
    def test_withdrawal_prose_without_rail_field_is_unknown(self):
        # "withdrawn to your bank account" is PROSE, not the structured "Withdrawal method:" field -> never EXTERNAL.
        p = PEPPERSTONE.parse(
            from_address=PS_FROM, subject="Your withdrawal request has been received",
            body="Your withdrawal request has been received.\nAmount: GBP 15.00 withdrawn to your bank account.\n"
                 "Reference: PS-SYNTH-0002\n")
        self.assertIsNotNone(p)
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")
        self.assertEqual(p.transaction_category, "UNKNOWN")        # under-claim-safe

    def test_deposit_via_card_is_not_an_external_withdrawal(self):
        # A card DEPOSIT carries the rail word but the deposit veto blocks externality (and lifecycle is not a
        # withdrawal lifecycle, so parse returns None entirely).
        p = PEPPERSTONE.parse(
            from_address=PS_FROM, subject="Your deposit was received",
            body="Your deposit of GBP 100.00 via Visa card was received. Payment method: Visa card\n")
        self.assertIsNone(p)   # no withdrawal lifecycle phrase -> not our event at all


class SenderDomainIsolation(TestCase):
    def test_each_parser_matches_only_its_own_registrable_domain(self):
        subj, body = PS_CONFIRMATION_REQUIRED["subject"], PS_CONFIRMATION_REQUIRED["body"]
        self.assertTrue(PEPPERSTONE.can_parse(subject=subj, body=body, from_address="x@pepperstone.com"))
        self.assertTrue(PEPPERSTONE.can_parse(subject=subj, body=body, from_address="x@mail.pepperstone.com"))  # subdomain
        # cross-broker: Pepperstone never claims Dominion/Vantage/TradersWay mail
        for other in ("x@dominionmarkets.com", "x@vantagemarkets.com", "x@tradersway.com"):
            self.assertFalse(PEPPERSTONE.can_parse(subject=subj, body=body, from_address=other), other)
        # look-alikes are refused (exact registrable-domain match, never substring)
        for evil in ("x@pepperstone.com.evil.com", "x@pepperstone-support.com", "x@notpepperstone.com"):
            self.assertFalse(PEPPERSTONE.can_parse(subject=subj, body=body, from_address=evil), evil)

    def test_dominion_and_vantage_bind_their_own_domains(self):
        subj = "confirm your withdrawal"
        body = "Please confirm your withdrawal request.\nAmount: USD 50.00\nWithdrawal method: bank wire\nReference: D-1\n"
        self.assertTrue(DOMINION.can_parse(subject=subj, body=body, from_address="x@dominionmarkets.com"))
        self.assertFalse(DOMINION.can_parse(subject=subj, body=body, from_address="x@vantagemarkets.com"))
        self.assertTrue(VANTAGE.can_parse(subject=subj, body=body, from_address="x@vantagemarkets.com"))
        self.assertFalse(VANTAGE.can_parse(subject=subj, body=body, from_address="x@dominionmarkets.com"))

    def test_dominion_external_withdrawal_via_rail_field(self):
        p = DOMINION.parse(
            from_address="payments@dominionmarkets.com", subject="Confirm your withdrawal",
            body="Please confirm your withdrawal request.\nAmount: USD 50.00\nWithdrawal method: bank wire\nReference: D-1\n")
        self.assertEqual(p.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertEqual(p.transaction_category, "EXTERNAL_WITHDRAWAL")
        self.assertEqual(p.broker, "Dominion Markets")
        self.assertEqual(p.amount, Decimal("50.00"))
        self.assertEqual(p.currency, "USD")


class ReferenceAndAmountExtraction(TestCase):
    def test_reference_that_is_a_url_is_dropped(self):
        p = VANTAGE.parse(
            from_address="no-reply@vantagemarkets.com", subject="Confirm your withdrawal",
            body="Please confirm your withdrawal request.\nWithdrawal method: Skrill\nAmount: EUR 20.00\n"
                 "Reference: https://vantagemarkets.com/r/abc\n")
        self.assertEqual(p.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertEqual(p.broker_reference_id, "")          # a URL reference is never stored

    def test_amount_thousands_separator_and_symbol(self):
        p = VANTAGE.parse(
            from_address="no-reply@vantagemarkets.com", subject="Withdrawal request received",
            body="Your withdrawal request has been received.\nAmount: £1,250.00\nReference: V-9\n")
        self.assertEqual(p.amount, Decimal("1250.00"))
        self.assertEqual(p.currency, "GBP")

    def test_single_anchored_internal_transfer_clause_extracts_accounts(self):
        # INTERNAL trading-account -> INTERNAL trading-account only (both numeric GuvFX-side accounts).
        p = VANTAGE.parse(
            from_address="no-reply@vantagemarkets.com", subject="Confirm your withdrawal",
            body="Please confirm your withdrawal request from account 70001234 to your account 70005678.\n"
                 "Withdrawal method: bank wire\nAmount: USD 30.00\nReference: V-10\n")
        self.assertEqual(p.source_account, "70001234")
        self.assertEqual(p.destination_account, "70005678")

    def test_ambiguous_double_transfer_clause_extracts_neither(self):
        p = VANTAGE.parse(
            from_address="no-reply@vantagemarkets.com", subject="Confirm your withdrawal",
            body="Please confirm your withdrawal request.\n"
                 "from account 70001234 to your account 70005678\nfrom account 80001111 to your account 80002222\n"
                 "Amount: USD 30.00\nReference: V-11\n")
        self.assertEqual(p.source_account, "")           # >1 clause -> never guessed
        self.assertEqual(p.destination_account, "")

    def test_external_card_pan_is_never_captured_as_destination(self):
        # SECURITY (ADR-0079 review HIGH): an external card PAN / bank number must NEVER be captured into the
        # write-once destination_account (a permanent, unredactable PII leak). The "to your account" anchor + the
        # \d{4,12}\b bound exclude any card/bank/wallet destination and structurally refuse a 13+ digit run.
        pan = "4111111111111111"
        p = VANTAGE.parse(
            from_address="no-reply@vantagemarkets.com", subject="Confirm your withdrawal",
            body="Please confirm your withdrawal request from account 70001234 to your card %s.\n"
                 "Withdrawal method: Visa card\nAmount: GBP 15.00\nReference: V-12\n" % pan)
        self.assertIsNotNone(p)
        self.assertEqual(p.destination_account, "")                 # external destination not captured at all
        blob = "|".join(str(getattr(p, f)) for f in (
            "source_account", "destination_account", "broker_reference_id", "currency"))
        self.assertNotIn(pan, blob)                                 # the full PAN surfaces in NO field
        self.assertNotIn(pan[:12], blob)                            # nor any 12-digit prefix of it


class Registration(TestCase):
    def setUp(self):
        parsers.clear_registry()

    def test_register_default_registers_all_five_brokers(self):
        register_default_parsers()
        names = {p.name for p in parsers.get_parsers()}
        self.assertSetEqual(names, {"tradersway", "is6", "pepperstone", "dominion_markets", "vantage"})

    def test_register_default_is_idempotent(self):
        register_default_parsers()
        register_default_parsers()
        self.assertEqual(len([p for p in parsers.get_parsers() if p.name == "pepperstone"]), 1)

    def test_select_parser_routes_by_domain(self):
        register_default_parsers()
        p = parsers.select_parser(**PS_CONFIRMATION_REQUIRED)
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "pepperstone")
