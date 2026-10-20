"""WV1 — regression for the GENUINE TradersWay external-withdrawal pair (captured 2026-10-19), REDACTED.

The fixtures reproduce the real emails' STRUCTURE — HTML-only multipart/related body, Gmail Authentication-Results
with an aligned DKIM pass, and the external-payout grammar — with the recipient name, confirmation URL, token,
blockchain address and the real account/reference values replaced by placeholders. Two messages, 33m01s apart in
the genuine capture, linked by ONE authenticated broker reference:

  * REQUEST     — "Received the request number #<N> to withdraw … Payment method: Ethereum" → EXTERNAL_WITHDRAWAL,
                  WITHDRAWAL_CONFIRMATION_REQUIRED, reference #N, amount/currency, source account UNRESOLVED (the
                  genuine email states no "trading account #"), OWNER-attributed.
  * COMPLETION  — "Payment request #<N> … status changed from Confirmed to Completed" → WITHDRAWAL_COMPLETED but
                  transaction_category UNKNOWN (no rail / no direction wording in its own text); externality is
                  established ONLY by WP5 correlation to the request sharing reference #N — never re-derived here.

It certifies (against the authenticated genuine shape): aligned-DKIM auth, the ONE certified parser→EXTERNAL path
(the ``Payment method:`` rail label, gated by a withdrawal-direction lifecycle), the neutral-completion contract,
request↔completion reference linkage, owner-attributed / account-UNRESOLVED attribution, no URL/token/address leak,
idempotency, and fail-closed on an unauthenticated sender. The values here are SYNTHETIC placeholders; the genuine
bytes live only in the immutable production evidence store (never in Git/CI/logs — WV1 §2).
"""
from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from broker_intelligence import ingestion, parsers, redaction
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.gmail_source import parse_rfc822
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import (BrokerEmailIdentity, BrokerEvent, EvidenceBlob, Provenance,
                                        TransactionCategory)
from broker_intelligence.parsers_tradersway import register_default_parsers
from trading.models import TradingAccount

User = get_user_model()
_n = 0

_MEMBER_EMAIL = "pilot-member@example.invalid"   # the member's connected mailbox (placeholder, not the real address)
_REF = "7654321"                                 # placeholder 7-digit broker reference (genuine value is redacted)

_AR = ("mx.google.com; dkim=pass header.i=@tradersway.com header.d=tradersway.com; "
       "spf=pass smtp.mailfrom=bounce.icpbounce.com; dmarc=fail (p=NONE) header.from=tradersway.com")


def _uniq():
    global _n
    _n += 1
    return "96%04d" % _n


def _acct(user, account_number, broker="TradersWay"):
    return TradingAccount.objects.create(user=user, name="A", broker_name=broker, account_number=account_number,
                                         is_demo=False, is_active=True)


# Redacted genuine-REQUEST email: HTML multipart/related; name/URL/token/address removed; NO "trading account #"
# (the genuine request states none → source account UNRESOLVED). Carries the certified rail label + direction wording.
def _raw_request(to_addr=_MEMBER_EMAIL, frm="TradersWay <payments@tradersway.com>",
                 subject="Please confirm your withdrawal request", ref=_REF, amount="250.00 USD", ar=_AR):
    html = (f"<html><body><p>Dear [REDACTED],</p>"
            f"<p>Received the request number #{ref} to withdraw funds from your account.</p>"
            f"<p>Amount: {amount}</p><p>Payment method: Ethereum</p>"
            f'<p>If you want to confirm the withdrawal request, click the link below: '
            f'<a href="https://[REDACTED]/c?token=[REDACTED]">confirm</a></p>'
            f"<p>Best regards, Finance Department Trader&#39;s Way</p></body></html>")
    return (
        f"From: {frm}\r\nTo: {to_addr}\r\nSubject: {subject}\r\n"
        f"Authentication-Results: {ar}\r\n"
        "Date: Mon, 19 Oct 2026 07:55:04 +0000\r\n"
        'Content-Type: multipart/related; boundary="BXYZ"\r\n\r\n'
        "--BXYZ\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html + "\r\n--BXYZ--\r\n"
    ).encode("utf-8")


# Redacted genuine-COMPLETION email: fewer fields — no rail, no "withdraw"/"withdrawal" token. Shares reference #N.
def _raw_completion(to_addr=_MEMBER_EMAIL, frm="TradersWay <payments@tradersway.com>",
                    subject="Payment request update", ref=_REF, ar=_AR):
    html = (f"<html><body><p>Dear [REDACTED],</p>"
            f"<p>Your Payment request #{ref} status changed from Confirmed to Completed.</p>"
            f"<p>Thank you for using TradersWay.</p></body></html>")
    return (
        f"From: {frm}\r\nTo: {to_addr}\r\nSubject: {subject}\r\n"
        f"Authentication-Results: {ar}\r\n"
        "Date: Mon, 19 Oct 2026 08:28:05 +0000\r\n"
        'Content-Type: multipart/related; boundary="BXYZ"\r\n\r\n'
        "--BXYZ\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html + "\r\n--BXYZ--\r\n"
    ).encode("utf-8")


