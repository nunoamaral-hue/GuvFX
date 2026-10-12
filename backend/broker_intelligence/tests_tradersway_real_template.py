"""Phase-4 regression for the genuine TradersWay internal-transfer template (captured 2026-10-12), REDACTED.

The fixture reproduces the real email's STRUCTURE — HTML-only multipart/related body, Gmail Authentication-Results
with an aligned DKIM pass + DMARC fail, and the transfer-template grammar — with the recipient name, confirmation
URL and token removed. It certifies: aligned-DKIM auth on this shape, INTERNAL_TRANSFER classification, amount/
reference/source/destination extraction, NO external withdrawal, transaction-account attribution (match vs
UNRESOLVED on mismatch), cross-user isolation, idempotency, and fail-closed on unknown/ambiguous input.
"""
from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from broker_intelligence import ingestion, parsers
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.gmail_source import parse_rfc822
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import (BrokerEmailIdentity, BrokerEvent, EvidenceBlob, Provenance,
                                        TransactionCategory, Withdrawal)
from broker_intelligence.parsers_tradersway import register_default_parsers
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "95%04d" % _n


def _acct(user, account_number, broker="TradersWay"):
    return TradingAccount.objects.create(user=user, name="A", broker_name=broker, account_number=account_number,
                                         is_demo=False, is_active=True)


# Redacted genuine-template email: HTML-only multipart/related; recipient name + confirmation URL/token removed.
def _raw(to_addr="nrfda1111@googlemail.com", frm="TradersWay <payments@tradersway.com>",
         subject="Please confirm your withdrawal request", src="54548", dst="647390",
         amount="24.12 USD", ref="4113188",
         ar="mx.google.com; dkim=pass header.i=@tradersway.com header.d=tradersway.com; "
            "spf=pass smtp.mailfrom=bounce.icpbounce.com; dmarc=fail (p=NONE) header.from=tradersway.com"):
    html = (f"<html><body><p>Dear [REDACTED],</p><p>{src} in amount of {amount} transferring to your trading "
            f"account {dst}. Your withdrawal request number is: {ref}.</p><p>If you want to confirm the withdrawal "
            f'request, click on the link below: <a href="https://[REDACTED]/c?token=[REDACTED]">confirm</a></p>'
            f"<p>Best regards, Finance Department Trader&#39;s Way</p></body></html>")
    return (
        f"From: {frm}\r\nTo: {to_addr}\r\nSubject: {subject}\r\n"
        f"Authentication-Results: {ar}\r\n"
        "Date: Sun, 12 Oct 2026 06:36:00 +0000\r\n"
        'Content-Type: multipart/related; boundary="BXYZ"\r\n\r\n'
        "--BXYZ\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html + "\r\n--BXYZ--\r\n"
    ).encode("utf-8")


def _mailmessage(raw: bytes) -> MailMessage:
    f = parse_rfc822(raw)
    return MailMessage(raw_bytes=raw, to_addresses=f["to_addresses"], from_address=f["from_address"],
                       subject=f["subject"], body=f["body"], received_at=f["received_at"],
                       auth_verdict=f["auth_verdict"])


@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com", BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS="mx.google.com")
class TradersWayRealTemplateTests(TestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="bi-tw-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()
        register_default_parsers()
        self.addCleanup(parsers.clear_registry)
        self.u = User.objects.create_user(username="tw%s" % _uniq(), email="tw@x.invalid", password="x")
        # The member's KNOWN GuvFX account (login 55442) — NOT the transaction's accounts (54548/647390).
        self.known = _acct(self.u, "55442")
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                           user=self.u, trading_account=self.known,
                                           status=BrokerEmailIdentity.Status.VERIFIED)

    def tearDown(self):
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)

    def _ingest(self, raw=None):
        return ingestion.ingest_message(_mailmessage(raw or _raw()), store=self.store, provenance=Provenance.REAL)

    # ---- parsing / classification / extraction (HTML-only body) ----
    def test_html_only_body_is_parsed_and_authenticated(self):
        ev = self._ingest()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.provenance, Provenance.REAL)
        self.assertEqual(ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")   # confirm-required, NOT completed

    def test_internal_transfer_classification_and_extraction(self):
        ev = self._ingest()
        self.assertEqual(ev.transaction_category, TransactionCategory.INTERNAL_TRANSFER)
        self.assertEqual(ev.amount, Decimal("24.12"))
        self.assertEqual(ev.currency, "USD")
        self.assertEqual(ev.broker_reference_id, "4113188")
        self.assertEqual(ev.source_account, "54548")
        self.assertEqual(ev.destination_account, "647390")

    def test_no_external_withdrawal_or_record(self):
        from broker_intelligence.correlation import run_correlation
        ev = self._ingest()
        self.assertNotEqual(ev.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)
        run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 0)   # internal transfer never becomes a withdrawal/stat

    # ---- transaction-account attribution (the core Phase-3 fix) ----
    def test_attribution_unresolved_when_transaction_accounts_do_not_match(self):
        # The member's only GuvFX account is 55442; the transaction is 54548 -> 647390. The event is owner-attributed
        # but the trading-account is UNRESOLVED (None) — NEVER bound to 55442 merely via the recipient email.
        ev = self._ingest()
        self.assertIsNone(ev.trading_account_id)
        self.assertEqual(ev.owner_id, self.u.id)

    def test_attribution_resolves_when_a_transaction_account_matches(self):
        matched = _acct(self.u, "54548")   # the member also holds the transaction's source account in GuvFX
        ev = self._ingest()
        self.assertEqual(ev.trading_account_id, matched.id)
        self.assertEqual(ev.owner_id, self.u.id)

    def test_ambiguous_transaction_match_is_unresolved(self):
        # Both transaction accounts are GuvFX accounts of the member -> ambiguous -> UNRESOLVED (never guess one).
        _acct(self.u, "54548")
        _acct(self.u, "647390")
        ev = self._ingest()
        self.assertIsNone(ev.trading_account_id)

    def test_cross_user_isolation(self):
        # Another member holds account 54548; the mail is owner-scoped to self.u, so the other member's account is
        # never matched (resolve_transaction_account is scoped to the resolved owner).
        other = User.objects.create_user(username="tw%s" % _uniq(), email="o@x.invalid", password="x")
        _acct(other, "54548")
        ev = self._ingest()
        self.assertIsNone(ev.trading_account_id)
        self.assertEqual(ev.owner_id, self.u.id)

    # ---- idempotency + fail-closed ----
    def test_idempotent_reingest(self):
        e1 = self._ingest()
        e2 = self._ingest()
        self.assertEqual(e1.id, e2.id)                   # same content -> same evidence -> same event
        self.assertEqual(BrokerEvent.objects.count(), 1)

    def test_unauthenticated_is_quarantined_no_event(self):
        raw = _raw(ar="mx.google.com; dkim=fail; spf=pass; dmarc=fail")   # not DKIM-aligned-pass
        ev = self._ingest(raw)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 1)   # evidence retained (quarantine)
        self.assertEqual(BrokerEvent.objects.count(), 0)

    def test_non_broker_sender_stores_nothing(self):
        raw = _raw(frm="mum@familymail.example",
                   ar="mx.google.com; dkim=pass header.d=familymail.example; dmarc=pass")
        ev = self._ingest(raw)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 0)   # non-broker REAL mail never retained
