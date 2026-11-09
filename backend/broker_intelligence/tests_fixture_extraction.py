"""§4-G — privacy-safe fixture-extraction unit tests (pure, synthetic; no genuine sample, no DB).

RULE-11 discipline: every masking claim has a POSITIVE control (the sensitive thing IS masked) AND a NEGATIVE
control (the structural thing is NOT over-masked), so a future change that silently stops masking — or starts
destroying template structure — fails here.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from broker_intelligence.fixture_extraction import (
    sanitise_text, residual_numeric_runs, residual_token_candidates, sanitise_header_block, extract_fixture,
    validate_pii_clean)


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
        # Packet 008: a LABELLED reference is now removed as <REF> (not <ACCT:n>) so references are redacted distinctly;
        # an UNLABELLED long digit run is still masked length-aware as <ACCT:n>.
        out = sanitise_text("Account 51763830 reference 4113188 processed")
        self.assertIn("<ACCT:8>", out)      # 8-digit account (unlabelled digit run)
        self.assertIn("<REF>", out)         # labelled reference removed
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
        # Neutral prefix ("serial" is not a reference label) so this still exercises the decimal blind-spot; a LABELLED
        # decimal-glued value (e.g. "txn 20261014...") is now actively removed as <REF> by the packet-008 masker.
        body = sanitise_text("serial 20261014.123456 end")
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


# ---------------------------------------------------------------------------------------------------------------------
# Packet 008 — personal-NAME redaction (the 007 leak was a name surviving in a multi-lingual HTML email). All data below
# is INVENTED; no genuine sample is reproduced. Each test is a mutation-killer with a positive control (the name IS
# gone) and, where structure matters, a negative control (labels/amounts survive).
# ---------------------------------------------------------------------------------------------------------------------
# A synthetic multi-lingual withdrawal-confirmation body AFTER HTML->text (what gmail_source.parse_rfc822 hands the
# masker): the same invented name recurs in several localised salutations and an explicit Name field.
_MULTILINGUAL_BODY = (
    "Withdrawal Request\n"
    "Dear Jane Doe,\n"
    "Sehr geehrte Frau Jane Doe,\n"
    "Estimada Jane Doe,\n"
    "您好 Jane Doe,\n"
    "Please approve or cancel this withdrawal within one hour.\n"
    "Name: Jane Doe\n"
    "Withdrawal method: Visa card\n"
    "Trading account number: 12345678\n"
    "Withdrawal amount: 15\n"
    "Currency: GBP\n"
    "Reference number: W1234567\n"
    "Contact: jane.doe@example.com  +44 20 7946 0958\n"
    "Approve withdrawal\n"
    "Cancel withdrawal\n")
_NAME_PARTS = ("Jane", "Doe", "jane", "doe")


class NameRedactionMultilingual(SimpleTestCase):
    def test_name_masked_in_every_localised_salutation_and_name_field(self):
        out = sanitise_text(_MULTILINGUAL_BODY, known_names=["Jane", "Doe"])
        for part in _NAME_PARTS:
            self.assertNotIn(part, out)
        # Each salutation lead survives as structure, with the name replaced by the placeholder. (An honorific adjacent
        # to the name, e.g. German "Frau", is itself over-masked into <NAME> — privacy-safe, structure still legible.)
        for lead in ("Dear <NAME>,", "Sehr geehrte <NAME>,", "Estimada <NAME>,", "您好 <NAME>,", "Name: <NAME>"):
            self.assertIn(lead, out)

    def test_mutation_english_only_salutation_would_leak_non_english_blocks(self):
        # MUTATION-KILLER: if the masker reverted to the old English-only "Dear <name>," rule, the name would survive
        # in the German/Spanish/Chinese salutations. Assert those are masked WITHOUT relying on an explicit known name,
        # so the test fails the moment multi-lingual self-discovery is removed.
        out = sanitise_text("Sehr geehrte Frau Ingrid Bauer,\nEstimada Ingrid Bauer,\n您好 Ingrid Bauer,",
                            known_names=[])
        self.assertNotIn("Ingrid", out)
        self.assertNotIn("Bauer", out)

    def test_structure_amount_currency_method_preserved(self):
        out = sanitise_text(_MULTILINGUAL_BODY, known_names=["Jane", "Doe"])
        for keep in ("Withdrawal Request", "Withdrawal method: Visa card", "Withdrawal amount: 15",
                     "Currency: GBP", "Approve withdrawal", "Cancel withdrawal"):
            self.assertIn(keep, out)


class NameSelfDiscovery(SimpleTestCase):
    def test_surname_discovered_from_message_when_guvfx_knows_first_name_only(self):
        # GuvFX records only the first name; the surname must be LEARNED from the message's own salutation/Name field
        # and removed in full. Mutation-killer for the self-discovery path.
        out = sanitise_text(_MULTILINGUAL_BODY, known_names=["Jane"])
        self.assertNotIn("Doe", out)
        self.assertNotIn("Jane", out)

    def test_known_name_masked_even_without_salutation(self):
        out = sanitise_text("Your account holder Olga Petrova confirmed the payout.", known_names=["Olga", "Petrova"])
        self.assertNotIn("Olga", out)
        self.assertNotIn("Petrova", out)

    def test_case_and_whitespace_variations(self):
        out = sanitise_text("dear   JANE    doe,\nregards, Jane  Doe", known_names=["Jane", "Doe"])
        for part in _NAME_PARTS + ("JANE",):
            self.assertNotIn(part, out)

    def test_name_split_by_markup_is_masked_when_known(self):
        # After tag-stripping, "Ja<b>ne</b>" -> "Ja ne"; the known-name char-flex pattern still masks it.
        out = sanitise_text("Dear Ja ne Doe,", known_names=["Jane", "Doe"])
        self.assertNotIn("Doe", out)
        self.assertNotIn("ne Doe", out)
        self.assertIn("<NAME>", out)

    def test_unicode_name_masked(self):
        out = sanitise_text("Dear Bjørn Solberg,\nName: Bjørn Solberg", known_names=["Bjørn", "Solberg"])
        self.assertNotIn("Bjørn", out)
        self.assertNotIn("Solberg", out)


class ReferencePhoneTokenRedaction(SimpleTestCase):
    def test_labelled_reference_removed(self):
        self.assertIn("<REF>", sanitise_text("Reference number: W1234567"))
        self.assertNotIn("W1234567", sanitise_text("Reference number: W1234567"))

    def test_confirmation_code_removed(self):
        self.assertNotIn("K9P2", sanitise_text("Confirmation code: K9P2QX"))

    def test_prose_after_label_word_not_masked(self):
        # A label word in prose with NO digit value must not be masked (the value must look like a reference).
        out = sanitise_text("We received your request today and will process it.")
        self.assertIn("request today", out)

    def test_phone_masked_amount_preserved(self):
        out = sanitise_text("Call +44 20 7946 0958. Amount: 1234.56 GBP")
        self.assertIn("<PHONE>", out)
        self.assertIn("1234.56", out)      # a decimal amount is NOT a phone number
        self.assertNotIn("7946", out)

    def test_mixed_alnum_token_masked(self):
        self.assertNotIn("X9fK2m", sanitise_text("code X9fK2m here"))


class FailClosedValidator(SimpleTestCase):
    def test_clean_sanitised_body_passes(self):
        out = sanitise_text(_MULTILINGUAL_BODY, known_names=["Jane", "Doe"])
        v = validate_pii_clean(subject="Withdrawal Request", body=out, known_names=["Jane", "Doe"])
        self.assertTrue(v["ok"], v["counts"])
        self.assertTrue(all(n == 0 for n in v["counts"].values()))

    def test_raw_body_fails_closed_with_categories(self):
        v = validate_pii_clean(subject="Withdrawal for Jane Doe", body=_MULTILINGUAL_BODY, known_names=["Jane", "Doe"])
        self.assertFalse(v["ok"])
        self.assertGreater(v["counts"]["known_or_discovered_name"], 0)
        self.assertGreater(v["counts"]["account_or_reference_number"], 0)
        self.assertGreater(v["counts"]["email"], 0)

    def test_residual_unknown_name_after_greeting_fails_closed(self):
        # A name GuvFX never knew, still present after a greeting (a redaction miss), is caught by the structural net.
        v = validate_pii_clean(body="Dear Hans Gruber, your request", known_names=[])
        self.assertFalse(v["ok"])
        self.assertGreater(v["counts"]["residual_salutation_or_field_name"], 0)

    def test_split_name_caught_in_collapsed_form(self):
        # "N u n o" must be detected as the known name "Nuno" via the collapsed/accent-folded comparison form.
        v = validate_pii_clean(body="hello N u n o here", known_names=["Nuno"])
        self.assertFalse(v["ok"])
        self.assertGreater(v["counts"]["known_or_discovered_name"], 0)

    def test_validator_reports_counts_only_no_values(self):
        v = validate_pii_clean(body=_MULTILINGUAL_BODY, known_names=["Jane", "Doe"])
        self.assertEqual(set(v.keys()), {"ok", "counts"})
        for val in v["counts"].values():
            self.assertIsInstance(val, int)


class HeaderNamePii(SimpleTestCase):
    def test_subject_with_name_and_email_is_masked(self):
        h = sanitise_header_block(from_address="noreply@pepperstone.com", to_addresses=("jane.doe@example.com",),
                                  subject="Withdrawal request for Jane Doe (jane.doe@example.com)",
                                  date="2026-11-05", auth_verdict="pass", known_names=["Jane", "Doe"])
        self.assertNotIn("Jane", h["subject"])
        self.assertNotIn("Doe", h["subject"])
        self.assertNotIn("jane.doe", h["subject"])
        self.assertEqual(h["to_domains"], ["example.com"])   # recipient reduced to domain only (no local-part)


class NonLatinNameRedaction(SimpleTestCase):
    """Packet-008 review (HIGH): the masker AND validate_pii_clean must handle NON-LATIN member names (CJK / Cyrillic /
    Greek / Arabic). A Latin-only implementation would leak them while reporting clean=True. All data INVENTED."""

    def test_cjk_name_masked_and_fail_closed_on_raw(self):
        raw = "提款申请 您好 张伟,\n姓名: 张伟\nAmount: 15.00\nCurrency: GBP"
        known = ["张", "伟", "张 伟"]        # first / last / full (invented CJK)
        out = sanitise_text(raw, known_names=known)
        self.assertNotIn("张伟", out)
        self.assertNotIn("张", out)
        self.assertIn("15.00", out)                          # amount shape preserved
        # MUTATION-KILLER: a Latin-only validator would pass the RAW body; it must FAIL closed.
        self.assertFalse(validate_pii_clean(body=raw, known_names=known)["ok"])
        self.assertTrue(validate_pii_clean(body=out, known_names=known)["ok"])

    def test_cyrillic_name_masked_and_fail_closed_on_raw(self):
        raw = "Здравствуйте, Иван Петров!"
        known = ["Иван", "Петров"]
        out = sanitise_text(raw, known_names=known)
        self.assertNotIn("Иван", out)
        self.assertNotIn("Петров", out)
        self.assertFalse(validate_pii_clean(body=raw, known_names=known)["ok"])
        self.assertTrue(validate_pii_clean(body=out, known_names=known)["ok"])

    def test_collapse_keeps_unicode_letters(self):
        # The collapsed comparison form must retain non-Latin letters (a Latin-only [^0-9a-z] strip would drop them).
        from broker_intelligence.fixture_extraction import _collapse
        self.assertIn("张伟", _collapse("张 伟"))
        self.assertEqual(_collapse("Иван"), "иван")   # casefolded cyrillic


class LatinNameAdjacentToNonLatinScript(SimpleTestCase):
    """Packet-008 review follow-up: a LATIN member name written directly against a non-Latin script with NO space
    (a CJK salutation '您好Jane,') must still be bounded and masked, and the validator must fail closed if it survives.
    Guards the Latin-letter word boundary (an all-Unicode-letter boundary would treat '好Jane' as one word and leak)."""

    def test_latin_name_directly_after_cjk_greeting_is_masked(self):
        out = sanitise_text("您好Jane,\nName: Jane Doe", known_names=["Jane", "Doe"])
        self.assertNotIn("Jane", out)
        self.assertIn("<NAME>", out)
        self.assertFalse(validate_pii_clean(body="您好Jane,", known_names=["Jane", "Doe"])["ok"])
        self.assertTrue(validate_pii_clean(body=out, known_names=["Jane", "Doe"])["ok"])