def _mailmessage(raw: bytes) -> MailMessage:
    f = parse_rfc822(raw)
    return MailMessage(raw_bytes=raw, to_addresses=f["to_addresses"], from_address=f["from_address"],
                       subject=f["subject"], body=f["body"], received_at=f["received_at"],
                       auth_verdict=f["auth_verdict"])


@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com",
                   BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS="mx.google.com")
class TradersWayExternalTemplateTests(TestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="bi-twx-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()
        register_default_parsers()
        self.addCleanup(parsers.clear_registry)
        self.u = User.objects.create_user(username="twx%s" % _uniq(), email="twx@x.invalid", password="x")
        # The member's KNOWN GuvFX account — deliberately NOT the (unregistered) TradersWay account the withdrawal
        # was made from, so the genuine case stays OWNER-attributed / account-UNRESOLVED.
        self.known = _acct(self.u, "55442")
        BrokerEmailIdentity.objects.create(email=_MEMBER_EMAIL, broker_name="TradersWay", user=self.u,
                                           trading_account=self.known, status=BrokerEmailIdentity.Status.VERIFIED)

    def tearDown(self):
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)

    def _ingest(self, raw):
        return ingestion.ingest_message(_mailmessage(raw), store=self.store, provenance=Provenance.REAL)

    # ---- REQUEST: the certified external path ----
    def test_request_authenticated_and_external(self):
        ev = self._ingest(_raw_request())
        self.assertIsNotNone(ev)
        self.assertEqual(ev.provenance, Provenance.REAL)
        self.assertEqual(ev.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")
        self.assertEqual(ev.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)
        self.assertEqual(ev.broker_reference_id, _REF)
        self.assertEqual(ev.amount, Decimal("250.00"))
        self.assertEqual(ev.currency, "USD")

    def test_request_is_owner_attributed_account_unresolved(self):
        ev = self._ingest(_raw_request())
        self.assertEqual(ev.owner_id, self.u.id)                 # attributed to the mailbox owner
        self.assertIsNone(ev.trading_account_id)                 # the genuine request states no account -> UNRESOLVED
        self.assertEqual(ev.source_account, "")                  # never invent a source account
        self.assertEqual(ev.destination_account, "")

    # ---- COMPLETION: neutral, externality only via correlation ----
    def test_completion_authenticated_completed_but_unknown_category(self):
        ev = self._ingest(_raw_completion())
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(ev.transaction_category, TransactionCategory.UNKNOWN)   # never self-claims external
        self.assertEqual(ev.broker_reference_id, _REF)
        self.assertEqual(ev.owner_id, self.u.id)
        self.assertIsNone(ev.trading_account_id)

    def test_request_and_completion_linked_by_broker_reference(self):
        req = self._ingest(_raw_request())
        done = self._ingest(_raw_completion())
        self.assertEqual(req.broker_reference_id, done.broker_reference_id)       # the WP5 correlation key
        self.assertEqual(req.broker_reference_id, _REF)
        self.assertNotEqual(req.id, done.id)                                      # two distinct authentic events

    # ---- privacy: no URL / token / address in any emitted field ----
    def test_no_sensitive_material_in_emitted_fields(self):
        for raw in (_raw_request(), _raw_completion()):
            ev = self._ingest(raw)
            for field in (ev.broker_reference_id, ev.currency, ev.broker, ev.event_type,
                          ev.transaction_category, ev.source_account, ev.destination_account):
                self.assertFalse(redaction.contains_sensitive(field or ""),
                                 f"emitted field leaked sensitive material: {field!r}")

    # ---- idempotency + fail-closed ----
    def test_idempotent_reingest(self):
        e1 = self._ingest(_raw_request())
        e2 = self._ingest(_raw_request())
        self.assertEqual(e1.id, e2.id)
        self.assertEqual(BrokerEvent.objects.filter(event_type="WITHDRAWAL_CONFIRMATION_REQUIRED").count(), 1)

    def test_unauthenticated_request_is_quarantined_no_event(self):
        raw = _raw_request(ar="mx.google.com; dkim=fail; spf=pass; dmarc=fail")
        ev = self._ingest(raw)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 1)        # evidence retained (quarantine)
        self.assertEqual(BrokerEvent.objects.count(), 0)

    def test_spoofed_lookalike_sender_not_parsed(self):
        raw = _raw_request(frm="TradersWay <payments@eviltradersway.com>",
                           ar="mx.google.com; dkim=pass header.d=eviltradersway.com; dmarc=pass")
        ev = self._ingest(raw)
        self.assertIsNone(ev)                                    # exact registrable-domain match, not a substring
        self.assertEqual(BrokerEvent.objects.count(), 0)
