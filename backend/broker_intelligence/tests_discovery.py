"""ADR-0089 — pure discovery-classifier + REDACTION-CONTRACT tests (no DB). Strict pre-retention gates, negatives,
multilingual, HTML, malformed-fail-closed, and the formally-tested guarantee that a redacted record carries no PII.
SYNTHETIC fixtures only."""
from __future__ import annotations

from django.test import SimpleTestCase

from broker_intelligence import discovery as d


def _c(subject="", body="", *, domain="newbroker.io", auth="pass", html=""):
    return d.classify_discovery(subject=subject, body=body, from_domain=domain, auth_verdict=auth, html_text=html)


class Gates(SimpleTestCase):
    def test_unknown_authenticated_completed_qualifies(self):
        r = _c("Withdrawal completed", "your withdrawal has been completed")
        self.assertTrue(r.qualifies)
        self.assertEqual(r.lifecycle_class, d.gdx.S_COMPLETED)
        self.assertEqual(r.confidence_band, "HIGH")

    def test_confirmation_and_processing_qualify_high(self):
        self.assertEqual(_c("Confirm withdrawal", "please confirm your withdrawal").confidence_band, "HIGH")
        self.assertEqual(_c("Withdrawal", "your withdrawal is being processed").confidence_band, "HIGH")

    def test_auth_fail_no_candidate(self):
        self.assertFalse(_c("Withdrawal completed", "completed", auth="fail").qualifies)
        self.assertFalse(_c("Withdrawal completed", "completed", auth=None).qualifies)
        self.assertFalse(_c("Withdrawal completed", "completed", auth="").qualifies)

    def test_intent_without_explicit_lifecycle_fails_closed(self):
        r = _c("About your withdrawal", "we are writing regarding your withdrawal")
        self.assertFalse(r.qualifies)
        self.assertIn(d.gdx.RC_NO_EXPLICIT_LIFECYCLE, r.reason_codes)

    def test_contradictory_terminal_no_candidate(self):
        self.assertFalse(_c("Withdrawal", "has been completed and has been cancelled").qualifies)


class Negatives(SimpleTestCase):
    def test_marketing_newsletter(self):
        self.assertFalse(_c("Trade now", "webinar bonus offer, unsubscribe here, market update").qualifies)

    def test_deposit(self):
        self.assertFalse(_c("Deposit received", "your deposit completed").qualifies)

    def test_trade_confirmation(self):
        self.assertFalse(_c("Trade confirmation", "your order filled, position closed").qualifies)

    def test_account_statement(self):
        self.assertFalse(_c("Your monthly statement", "your account statement is ready").qualifies)

    def test_login_alert(self):
        self.assertFalse(_c("New login", "new sign-in to your account, security alert").qualifies)

    def test_kyc_only(self):
        self.assertFalse(_c("Verify your identity", "please complete your verification, kyc document request").qualifies)

    def test_ambiguous_funds_language_without_lifecycle(self):
        # "funds" alone, no explicit withdrawal lifecycle -> not a candidate (conservative)
        self.assertFalse(_c("Your funds", "information about your funds").qualifies)


class Multilingual(SimpleTestCase):
    def test_multilingual_completion(self):
        self.assertTrue(_c("Auszahlung", "ihre auszahlung abgeschlossen").qualifies)
        self.assertTrue(_c("출금", "인출 완료되었습니다").qualifies)


class HtmlAndMalformed(SimpleTestCase):
    def test_html_variant(self):
        r = _c("Withdrawal", "", html="<html><body><p>Your withdrawal has been completed</p></body></html>")
        self.assertTrue(r.qualifies)
        self.assertEqual(r.lifecycle_class, d.gdx.S_COMPLETED)

    def test_malformed_input_fails_closed(self):
        self.assertFalse(d.classify_discovery(subject=None, body=None, from_domain=None, auth_verdict=None).qualifies)

    def test_subject_prefilter(self):
        self.assertTrue(d.subject_has_withdrawal_keyword("Your withdrawal is ready"))
        self.assertFalse(d.subject_has_withdrawal_keyword("Your monthly newsletter"))


