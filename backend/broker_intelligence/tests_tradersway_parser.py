"""WP4 — TradersWay parser #1 + transaction classification + the internal-transfer NEGATIVE regression.

The headline Sponsor-safety test (packet 2026-10-08 §7/§8): the genuine TradersWay "Please confirm your withdrawal
request" email was an INTERNAL TRANSFER. It must NOT classify as an external withdrawal, must NOT create an external
Withdrawal / count / duration, must NOT emit the confirmation URL/token, and the raw evidence must still be stored.
Positive external-withdrawal fixtures are explicitly SYNTHETIC (labelled) pending a genuine external sample.
"""
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.utils import InternalError, ProgrammingError
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import ingestion, parsers, redaction
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import BrokerEvent, TransactionCategory
from broker_intelligence.parsers_tradersway import TradersWayParser, register_default_parsers
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "93%04d" % _n


def _acct(**kw):
    login = _uniq()
    u = User.objects.create_user(username="tw%s" % login, email="%s@x.invalid" % login, password="x")
    d = dict(user=u, name="A", broker_name="TradersWay", account_number=login, is_demo=True, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


# ---- The genuine internal-transfer email, reconstructed from the Sponsor-provided REAL fields (packet §7). The
# ---- confirmation URL/token is deliberately a placeholder here (the real token was NOT provided and is sensitive).
REAL_INTERNAL_TRANSFER = dict(
    from_address="payments@tradersway.com",
    subject="Please confirm your withdrawal request",
    body=("Dear Client,\n\nPlease confirm your withdrawal request for trading account 55442.\n"
          "Amount: 200.00 USD\nReference: 4112808\n\n"
          "Confirm here: https://tradersway.com/confirm?t=PLACEHOLDER_TOKEN_647390_do_not_follow\n\n"
          "Thank you, TradersWay Payments"))

# SYNTHETIC "external-sounding" prose fixture (labelled). In V1 this must NOT auto-classify EXTERNAL_WITHDRAWAL —
# the parser never derives external from prose; the positive path is certified against a genuine external sample.
SYNTHETIC_EXTERNAL_PROSE = dict(
    from_address="payments@tradersway.com",
    subject="Your withdrawal request has been received",
    body=("Your withdrawal request has been received for trading account 55442.\n"
          "Amount: 150.00 USD withdrawn to your bank account ending 4321.\nReference: 9900001\n"))


class TransactionClassifierTests(TestCase):
    def setUp(self):
        self.p = TradersWayParser()

    def test_internal_transfer_email_is_not_external_withdrawal(self):
        # THE negative regression: "confirm your withdrawal request" with no external-destination evidence.
        parsed = self.p.parse(**REAL_INTERNAL_TRANSFER)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")      # lifecycle recognised
        self.assertNotEqual(parsed.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)  # NEVER external
        self.assertEqual(parsed.transaction_category, TransactionCategory.UNKNOWN)   # ambiguous -> UNKNOWN, not guessed
        self.assertEqual(parsed.broker_reference_id, "4112808")                      # the reference, NOT a token
        self.assertEqual(parsed.amount, Decimal("200.00"))
        self.assertEqual(parsed.currency, "USD")

    def test_no_confirmation_url_or_token_in_any_emitted_field(self):
        parsed = self.p.parse(**REAL_INTERNAL_TRANSFER)
        for field in (parsed.broker_reference_id, parsed.currency, parsed.broker, parsed.event_type,
                      parsed.transaction_category):
            self.assertFalse(redaction.contains_sensitive(field or ""),
                             f"emitted field leaked sensitive material: {field!r}")

    def test_prose_never_classifies_external(self):
        # V1 invariant: the parser has NO prose->EXTERNAL path. Even strongly external-sounding prose must NOT
        # auto-classify EXTERNAL_WITHDRAWAL (that is certified against a genuine sample). Lifecycle still parses.
        parsed = self.p.parse(**SYNTHETIC_EXTERNAL_PROSE)
        self.assertEqual(parsed.event_type, "WITHDRAWAL_REQUESTED")
        self.assertNotEqual(parsed.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)
        self.assertEqual(parsed.amount, Decimal("150.00"))
        self.assertEqual(parsed.currency, "USD")

    def test_no_prose_input_ever_yields_external(self):
        # Battery of the residual-bypass triggers the re-review found. NONE may classify EXTERNAL_WITHDRAWAL.
        bodies = [
            "Please confirm your withdrawal request for account 55442. Amount: 200.00 USD\n"
            "These funds will be moved internally, rather than withdrawn to your bank account.\n",
            "Please confirm your withdrawal request for account 55442. Amount: 200.00 USD\n"
            "Your funds have been transferred to your wallet.\n",
            "Please confirm your withdrawal request for account 55442. Amount: 200.00 USD\n"
            "We moved your money from one of your profiles to the linked one. Normally funds are sent to your bank account.\n",
            "Please confirm your withdrawal request for account 55442. Amount: 200.00 USD\n"
            "FAQ: withdrawals are normally sent to your bank account within 3 days.\n",
            "Amount: 150.00 USD credited to your trading account instead of sent to your bank account.\n",
        ]
        for b in bodies:
            parsed = self.p.parse(from_address="payments@tradersway.com",
                                  subject="Please confirm your withdrawal request", body=b)
            if parsed is not None:
                self.assertNotEqual(parsed.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL,
                                    f"prose over-claimed EXTERNAL: {b!r}")

    def test_european_decimal_amount_not_fragment_grabbed(self):
        parsed = self.p.parse(from_address="payments@tradersway.com",
                              subject="Please confirm your withdrawal request",
                              body="Please confirm your withdrawal request for account 55442. Amount: 12,34 EUR\n")
        self.assertEqual(parsed.amount, Decimal("12.34"))        # not 34
        self.assertEqual(parsed.currency, "EUR")

    def test_no_amount_label_yields_none(self):
        parsed = self.p.parse(from_address="payments@tradersway.com",
                              subject="Please confirm your withdrawal request",
                              body="Your withdrawal from MT5 account 55442 USD is awaiting confirmation.\n")
        self.assertIsNone(parsed.amount)                         # no 'Amount:' label -> never grab the account number
        self.assertEqual(parsed.transaction_category, TransactionCategory.UNKNOWN)

    def test_word_withdrawal_alone_is_not_external(self):
        parsed = self.p.parse(from_address="payments@tradersway.com",
                              subject="Withdrawal request received",
                              body="We received your withdrawal request for account 55442. Amount: 50.00 USD.")
        self.assertEqual(parsed.transaction_category, TransactionCategory.UNKNOWN)   # no external destination -> not external

    # --- Regressions for the WP4 review CRITICAL: classifier must be negation-aware + not asymmetric. ---
    def test_negated_bank_account_boilerplate_is_not_external(self):
        # "between your MT5 accounts, not to any bank account" — internal phrasing + a negated external token.
        parsed = self.p.parse(
            from_address="payments@tradersway.com",
            subject="Please confirm your withdrawal request",
            body=("Please confirm your withdrawal request for trading account 55442.\nAmount: 200.00 USD\n"
                  "Note: funds will be moved between your MT5 accounts, not to any bank account.\n"))
        self.assertNotEqual(parsed.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)
        self.assertEqual(parsed.transaction_category, TransactionCategory.INTERNAL_TRANSFER)

    def test_security_footer_bank_mention_is_not_external(self):
        parsed = self.p.parse(
            from_address="payments@tradersway.com",
            subject="Please confirm your withdrawal request",
            body=("Please confirm your withdrawal request for account 55442. Amount: 200.00 USD\n"
                  "Security notice: TradersWay will never email you to confirm your bank account details.\n"))
        self.assertNotEqual(parsed.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)

    def test_payment_method_boilerplate_is_not_external(self):
        parsed = self.p.parse(
            from_address="payments@tradersway.com", subject="Please confirm your withdrawal request",
            body="Please confirm your withdrawal request for account 55442. Amount: 200.00 USD\nManage your payment method in the portal.\n")
        self.assertNotEqual(parsed.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)

    # --- Regressions for the WP4 review MEDIUM: amount extraction must not mis-grab / mis-scale / fabricate currency. ---
    def test_account_number_not_grabbed_as_amount(self):
        parsed = self.p.parse(
            from_address="payments@tradersway.com", subject="Please confirm your withdrawal request",
            body="Trading account 55442 USD\nWithdrawal amount: 200.00 USD\nReference: 4112808\n")
        self.assertEqual(parsed.amount, Decimal("200.00"))      # not 55442
        self.assertEqual(parsed.currency, "USD")

    def test_invalid_currency_token_not_fabricated(self):
        parsed = self.p.parse(
            from_address="payments@tradersway.com", subject="Please confirm your withdrawal request",
            body="Please confirm your withdrawal request for account 55442. Amount: 200.00 NET of fees\n")
        self.assertEqual(parsed.amount, Decimal("200.00"))
        self.assertEqual(parsed.currency, "")                   # "NET" is not a currency -> not fabricated

    def test_ambiguous_comma_amount_not_mis_scaled(self):
        parsed = self.p.parse(
            from_address="payments@tradersway.com", subject="Please confirm your withdrawal request",
            body="Please confirm your withdrawal request for account 55442. Amount: 200,00 USD\n")
        self.assertNotEqual(parsed.amount, Decimal("20000"))    # never a 100x mis-scale from a decimal comma

    def test_base64_token_and_schemeless_link_are_redacted(self):
        self.assertIn("[REDACTED]", redaction.redact("token A1b2C3d4+E5f6/G7h8=i9J0kLmNoPqR here"))
        self.assertIn("[REDACTED]", redaction.redact("confirm at tradersway.com/w/confirm/4112808abcDEF now"))
        # Scheme-less query-only and :port confirmation links (re-review #9) must also be masked.
        self.assertIn("[REDACTED]", redaction.redact("Confirm at tradersway.com?confirm=yes&id=4112808 now"))
        self.assertIn("[REDACTED]", redaction.redact("go to tradersway.com:8443/confirm/abc now"))
        self.assertFalse(redaction.contains_sensitive("WITHDRAWAL_CONFIRMATION_REQUIRED"))   # enum not over-redacted
        self.assertFalse(redaction.contains_sensitive("4112808"))                            # numeric ref not redacted
        self.assertFalse(redaction.contains_sensitive("support@guvfx.com"))                  # email not masked as a link

    def test_non_tradersway_sender_not_claimed(self):
        self.assertFalse(self.p.can_parse(from_address="payments@someonelse.com",
                                          subject="withdrawal", body="x"))

    def test_redaction_masks_urls_and_tokens(self):
        red = redaction.redact(REAL_INTERNAL_TRANSFER["body"])
        self.assertNotIn("PLACEHOLDER_TOKEN", red)
        self.assertNotIn("https://tradersway.com/confirm", red)
        self.assertIn("[REDACTED]", red)


class _TmpEvidence(TestCase):
    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp(prefix="bi-wp4-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()
        register_default_parsers()                      # register the real TradersWay parser for the pipeline

    def tearDown(self):
        parsers.clear_registry()
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)
        super().tearDown()


class InternalTransferIngestionTests(_TmpEvidence):
    def _msg(self, spec):
        raw = (f"From: {spec['from_address']}\r\nSubject: {spec['subject']}\r\n\r\n{spec['body']}").encode()
        return MailMessage(raw_bytes=raw, to_addresses=("ba" + "0" * 32 + "@accounts.guvfx.com",),
                           from_address=spec["from_address"], subject=spec["subject"], body=spec["body"],
                           received_at=timezone.now())

    def test_internal_transfer_ingests_as_non_external_with_evidence_retained(self):
        ev = ingestion.ingest_message(self._msg(REAL_INTERNAL_TRANSFER), store=self.store)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertNotEqual(ev.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)  # no external Withdrawal
        self.assertEqual(ev.transaction_category, TransactionCategory.UNKNOWN)
        self.assertEqual(ev.broker_reference_id, "4112808")
        self.assertIsNotNone(ev.evidence)                                           # raw evidence preserved
        # No emitted BrokerEvent field carries the confirmation URL/token.
        for field in (ev.broker_reference_id, ev.currency, ev.broker):
            self.assertFalse(redaction.contains_sensitive(field or ""))

    def test_external_withdrawal_count_excludes_internal(self):
        ingestion.ingest_message(self._msg(REAL_INTERNAL_TRANSFER), store=self.store)
        # The withdrawal projection (WP5) counts ONLY EXTERNAL_WITHDRAWAL; the internal transfer must be excluded.
        ext = BrokerEvent.objects.filter(transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL).count()
        self.assertEqual(ext, 0)

    def test_transaction_category_is_db_immutable(self):
        ev = ingestion.ingest_message(self._msg(REAL_INTERNAL_TRANSFER), store=self.store)
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                BrokerEvent.objects.filter(pk=ev.pk).update(
                    transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL)   # evidential -> trigger blocks it
