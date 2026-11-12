"""ADR-0093 — Vantage "processed successfully" completion parsing + single-login extraction.

SYNTHETIC / PII-FREE: no real name, email, account number, reference, message id or body is reproduced. The Vantage
sender domain is the public brand domain (local-parts invented). The genuine completion email's PARSER-RELEVANT
structure (a perfective "...has been processed successfully" statement, no stable reference, an "account <digits>"
login with no ':' separator) was established privacy-safely (boolean phrase match on the retained evidence, never a
body read into output); these fixtures reproduce only that structure, with a synthetic 8-digit login ending 5017.

Mutation-killers: the completion must UPGRADE only a non-terminal base, only on the PERFECTIVE phrase, only without a
deposit context; future/habitual/terminal-negative/deposit variants must never read as a completion; the upgrade is
PER-BROKER (Vantage only) so Pepperstone/Dominion are unchanged; the login is kept only when UNIQUE and a 13+ digit
PAN is never captured.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from broker_intelligence.parsers_stream_b import PEPPERSTONE, VANTAGE

VT_FROM = "no-reply@vantagemarkets.com"
LOGIN = "70005017"  # synthetic 8-digit MT5 login ending 5017 (mirrors the genuine suffix; not a real account)


def _vt(subject, body):
    return VANTAGE.parse(from_address=VT_FROM, subject=subject, body=body)


class VantageCompletionClassification(SimpleTestCase):
    def test_processed_successfully_is_a_completion(self):
        # The genuine Vantage completion: a request-successful opening PLUS a perfective "processed successfully"
        # statement whose "processed" is NOT adjacent to "withdrawal" (so the shared completion anchors miss it).
        p = _vt("Withdrawal Request Successful",
                "Your withdrawal request was successful. Your request has been processed successfully.\n"
                "Account %s\nAmount: 30.00\n" % LOGIN)
        self.assertIsNotNone(p)
        self.assertEqual(p.event_type, "WITHDRAWAL_COMPLETED")      # upgraded from the REQUESTED base
        self.assertEqual(p.broker, "Vantage")
        self.assertEqual(p.amount, Decimal("30.00"))
        self.assertEqual(p.currency, "")                            # currency genuinely unresolved — never invented
        self.assertEqual(p.transaction_category, "UNKNOWN")         # completion carries no rail -> not re-claimed external
        self.assertEqual(p.source_account, LOGIN)                   # single stated login extracted
        self.assertEqual(p.broker_reference_id, "")                 # no stable reference (ADR-0090)

    def test_submission_is_requested_not_completed(self):
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted.\nAccount %s\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")      # submission stays an opening
        self.assertEqual(p.source_account, LOGIN)

    def test_request_successful_without_processed_is_requested(self):
        # Packet negative control: "request successful" WITHOUT the processed-successfully body is NEVER a completion.
        p = _vt("Withdrawal Request Successful",
                "Your withdrawal request was successful and is pending review.\nAmount: 30.00\n")
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")

    def test_future_processed_successfully_is_not_a_completion(self):
        # "will be processed successfully" lacks the perfective auxiliary -> never upgraded.
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted and will be processed successfully within 24 hours.\n"
                "Amount: 30.00\n")
        self.assertNotEqual(getattr(p, "event_type", None), "WITHDRAWAL_COMPLETED")
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")

    def test_habitual_promotional_processed_successfully_is_not_a_completion(self):
        # "withdrawals are processed successfully" (habitual/promotional) lacks the perfective auxiliary.
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted. Our withdrawals are processed successfully within 24 hours.\n"
                "Amount: 30.00\n")
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")

    def test_deposit_processed_successfully_does_not_complete_a_withdrawal(self):
        # Deposit veto: a "deposit was processed successfully" line never flips a withdrawal email to COMPLETED.
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted. Separately, your deposit was processed successfully.\n"
                "Amount: 30.00\n")
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")

    def test_terminal_negative_is_never_upgraded_to_completed(self):
        # A rejected/failed outcome is terminal and is NEVER upgraded, even if a perfective "processed successfully"
        # statement appears elsewhere in the mail (e.g. a refund line).
        p = _vt("Withdrawal Rejected",
                "Your withdrawal request was rejected. Your refund has been processed successfully.\n"
                "Amount: 30.00\n")
        self.assertEqual(p.event_type, "WITHDRAWAL_REJECTED")

    def test_subject_only_processed_successfully_is_not_enough_without_body(self):
        # The perfective phrase must be present in the text (subject+body are classified together); a bare subject
        # with no perfective body statement is not upgraded.
        p = _vt("Processed",  # no perfective "has been processed successfully" anywhere
                "Your withdrawal has been submitted.\nAmount: 30.00\n")
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")


class VantageSingleLoginExtraction(SimpleTestCase):
    def test_single_stated_login_is_extracted(self):
        p = _vt("Withdrawal Request Successful",
                "Your request has been processed successfully for account %s.\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.source_account, LOGIN)

    def test_ambiguous_multiple_logins_fail_closed(self):
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted from account %s. Legacy account 70009999 is closed.\n"
                "Amount: 30.00\n" % LOGIN)
        self.assertEqual(p.source_account, "")   # >1 distinct login -> never guessed (resolver fails closed)

    def test_pan_length_number_is_never_captured_as_login(self):
        pan = "4111111111111111"  # 16-digit card PAN
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted to account %s.\nCard on file: %s\nAmount: 30.00\n" % (LOGIN, pan))
        self.assertEqual(p.source_account, LOGIN)
        blob = "|".join(str(getattr(p, f)) for f in ("source_account", "destination_account", "broker_reference_id"))
        self.assertNotIn(pan, blob)
        self.assertNotIn(pan[:12], blob)

    def test_transfer_clause_still_wins_over_single_login(self):
        # When an anchored internal-transfer clause is present it is used unchanged; the single-login fallback only
        # runs when the transfer clause yielded nothing.
        p = _vt("Confirm your withdrawal",
                "Please confirm your withdrawal request from account 70001234 to your account 70005678.\n"
                "Amount: 30.00\n")
        self.assertEqual(p.source_account, "70001234")
        self.assertEqual(p.destination_account, "70005678")


class OverClaimGuards(SimpleTestCase):
    """ADR-0093 adversarial-review regressions: a perfective 'processed successfully' must refer to the WITHDRAWAL,
    in the same sentence, and never via a future/modal/negated or word-fragment match."""

    def test_unrelated_success_line_in_same_mail_stays_requested(self):
        # A withdrawal-request email whose SEPARATE sentence reports an unrelated success must NOT complete.
        for other in ("your identity verification has been processed successfully",
                      "your refund has been processed successfully",
                      "your internal transfer has been processed successfully",
                      "your card payment has been processed successfully"):
            p = _vt("Withdrawal Request Successful",
                    f"Your withdrawal request was successful and is pending review. Separately, {other}.\n"
                    "Amount: 30.00\n")
            self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED", other)

    def test_refund_word_fragment_does_not_anchor_a_completion(self):
        # "reFUND" must not satisfy the 'funds' noun (word-boundary anchored).
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted. Separately, your refund has been processed successfully.\n")
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")

    def test_future_perfect_and_modal_are_not_completed(self):
        for body in ("Your withdrawal will have been processed successfully soon.\nAmount: 30.00\n",
                     "Your withdrawal request should have been processed successfully by now.\nAmount: 30.00\n",
                     "Your withdrawal may have been processed successfully.\nAmount: 30.00\n"):
            p = _vt("Withdrawal update", body)
            self.assertNotEqual(getattr(p, "event_type", None), "WITHDRAWAL_COMPLETED", body)

    def test_negated_completion_is_not_completed(self):
        p = _vt("Withdrawal update", "Your withdrawal has not been processed successfully.\nAmount: 30.00\n")
        self.assertNotEqual(getattr(p, "event_type", None), "WITHDRAWAL_COMPLETED")

    def test_grouped_pan_fragment_is_not_captured_as_login(self):
        p = _vt("Withdrawal Request Successful",
                "Your request has been processed successfully for account 70005017.\n"
                "Card on file account 4111 1111 1111 1111.\nAmount: 30.00\n")
        self.assertEqual(p.source_account, "70005017")   # the login, not the grouped-PAN fragment
        blob = "|".join(str(getattr(p, f)) for f in ("source_account", "destination_account"))
        self.assertNotIn("4111", blob)


class CurrencyExtraction(SimpleTestCase):
    """ADR-0095 — Vantage states the currency on its OWN labelled line ("Currency Type: GBP"), often with the value on
    the NEXT line, so the inline amount-currency is blank. SYNTHETIC fixtures only."""

    def test_submission_extracts_amount_and_currency(self):
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted for account %s.\n"
                "Currency Type: GBP\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")
        self.assertEqual(p.amount, Decimal("30.00"))
        self.assertEqual(p.currency, "GBP")                 # from the standalone "Currency Type:" field
        self.assertEqual(p.source_account, LOGIN)

    def test_currency_type_value_on_next_line(self):
        # The genuine layout: the label and its value sit on separate lines.
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted for account %s.\nCurrency Type:\n GBP\nAmount:\n 30.00\n" % LOGIN)
        self.assertEqual(p.amount, Decimal("30.00"))
        self.assertEqual(p.currency, "GBP")

    def test_currency_whitespace_and_case(self):
        for body, ccy in (("Currency Type:   gbp\nAmount: 30.00\n", "GBP"),
                          ("CURRENCY TYPE : GBP\nAmount: 30.00\n", "GBP"),
                          ("Currency Type:\n USD\nAmount: 30.00\n", "USD")):
            p = _vt("Withdrawal Submitted", "account %s.\n%s" % (LOGIN, body))
            self.assertEqual(p.currency, ccy)

    def test_account_currency_line_does_not_override_currency_type(self):
        # A preceding "Account Currency: USD" (or a bare "Currency: USD") must NOT win over the transaction's
        # "Currency Type" field — it is line-anchored and the "Type" word is mandatory.
        p = _vt("Withdrawal Submitted",
                "account %s.\nAccount Currency: USD\nCurrency Type: GBP\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.currency, "GBP")
        p2 = _vt("Withdrawal Submitted", "account %s.\nCurrency: USD\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p2.currency, "")       # bare "Currency:" (no "Type") is not read

    def test_empty_currency_type_field_stays_blank(self):
        p = _vt("Withdrawal Submitted",
                "account %s.\nCurrency Type:\n\nUSD reference follows.\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.currency, "")        # an empty field never adopts a stray code across blank lines

    def test_missing_currency_stays_blank(self):
        p = _vt("Withdrawal Submitted", "Your withdrawal has been submitted for account %s.\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.amount, Decimal("30.00"))
        self.assertEqual(p.currency, "")                    # absent -> blank, never invented

    def test_unsupported_currency_token_blank(self):
        p = _vt("Withdrawal Submitted", "account %s.\nCurrency Type: XYZ\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.currency, "")                    # XYZ is not a known currency -> blank

    def test_prose_mention_is_not_a_currency(self):
        p = _vt("Withdrawal Submitted",
                "Your withdrawal has been submitted for account %s. Funds are paid in GBP within 3 days.\n"
                "Amount: 30.00\n" % LOGIN)
        self.assertEqual(p.currency, "")                    # "in GBP" prose has no currency label -> blank

    def test_card_and_suffix_not_parsed_as_amount(self):
        p = _vt("Withdrawal Submitted",
                "account %s.\nCard Number: 465858**** **3055\nCurrency Type: GBP\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.amount, Decimal("30.00"))        # the Amount field, not the card digits
        self.assertEqual(p.currency, "GBP")
        self.assertEqual(p.source_account, LOGIN)

    def test_completion_without_currency_stays_blank(self):
        p = _vt("Withdrawal Request Successful",
                "Your request has been processed successfully for account %s.\nAmount: 30.00\n" % LOGIN)
        self.assertEqual(p.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(p.currency, "")                    # the completion states no currency

    def test_pepperstone_does_not_read_currency_type_field(self):
        # Pepperstone does NOT opt into currency_from_field; a bare "Currency Type: GBP" line in its TEXT body is not
        # read (its currency comes from the structured amount field). Byte-for-byte unchanged.
        p = PEPPERSTONE.parse(from_address="no-reply@pepperstone.com", subject="Withdrawal Submitted",
                              body="Your withdrawal has been submitted.\nCurrency Type: GBP\nAmount: 30.00\n")
        self.assertEqual(getattr(p, "currency", ""), "")            # not read for Pepperstone (text path)


class PerBrokerGating(SimpleTestCase):
    def test_pepperstone_does_not_upgrade_on_processed_successfully(self):
        # Same text that completes a Vantage mail must NOT complete a Pepperstone mail: the perfective-completion
        # upgrade and single-login extraction are OFF for every broker except Vantage.
        body = ("Your withdrawal has been submitted. Your request has been processed successfully.\n"
                "Account %s\nAmount: 30.00\n" % LOGIN)
        vt = VANTAGE.parse(from_address=VT_FROM, subject="Withdrawal Submitted", body=body)
        ps = PEPPERSTONE.parse(from_address="no-reply@pepperstone.com", subject="Withdrawal Submitted", body=body)
        self.assertEqual(vt.event_type, "WITHDRAWAL_COMPLETED")     # Vantage upgrades
        self.assertEqual(ps.event_type, "WITHDRAWAL_REQUESTED")     # Pepperstone unchanged
        self.assertEqual(ps.source_account, "")                     # Pepperstone does not do single-login extraction
