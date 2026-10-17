"""§4-G — privacy-safe fixture-extraction unit tests (pure, synthetic; no genuine sample, no DB).

RULE-11 discipline: every masking claim has a POSITIVE control (the sensitive thing IS masked) AND a NEGATIVE
control (the structural thing is NOT over-masked), so a future change that silently stops masking — or starts
destroying template structure — fails here.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from broker_intelligence.fixture_extraction import (
    sanitise_text, residual_numeric_runs, residual_token_candidates, sanitise_header_block, extract_fixture)


class SanitiseTextPositiveControls(SimpleTestCase):
    """The sensitive material IS masked."""

    def test_email_address_is_masked(self):
        out = sanitise_text("Please reply to john.doe+acct@is6.com today")
        self.assertIn("<EMAIL>", out)
        self.assertNotIn("john.doe", out)
        self.assertNotIn("is6.com", out)

    def test_salutation_name_is_masked(self):
        out = sanitise_text("Dear John Smith,\nYour request was received.")
        self.assertIn("Dear <NAME>,", out)
        self.assertNotIn("John", out)
        self.assertNotIn("Smith", out)

    def test_account_and_reference_numbers_are_masked_length_aware(self):
        out = sanitise_text("Account 51763830 reference 4113188 processed")
        self.assertIn("<ACCT:8>", out)      # 8-digit account
        self.assertIn("<ACCT:7>", out)      # 7-digit reference
        self.assertNotIn("51763830", out)
        self.assertNotIn("4113188", out)

    def test_confirmation_url_is_masked(self):
        out = sanitise_text("Confirm at https://tradersway.com/confirm?token=abc123 now")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("https://", out)
        self.assertNotIn("token=abc123", out)

    def test_long_opaque_token_is_masked(self):
        out = sanitise_text("auth code X9fK2mQ7bT4wL1pZ8nR3 attached")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("X9fK2mQ7bT4wL1pZ8nR3", out)


class SanitiseTextNegativeControls(SimpleTestCase):
    """The STRUCTURE / amount shape is NOT destroyed (over-masking would ruin the fixture's purpose)."""

    def test_small_decimal_amount_is_preserved(self):
        out = sanitise_text("Amount: 24.12 USD")
        self.assertIn("24.12", out)
        self.assertIn("USD", out)

    def test_grouped_decimal_amount_is_preserved(self):
        out = sanitise_text("Total: 1000.00 EUR")
        self.assertIn("1000.00", out)
        self.assertIn("EUR", out)

    def test_large_decimal_amount_integer_part_preserved(self):
        # The decimal guard means a 5+ digit integer part of an AMOUNT is not mistaken for an account number.
        out = sanitise_text("Amount withdrawn: 12345.67 USD")
        self.assertIn("12345.67", out)
        self.assertNotIn("<ACCT", out)

    def test_whole_number_amount_is_masked_as_identifier_privacy_safe(self):
        # DOCUMENTED tradeoff (privacy-safe default): a whole-number run >= min_account_digits with NO decimal
        # separator is indistinguishable from an account/reference, so it is masked. The label + currency keep the
        # amount legible to a reviewer, and the residual net (broader than the masker) does not re-expose it.
        out = sanitise_text("Amount: 50000 USD")
        self.assertIn("<ACCT:5>", out)
        self.assertNotIn("50000", out)
        self.assertIn("Amount:", out)
        self.assertIn("USD", out)

    def test_labels_and_lifecycle_words_preserved(self):
        text = "Reference: ...\nAmount: ...\nDestination: ...\nStatus: WITHDRAWAL_COMPLETED"
        out = sanitise_text(text)
        for token in ("Reference:", "Amount:", "Destination:", "Status:", "WITHDRAWAL_COMPLETED"):
            self.assertIn(token, out)

    def test_short_numbers_preserved(self):
        out = sanitise_text("Funds arrive in 3 business days (ref year 2026).")
        self.assertIn("3", out)
        self.assertIn("2026", out)   # 4-digit, below the default account threshold -> preserved

    def test_non_str_is_coerced_not_raised(self):
        self.assertEqual(sanitise_text(None), "")


class ResidualNumericRuns(SimpleTestCase):
    def test_surfaces_surviving_identifier_like_runs(self):
        # A 4-digit standalone run is below the account-mask threshold (5) but is surfaced for human review.
        runs = residual_numeric_runs(sanitise_text("Ticket 4821 remains"))
        self.assertIn("4821", runs)

    def test_small_amount_below_threshold_not_flagged(self):
        runs = residual_numeric_runs(sanitise_text("Amount: 24.12 USD"))
        self.assertEqual(runs, [])   # both parts are <3 digits

    def test_decimal_glued_identifier_is_surfaced_even_though_masker_skips_it(self):
        # THE REVIEW NET IS BROADER THAN THE MASKER: a <digits>.<digits> machine reference (date.sequence, or an
        # account glued to a decimal amount) is left by the masker (it looks like a decimal), but MUST still be
        # surfaced for a human to redact — the net does not share the masker's decimal blind spot.
        body = sanitise_text("txn 20261014.123456 ref")
        self.assertIn("20261014", body)                       # masker preserved it (looked like a decimal)
        runs = residual_numeric_runs(body)
        self.assertIn("20261014", runs)                       # but the review net surfaces it
        self.assertIn("123456", runs)


class ResidualTokenCandidates(SimpleTestCase):
    def test_short_mixed_alnum_code_is_surfaced(self):
        runs = residual_token_candidates("confirmation code X9fK2mQ7 applies")
        self.assertIn("X9fK2mQ7", runs)

    def test_plain_words_and_enums_and_pure_numbers_not_flagged(self):
        runs = residual_token_candidates("WITHDRAWAL_COMPLETED withdrawal 123456 USD")
        self.assertEqual(runs, [])   # no letter+digit mixed run


class HeaderAndFixture(SimpleTestCase):
    def test_header_reduces_addresses_to_domain(self):
        h = sanitise_header_block(from_address="noreply@tradersway.com",
                                  to_addresses=("member@gmail.com",), subject="Dear John, your withdrawal",
                                  date="2026-10-14", auth_verdict="pass")
        self.assertEqual(h["from_domain"], "tradersway.com")
        self.assertEqual(h["to_domains"], ["gmail.com"])
        self.assertIn("Dear <NAME>,", h["subject"])
        self.assertEqual(h["auth_verdict"], "pass")

    def test_extract_fixture_shape(self):
        fx = extract_fixture(from_address="noreply@tradersway.com", to_addresses=("m@gmail.com",),
                             subject="Withdrawal update", body="Dear Jane,\nAccount 51763830\nAmount: 500.00 USD",
                             date="2026-10-14", auth_verdict="pass")
        self.assertIn("<NAME>", fx["body"])
        self.assertIn("<ACCT:8>", fx["body"])
        self.assertIn("500.00", fx["body"])          # amount preserved
        self.assertEqual(fx["header"]["from_domain"], "tradersway.com")
        self.assertEqual(fx["min_account_digits"], 5)
