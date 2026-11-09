"""ADR-0088 — pure-detector unit tests (no DB). Deterministic dispositions, lifecycle classification, completion
semantics, multilingual wording, negatives, redaction and safe extraction. SYNTHETIC fixtures only."""
from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from broker_intelligence import generic_detector as g

_PAT = [{"name": "acme", "version": 1, "broker_display_name": "Acme FX",
         "domains": ["acme-fx.com"], "subject_indicators": ["withdrawal"], "body_indicators": []}]


def _d(subject="", body="", *, domain="acme-fx.com", auth="pass", allow=True, patterns=_PAT, html="", ref=""):
    return g.detect(subject=subject, body=body, from_domain=domain, auth_verdict=auth,
                    sender_allowlisted=allow, patterns=patterns, html_text=html, broker_reference_id=ref)


class Dispositions(SimpleTestCase):
    def test_high_confidence_requires_allowlist_and_pattern(self):
        d = _d("Withdrawal completed", "GBP 42.00 sent to account ending 7766")
        self.assertEqual(d.disposition, g.GENERIC_HIGH_CONFIDENCE)
        self.assertEqual(d.proposed_state, g.S_COMPLETED)
        self.assertEqual((d.amount, d.currency), (Decimal("42.00"), "GBP"))
        self.assertEqual(d.masked_account_suffix, "••••" + "7766")
        self.assertIsNotNone(d.parsed_event)
        self.assertEqual(d.parsed_event.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(d.parsed_event.transaction_category, "UNKNOWN")  # never asserts EXTERNAL without certification

    def test_allowlisted_without_pattern_is_review_not_accept(self):
        d = _d("Withdrawal completed", "funds sent", patterns=[])
        self.assertEqual(d.disposition, g.REVIEW_REQUIRED)
        self.assertIn(g.RC_NO_MATCHING_PATTERN, d.reason_codes)
        self.assertIsNone(d.parsed_event)

    def test_unseen_sender_is_review_never_accept(self):
        d = _d("Withdrawal completed", "USD 20 sent, account ending 1234", domain="newbroker.io",
               allow=False, patterns=[])
        self.assertEqual(d.disposition, g.REVIEW_REQUIRED)
        self.assertIn(g.RC_SENDER_NOT_ALLOWLISTED, d.reason_codes)
        self.assertIsNone(d.parsed_event)

    def test_auth_fail_quarantines(self):
        d = _d("Withdrawal completed", "funds sent", auth="fail")
        self.assertEqual(d.disposition, g.QUARANTINED)
        self.assertIn(g.RC_AUTH_FAIL, d.reason_codes)
        self.assertIsNone(d.parsed_event)

    def test_no_auth_verdict_quarantines(self):
        self.assertEqual(_d("withdrawal completed", "funds sent", auth=None).disposition, g.QUARANTINED)

    def test_intent_without_explicit_lifecycle_is_review(self):
        d = _d("Regarding your withdrawal", "we are writing about your withdrawal")
        self.assertEqual(d.disposition, g.REVIEW_REQUIRED)
        self.assertIn(g.RC_NO_EXPLICIT_LIFECYCLE, d.reason_codes)

    def test_contradictory_terminal_quarantines(self):
        d = _d("withdrawal", "your withdrawal has been completed and has been cancelled")
        self.assertEqual(d.disposition, g.QUARANTINED)
        self.assertIn(g.RC_CONTRADICTORY_LIFECYCLE, d.reason_codes)


class LifecycleAndCompletion(SimpleTestCase):
    def test_requested(self):
        self.assertEqual(_d("Withdrawal request received", "we received your withdrawal request").proposed_state,
                         g.S_REQUESTED)

    def test_confirmation_required(self):
        self.assertEqual(_d("Confirm your withdrawal", "please approve or cancel your withdrawal").proposed_state,
                         g.S_CONFIRMATION_REQUIRED)

    def test_processing(self):
        self.assertEqual(_d("Withdrawal update", "your withdrawal is being processed").proposed_state, g.S_PROCESSING)

    def test_completed(self):
        self.assertEqual(_d("Withdrawal", "your withdrawal has been completed").proposed_state, g.S_COMPLETED)

    def test_failed(self):
        self.assertEqual(_d("Withdrawal", "your withdrawal has failed").proposed_state, g.S_FAILED)

    def test_cancelled(self):
        self.assertEqual(_d("Withdrawal", "your withdrawal has been cancelled").proposed_state, g.S_CANCELLED)

    def test_processed_past_tense_is_completed(self):
        self.assertEqual(_d("Withdrawal", "we've processed your withdrawal").proposed_state, g.S_COMPLETED)
        self.assertEqual(_d("Withdrawal", "your withdrawal has been processed").proposed_state, g.S_COMPLETED)

    def test_being_and_will_be_processed_are_not_completed(self):
        self.assertEqual(_d("Withdrawal", "your withdrawal is being processed").proposed_state, g.S_PROCESSING)
        self.assertEqual(_d("Withdrawal", "your withdrawal will be processed shortly").proposed_state, g.S_PROCESSING)
        # bare 'processed' alone with future wording must never become COMPLETED
        self.assertNotEqual(_d("Withdrawal", "it will be processed in 1-2 business days").proposed_state, g.S_COMPLETED)

    def test_most_advanced_state_wins(self):
        self.assertEqual(_d("Withdrawal", "we received your withdrawal request; it has been completed").proposed_state,
                         g.S_COMPLETED)

    def test_multilingual_completion(self):
        self.assertEqual(_d("Auszahlung", "ihre auszahlung abgeschlossen").proposed_state, g.S_COMPLETED)
        self.assertEqual(_d("出金", "出金完了のお知らせ").proposed_state, g.S_COMPLETED)
        self.assertEqual(_d("인출", "인출 완료되었습니다").proposed_state, g.S_COMPLETED)


class Negatives(SimpleTestCase):
    def test_deposit_is_not_withdrawal(self):
        self.assertEqual(_d("Deposit received", "your deposit completed").disposition, g.NOT_WITHDRAWAL)

    def test_marketing_is_not_withdrawal(self):
        self.assertEqual(_d("Trade now", "special offer webinar, unsubscribe here").disposition, g.NOT_WITHDRAWAL)

    def test_account_verification_is_not_withdrawal(self):
        self.assertEqual(_d("Verify your email", "please verify your account").disposition, g.NOT_WITHDRAWAL)

    def test_margin_call_is_not_withdrawal(self):
        self.assertEqual(_d("Margin call", "your margin level is low, stop out risk").disposition, g.NOT_WITHDRAWAL)

    def test_trade_alert_is_not_withdrawal(self):
        self.assertEqual(_d("Order filled", "your position closed, trade executed").disposition, g.NOT_WITHDRAWAL)

    def test_deposit_with_withdrawal_lifecycle_is_still_a_withdrawal(self):
        # a genuine withdrawal email that merely mentions the word 'deposit' must NOT be lost
        d = _d("Withdrawal completed", "your withdrawal has been completed (not a deposit)")
        self.assertNotEqual(d.disposition, g.NOT_WITHDRAWAL)
        self.assertEqual(d.proposed_state, g.S_COMPLETED)


class Extraction(SimpleTestCase):
    def test_amount_symbol_and_iso(self):
        self.assertEqual(_d("Withdrawal completed", "£42.00 sent").amount, Decimal("42.00"))
        self.assertEqual(_d("Withdrawal completed", "USD 1,250.50 sent").amount, Decimal("1250.50"))
        self.assertEqual(_d("Withdrawal completed", "20 EUR sent").currency, "EUR")

    def test_missing_amount_is_none_not_fabricated(self):
        self.assertIsNone(_d("Withdrawal completed", "your withdrawal has been completed").amount)

    def test_account_suffix_masked_last4_only(self):
        d = _d("Withdrawal completed", "sent to account 99887766")  # full 8-digit number present
        # only the last 4 is ever surfaced; the full account must never appear in any field
        self.assertEqual(d.masked_account_suffix, "••••" + "7766")
        for fld in (d.candidate_broker_name, d.currency, d.masked_account_suffix, d.matched_pattern):
            self.assertNotIn("99887766", fld)

    def test_missing_account_leaves_suffix_empty(self):
        self.assertEqual(_d("Withdrawal completed", "your withdrawal has been completed").masked_account_suffix, "")

    def test_orphan_completion_fabricates_nothing(self):
        d = _d("Withdrawal", "your withdrawal has been completed")   # no amount/ref/account/request
        self.assertEqual(d.proposed_state, g.S_COMPLETED)
        self.assertIsNone(d.amount)
        self.assertEqual(d.broker_reference_id, "")
        self.assertEqual(d.masked_account_suffix, "")


class HtmlAndRedaction(SimpleTestCase):
    def test_html_path_detects_lifecycle(self):
        d = _d("Withdrawal", "", html="<html><body><p>Your withdrawal has been completed</p><td>GBP 42.00</td></body></html>")
        self.assertEqual(d.proposed_state, g.S_COMPLETED)
        self.assertEqual((d.amount, d.currency), (Decimal("42.00"), "GBP"))

    def test_no_url_or_token_in_any_stored_field(self):
        d = _d("Withdrawal completed",
               "confirm at https://acme-fx.com/confirm?token=abc123DEF456ghi789XY sent to account ending 7766")
        for fld in (d.candidate_broker_name, d.currency, d.masked_account_suffix, d.matched_pattern,
                    d.broker_reference_id):
            from broker_intelligence.redaction import contains_sensitive
            self.assertFalse(contains_sensitive(fld), fld)

    def test_broker_name_derived_from_domain_only(self):
        d = _d("Withdrawal completed", "Dear John Smith, your withdrawal is complete", domain="acme-fx.com",
               patterns=[])
        # name derived from DOMAIN, never the body salutation
        self.assertEqual(d.candidate_broker_name, "Acme Fx")
        self.assertNotIn("John", d.candidate_broker_name)
        self.assertNotIn("Smith", d.candidate_broker_name)


class FalseCompletionRegression(SimpleTestCase):
    """ADR-0088 adversarial regression (review HIGH finding): over-broad completion fragments must NOT mark
    present/future-tense or non-fund mail COMPLETED."""

    def test_confirmation_sent_to_email_during_processing_is_not_completed(self):
        d = _d("Withdrawal update", "your withdrawal is being processed. a confirmation has been sent to your email.")
        self.assertEqual(d.proposed_state, g.S_PROCESSING)

    def test_future_transfer_is_not_completed(self):
        self.assertNotEqual(_d("Withdrawal", "your withdrawal will be transferred to your bank account").proposed_state,
                            g.S_COMPLETED)

    def test_future_credit_is_not_completed(self):
        self.assertNotEqual(_d("Withdrawal", "your withdrawal will be credited to your account soon").proposed_state,
                            g.S_COMPLETED)

    def test_verification_code_sent_is_not_a_withdrawal(self):
        self.assertEqual(_d("Verify", "a verification code has been sent to your phone").disposition, g.NOT_WITHDRAWAL)

    def test_genuine_fund_completion_still_completes(self):
        self.assertEqual(_d("Withdrawal",
                            "your withdrawal has been completed; funds have been sent to your bank").proposed_state,
                         g.S_COMPLETED)
        self.assertEqual(_d("Payout", "your withdrawal payout has been sent").proposed_state, g.S_COMPLETED)
