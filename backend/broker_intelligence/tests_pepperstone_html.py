"""Packet 008 — HTML-aware Pepperstone withdrawal-confirmation adapter tests.

Every fixture here is SYNTHETIC and PII-FREE: invented names, account numbers, references, amounts. No genuine sample
is reproduced. These exercise the structural HTML path (``html_body``) added so the parser reads the withdrawal-detail
VALUES — which the broker's degraded text/plain alternative drops — from the text/html label/spacer/value table, with
smart-punctuation folding and an approve/cancel confirmation-required classifier. Positive AND negative controls, so a
regression (stops masking structure, mis-classifies, over-claims externality, or fabricates a value) fails here.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from broker_intelligence.parsers_stream_b import PEPPERSTONE

PS_FROM = "no-reply@pepperstone.com"

# A synthetic row mirroring the genuine template: <label td><spacer td>&nbsp;<value td>, label in <strong>, value next.
_ROW = ('<tr><td style="font-weight:bold"><strong>{label}</strong></td>'
        '<td width="64" class="spechide">&nbsp;</td>'
        '<td align="left" style="font-weight:300">{value}</td></tr>')


def _pep_html(*, salutation="Hi Jordan Alvarez,", intro="We’ve received your withdrawal request",
              lifecycle_block=("We’re about to process your request but want to make sure all your details are "
                               "correct.<br>Approve or Cancel the withdrawal request and we’ll handle the rest. "
                               "If we don’t hear back from you within 1 hour, your withdrawal confirmation will "
                               "be cancelled."),
              rows=None, actions="<p>Approve withdrawal ( # ) Cancel withdrawal ( # )</p>",
              extra_langs=False):
    """Build a synthetic Pepperstone-style HTML body. ``rows`` is a list of (label, value) detail rows."""
    if rows is None:
        rows = [("Withdrawal method", "Bank Wire"), ("Name", "Jordan Alvarez"),
                ("Trading account number", "99887766"), ("Withdrawal amount", "250.00"),
                ("Currency", "EUR"), ("Reference number", "WX12345678")]
    detail = "".join(_ROW.format(label=l, value=v) for l, v in rows)
    langs = ""
    if extra_langs:
        # Repeat the same content in two other languages (dedup must not change the classification/values).
        langs = ('<div>Auszahlungsantrag Sehr geehrte/r <strong>Jordan Alvarez</strong>, Genehmigen oder stornieren '
                 'Sie die Auszahlung.</div>'
                 '<div>提款申请 您好<strong>Jordan Alvarez</strong>，</div>')
    return ("<html><head><style>.x{color:#fff}</style></head><body>"
            "<div><p>" + intro + "</p><p>" + salutation + "</p>" + langs + "<p>" + lifecycle_block + "</p>"
            "<h2>Withdrawal details</h2><table border='0'>" + detail + "</table>" + actions +
            "<p>Kind regards, <strong>The Pepperstone Team</strong></p></div></body></html>")


class PepperstoneHtmlConfirmationRequired(SimpleTestCase):
    def test_full_confirmation_required_external_withdrawal(self):
        ev = PEPPERSTONE.parse(subject="We’ve received your withdrawal request", body="",
                               from_address=PS_FROM, html_body=_pep_html())
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertEqual(ev.transaction_category, "EXTERNAL_WITHDRAWAL")
        self.assertEqual(ev.amount, Decimal("250.00"))
        self.assertEqual(ev.currency, "EUR")                           # NO GBP assumption
        self.assertEqual(ev.source_account, "99887766")
        self.assertEqual(ev.broker_reference_id, "WX12345678")

    def test_never_completed_from_confirmation_email(self):
        ev = PEPPERSTONE.parse(subject="We’ve received your withdrawal request", body="",
                               from_address=PS_FROM, html_body=_pep_html())
        self.assertNotEqual(ev.event_type, "WITHDRAWAL_COMPLETED")

    def test_curly_apostrophe_subject_classifies(self):
        # MUTATION-KILLER: the genuine subject uses U+2019; without smart-punctuation folding the anchor would miss.
        ev = PEPPERSTONE.parse(subject="We’ve received your withdrawal request", body="",
                               from_address=PS_FROM, html_body=_pep_html())
        self.assertIsNotNone(ev)

    def test_multilingual_blocks_single_classification_and_values(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM,
                               html_body=_pep_html(extra_langs=True))
        self.assertEqual(ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertEqual(ev.transaction_category, "EXTERNAL_WITHDRAWAL")
        self.assertEqual(ev.amount, Decimal("250.00"))
        self.assertEqual(ev.currency, "EUR")

    def test_amount_currency_no_gbp_assumption(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM,
                               html_body=_pep_html(rows=[("Withdrawal method", "Skrill"),
                                                         ("Withdrawal amount", "1,234.56"), ("Currency", "USD"),
                                                         ("Reference number", "RF99")]))
        self.assertEqual(ev.amount, Decimal("1234.56"))
        self.assertEqual(ev.currency, "USD")


class PepperstoneHtmlFailClosedAndUnderClaim(SimpleTestCase):
    def test_currency_conflict_fails_closed(self):
        # Amount field embeds USD but the Currency field says EUR -> inconsistent money evidence -> decline (None).
        ev = PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM,
                               html_body=_pep_html(rows=[("Withdrawal method", "Bank Wire"),
                                                         ("Withdrawal amount", "USD 250.00"), ("Currency", "EUR"),
                                                         ("Reference number", "RF1")]))
        self.assertIsNone(ev)

    def test_missing_amount_is_none_not_fabricated(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM,
                               html_body=_pep_html(rows=[("Withdrawal method", "Bank Wire"),
                                                         ("Currency", "EUR"), ("Reference number", "RF2")]))
        self.assertEqual(ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertIsNone(ev.amount)                                   # not fabricated

    def test_missing_method_category_underclaims_unknown(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM,
                               html_body=_pep_html(rows=[("Withdrawal amount", "50.00"), ("Currency", "EUR"),
                                                         ("Reference number", "RF3")]))
        self.assertEqual(ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertEqual(ev.transaction_category, "UNKNOWN")           # no rail -> under-claim, never over-claim

    def test_missing_account_leaves_source_empty(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM,
                               html_body=_pep_html(rows=[("Withdrawal method", "Bank Wire"),
                                                         ("Withdrawal amount", "50.00"), ("Currency", "EUR")]))
        self.assertEqual(ev.source_account, "")

    def test_deposit_method_not_external(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM,
                               html_body=_pep_html(
                                   lifecycle_block="Approve or Cancel the withdrawal request within 1 hour.",
                                   rows=[("Withdrawal method", "Store credit"), ("Withdrawal amount", "50.00"),
                                         ("Currency", "EUR")]))
        self.assertEqual(ev.transaction_category, "UNKNOWN")           # unrecognised rail -> not external


class PepperstoneHtmlLifecycleDistinctness(SimpleTestCase):
    def _html(self, lc):
        return ("<html><body><p>%s</p><h2>Withdrawal details</h2><table>" % lc
                + _ROW.format(label="Withdrawal method", value="Bank Wire")
                + _ROW.format(label="Withdrawal amount", value="50.00")
                + _ROW.format(label="Currency", value="EUR") + "</table></body></html>")

    def test_completed_not_confused_with_confirmation(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal completed", body="", from_address=PS_FROM,
                               html_body=self._html("Your withdrawal has been completed and funds sent."))
        self.assertEqual(ev.event_type, "WITHDRAWAL_COMPLETED")

    def test_rejected_not_confused_with_confirmation(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal rejected", body="", from_address=PS_FROM,
                               html_body=self._html("Your withdrawal request has been rejected."))
        self.assertEqual(ev.event_type, "WITHDRAWAL_REJECTED")

    def test_cancelled_status_not_confused_with_confirmation(self):
        ev = PEPPERSTONE.parse(subject="Withdrawal cancelled", body="", from_address=PS_FROM,
                               html_body=self._html("Your withdrawal request has been cancelled."))
        self.assertEqual(ev.event_type, "WITHDRAWAL_CANCELLED")


class PepperstoneHtmlRobustness(SimpleTestCase):
    def test_malformed_html_does_not_crash(self):
        # A pathological body must never crash ingestion (contract) — returns an event or None, never raises.
        for bad in ("<html><body><table><tr><td>Withdrawal amount", "<<>>not really html Approve or cancel withdrawal",
                    "<td>Currency</td><td>", ""):
            PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM, html_body=bad)

    def test_wrong_sender_domain_refused(self):
        self.assertFalse(PEPPERSTONE.can_parse(subject="x", body="withdrawal", from_address="no-reply@pepperst0ne.com"))

    def test_no_html_uses_text_path(self):
        # With no html_body the legacy text path still classifies a plain-text confirmation (backward compatible).
        ev = PEPPERSTONE.parse(subject="Action required", body="Please confirm your withdrawal request.\n"
                               "Amount: GBP 15.00\nWithdrawal method: Visa card\n", from_address=PS_FROM)
        self.assertEqual(ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertEqual(ev.transaction_category, "EXTERNAL_WITHDRAWAL")

    def test_no_name_value_pulled_as_a_field(self):
        # The Name field value must never be emitted into any parsed field (it is PII).
        ev = PEPPERSTONE.parse(subject="Withdrawal request", body="", from_address=PS_FROM, html_body=_pep_html())
        for v in (ev.broker_reference_id, ev.source_account, ev.currency or ""):
            self.assertNotIn("Jordan", v)
            self.assertNotIn("Alvarez", v)


class ParseRfc822ExposesHtmlBody(SimpleTestCase):
    """parse_rfc822 must EXPOSE the text/html part additively while keeping ``body`` = text/plain (the parser then
    reads structured values the degraded text/plain alternative drops)."""

    def test_multipart_alternative_exposes_html_and_keeps_plain_body(self):
        from broker_intelligence.gmail_source import parse_rfc822
        raw = (b"From: no-reply@pepperstone.com\r\nTo: member@example.com\r\n"
               b"Subject: We received your withdrawal request\r\n"
               b"MIME-Version: 1.0\r\n"
               b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
               b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
               b"*Withdrawal amount*\r\n*Currency*\r\n"            # value-less plain alternative
               b"\r\n--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
               b"<html><body><table><tr><td>Withdrawal amount</td><td>&nbsp;</td><td>42.00</td></tr>"
               b"<tr><td>Currency</td><td>&nbsp;</td><td>EUR</td></tr></table></body></html>\r\n--B--\r\n")
        f = parse_rfc822(raw)
        self.assertIn("Withdrawal amount", f["body"])              # plain part chosen for body
        self.assertNotIn("42.00", f["body"])                      # ...which lacks the value
        self.assertIn("42.00", f["html_body"])                    # html part exposed, WITH the value
        self.assertIn("EUR", f["html_body"])

    def test_html_only_message_sets_html_body(self):
        from broker_intelligence.gmail_source import parse_rfc822
        raw = (b"From: no-reply@pepperstone.com\r\nTo: m@example.com\r\nSubject: s\r\n"
               b"Content-Type: text/html; charset=utf-8\r\n\r\n<html><body><p>Hi</p></body></html>\r\n")
        f = parse_rfc822(raw)
        self.assertIn("<p>Hi</p>", f["html_body"])