class RedactionContract(SimpleTestCase):
    """The formally-tested redaction contract: a message full of PII yields a record with NONE of it."""

    RAW = (b"From: John Smith <john.smith@newbroker.io>\r\nSubject: Withdrawal completed\r\n"
           b"Authentication-Results: mx.google.com; dmarc=pass; spf=pass; dkim=pass\r\n\r\n"
           b"Dear John Smith, your withdrawal of GBP 42.00 to account 99887766 (ref WX123ABC) "
           b"has been completed and sent to your bank.")
    PII = ["John", "Smith", "john.smith", "99887766", "WX123ABC", "42.00", "has been completed",
           "Dear John", "sent to your bank"]

    def _record(self):
        r = d.classify_discovery(subject="Withdrawal completed",
                                 body="Dear John Smith, your withdrawal of GBP 42.00 to account 99887766 "
                                      "(ref WX123ABC) has been completed and sent to your bank.",
                                 from_domain="newbroker.io", auth_verdict="pass", spf="pass", dkim="pass", dmarc="pass")
        return r, d.build_discovery_record(owner=None, raw_bytes=self.RAW, provider_message_id="gmail-abc-123",
                                           result=r, received_at=None)

    def test_record_has_no_pii(self):
        _, rec = self._record()
        ok, bad = d.validate_discovery_record_clean(rec, forbidden_substrings=self.PII)
        self.assertTrue(ok, "redaction leak: %s" % bad)

    def test_record_fields_are_only_redacted(self):
        _, rec = self._record()
        # registrable domain is the broker's PUBLIC domain (not PII); everything else is a hash/enum/verdict.
        self.assertEqual(rec["registrable_domain"], "newbroker.io")
        self.assertEqual(len(rec["message_sha256"]), 64)
        self.assertEqual(len(rec["provider_message_id_hash"]), 64)
        self.assertEqual(len(rec["sender_domain_hash"]), 64)
        self.assertEqual(rec["confidence_band"], "HIGH")
        self.assertEqual(rec["lifecycle_class"], d.gdx.S_COMPLETED)
        # the message-id hash must NOT be the raw id, and the domain hash must not be the raw domain
        self.assertNotIn("gmail-abc-123", rec["provider_message_id_hash"])
        self.assertNotIn("newbroker", rec["sender_domain_hash"])

    def test_unauthenticated_domain_not_stored(self):
        r = d.classify_discovery(subject="Withdrawal completed", body="has been completed",
                                 from_domain="spoofed.io", auth_verdict="fail")
        rec = d.build_discovery_record(owner=None, raw_bytes=self.RAW, provider_message_id="x", result=r,
                                       received_at=None)
        self.assertEqual(rec["registrable_domain"], "")   # domain withheld unless auth-approved
        self.assertFalse(rec["auth_policy_passed"])


class AdversarialRegression(SimpleTestCase):
    """ADR-0089 adversarial regression (review LOW findings)."""

    def test_deposit_with_completion_phrase_is_not_a_withdrawal(self):
        # a generic completion phrase ('has been completed') with NO withdrawal WORD must not qualify
        self.assertFalse(_c("Deposit", "your deposit has been completed").qualifies)
        self.assertFalse(_c("Payment", "your payment has been completed").qualifies)

    def test_auth_verdicts_parsed_from_header_not_body(self):
        raw = (b"From: x <a@x.io>\r\nSubject: s\r\nAuthentication-Results: mx.google.com; spf=fail\r\n\r\n"
               b"Nice try: dkim=pass dmarc=pass spf=pass embedded in the body.")
        spf, dkim, dmarc = d.extract_auth_results(raw.decode())
        self.assertEqual(spf, "fail")        # from the Authentication-Results header
        self.assertEqual(dkim, "")           # body 'dkim=pass' ignored
        self.assertEqual(dmarc, "")           # body 'dmarc=pass' ignored


class VantageTerminologyRegression(SimpleTestCase):
    """ADR-0089 live-observation regression (Vantage): submission/acceptance terminology recognised; footer
    boilerplate ('unsubscribe'/'trade now'/'login') in the BODY must not cause a false negative; 'request
    successful' stays conservative (REQUESTED, never COMPLETED). SYNTHETIC only."""
    FOOTER = " Unsubscribe here | Trade now | Login to your account | View statement"

    def test_submitted_stage_recognised_despite_footer(self):
        r = _c("Withdrawal Submitted", "your withdrawal has been submitted and is being reviewed." + self.FOOTER)
        self.assertTrue(r.qualifies)
        self.assertEqual(r.lifecycle_class, d.gdx.S_REQUESTED)

    def test_request_successful_is_requested_not_completed(self):
        r = _c("Withdrawal Request Successful", "your withdrawal request was successful." + self.FOOTER)
        self.assertTrue(r.qualifies)
        self.assertEqual(r.lifecycle_class, d.gdx.S_REQUESTED)       # acceptance of the REQUEST, NEVER completed
        self.assertNotEqual(r.lifecycle_class, d.gdx.S_COMPLETED)

    def test_body_unsubscribe_does_not_reject_a_genuine_withdrawal(self):
        # the EXACT Vantage failure: 'unsubscribe' in the footer must not reject a genuine withdrawal
        self.assertTrue(_c("Withdrawal Submitted", "your withdrawal request" + self.FOOTER).qualifies)

    def test_marketing_subject_still_rejected(self):
        self.assertFalse(_c("Trade now special offer", "withdrawal bonus unsubscribe").qualifies)

    def test_statement_subject_still_rejected(self):
        self.assertFalse(_c("Your account statement", "withdrawals included unsubscribe").qualifies)

    def test_deposit_still_rejected(self):
        self.assertFalse(_c("Deposit received", "your deposit has been completed").qualifies)
