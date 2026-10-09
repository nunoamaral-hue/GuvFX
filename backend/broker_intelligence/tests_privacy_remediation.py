"""R1/R2/R3 privacy + attribution remediation (DARK).

R1 — broker-sender allowlist + retention/acquisition gate (never acquire/retain non-broker personal mail; fail-closed).
R2 — gmail.com/googlemail.com (+ dots/+tag) canonical equivalence in identity attribution.
R3a — exact broker-domain matching (reject look-alike domains; no substring).
R3b — Authentication-Results sender-authenticity verdict + gate (the From header is never trusted on its own).
"""
from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from broker_intelligence import broker_senders as BS
from broker_intelligence import ingestion, parsers, resolver
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.gmail_source import parse_rfc822
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import BrokerEmailIdentity, BrokerEvent, EvidenceBlob, Provenance
from broker_intelligence.parsers_tradersway import TradersWayParser, register_default_parsers
from broker_intelligence.services import mint_alias_for_account
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "93%04d" % _n


def _acct(user=None):
    login = _uniq()
    u = user or User.objects.create_user(username="pr%s" % login, email="%s@x.invalid" % login, password="x")
    return TradingAccount.objects.create(user=u, name="A", broker_name="TradersWay", account_number=login,
                                         is_demo=False, is_active=True)


# ── R1 / R3a: allowlist + EXACT domain matching ──────────────────────────────────────────────────────────
class SenderAllowlistTests(SimpleTestCase):
    @override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com")
    def test_exact_domain_and_subdomain_only_never_substring(self):
        self.assertTrue(BS.is_allowlisted_broker_sender("payments@tradersway.com"))
        self.assertTrue(BS.is_allowlisted_broker_sender("TradersWay <no-reply@mail.tradersway.com>"))  # subdomain
        self.assertFalse(BS.is_allowlisted_broker_sender("payments@eviltradersway.com"))   # NOT a substring match
        self.assertFalse(BS.is_allowlisted_broker_sender("payments@tradersway.com.evil.com"))
        self.assertFalse(BS.is_allowlisted_broker_sender("not-an-email"))

    @override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="")
    def test_empty_allowlist_is_fail_closed(self):
        self.assertFalse(BS.is_allowlisted_broker_sender("payments@tradersway.com"))

    @override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com, otherbroker.example")
    def test_multiple_domains(self):
        self.assertTrue(BS.is_allowlisted_broker_sender("x@otherbroker.example"))
        self.assertTrue(BS.is_allowlisted_broker_sender("x@tradersway.com"))


class CanParseExactDomainTests(SimpleTestCase):
    def test_rejects_lookalike_and_substring_domains(self):
        p = TradersWayParser()
        self.assertTrue(p.can_parse(subject="withdrawal", body="", from_address="payments@tradersway.com"))
        self.assertTrue(p.can_parse(subject="withdrawal", body="", from_address="x@mail.tradersway.com"))
        self.assertFalse(p.can_parse(subject="withdrawal", body="", from_address="payments@eviltradersway.com"))
        self.assertFalse(p.can_parse(subject="withdrawal", body="", from_address="x@tradersway.com.evil.com"))


# ── R2: gmail/googlemail canonical equivalence ───────────────────────────────────────────────────────────
class CanonicalAddressTests(SimpleTestCase):
    def test_gmail_googlemail_dots_plus_equivalence(self):
        c = resolver.canonical_address
        self.assertEqual(c("n.rfda1111+tw@googlemail.com"), "nrfda1111@gmail.com")
        self.assertEqual(c("NRFDA1111@gmail.com"), "nrfda1111@gmail.com")
        self.assertEqual(c("TradersWay <nrfda1111@GoogleMail.com>"), "nrfda1111@gmail.com")

    def test_non_google_domains_unchanged(self):
        # Only Gmail ignores dots/plus — never munge another provider's local-part.
        self.assertEqual(resolver.canonical_address("foo.bar+x@outlook.com"), "foo.bar+x@outlook.com")


class IdentityCanonicalMatchTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="idu", email="idu@x.invalid", password="x")
        self.acct = _acct(self.u)
        # The broker registered the account under the @googlemail.com spelling; the connected Gmail surfaces @gmail.com.
        BrokerEmailIdentity.objects.create(
            email="nrfda1111@googlemail.com", broker_name="TradersWay", user=self.u,
            trading_account=self.acct, status=BrokerEmailIdentity.Status.VERIFIED)

    def test_gmail_header_matches_googlemail_identity(self):
        _alias, account = resolver.resolve(("nrfda1111@gmail.com",), owner_user=self.u)
        self.assertEqual(account, self.acct)

    def test_dotted_plus_form_matches(self):
        _alias, account = resolver.resolve(("n.rfda1111+tw@gmail.com",), owner_user=self.u)
        self.assertEqual(account, self.acct)

    def test_different_mailbox_does_not_match(self):   # negative control
        _alias, account = resolver.resolve(("someoneelse@gmail.com",), owner_user=self.u)
        self.assertIsNone(account)

    def test_other_user_cannot_resolve(self):          # owner-scope firewall preserved under canonicalisation
        other = User.objects.create_user(username="idv", email="idv@x.invalid", password="x")
        _alias, account = resolver.resolve(("nrfda1111@gmail.com",), owner_user=other)
        self.assertIsNone(account)


# ── R3b: Authentication-Results verdict parsing ──────────────────────────────────────────────────────────
class AuthVerdictParseTests(SimpleTestCase):
    def _verdict(self, ar_line: str):
        ar = (f"Authentication-Results: {ar_line}\r\n").encode() if ar_line else b""
        raw = b"From: x@tradersway.com\r\n" + ar + b"Subject: s\r\n\r\nbody"
        return parse_rfc822(raw)["auth_verdict"]

    def test_dmarc_pass(self):
        self.assertEqual(self._verdict("mx.google.com; spf=pass; dkim=pass; dmarc=pass"), "pass")

    def test_spf_and_dkim_pass_without_dmarc(self):
        self.assertEqual(self._verdict("mx.google.com; spf=pass; dkim=pass"), "pass")

    def test_present_but_failing_is_fail(self):
        self.assertEqual(self._verdict("mx.google.com; spf=fail; dkim=fail; dmarc=fail"), "fail")

    def test_absent_is_none(self):
        self.assertIsNone(self._verdict(""))


# ── R1 + R3b: ingestion retention + authenticity gates (REAL mail only) ──────────────────────────────────
@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com")
class IngestionGateTests(TestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="bi-pr-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()
        register_default_parsers()
        self.addCleanup(parsers.clear_registry)
        self.u = User.objects.create_user(username="rg", email="rg@x.invalid", password="x")
        self.acct = _acct(self.u)
        self.alias = mint_alias_for_account(self.acct)

    def tearDown(self):
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)

    def _msg(self, frm, auth_verdict):
        return MailMessage(
            raw_bytes=("%s|%s" % (frm, auth_verdict)).encode(), to_addresses=(self.alias.address(),),
            from_address=frm, subject="Your withdrawal request has been received",
            body="Amount: 200.00 USD\nReference: W-1", received_at=timezone.now(), auth_verdict=auth_verdict)

    def test_real_non_broker_sender_stores_nothing(self):
        ev = ingestion.ingest_message(self._msg("mum@familymail.example", "pass"),
                                      store=self.store, provenance=Provenance.REAL)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 0)          # personal mail is NEVER retained

    def test_real_broker_unauthenticated_quarantines_without_event(self):
        ev = ingestion.ingest_message(self._msg("payments@tradersway.com", "fail"),
                                      store=self.store, provenance=Provenance.REAL)
        self.assertIsNone(ev)                                      # From not trusted on its own -> no event
        self.assertEqual(EvidenceBlob.objects.count(), 1)         # evidence retained (quarantine)
        self.assertEqual(BrokerEvent.objects.count(), 0)

    def test_real_broker_missing_auth_quarantines_without_event(self):
        ev = ingestion.ingest_message(self._msg("payments@tradersway.com", None),
                                      store=self.store, provenance=Provenance.REAL)
        self.assertIsNone(ev)
        self.assertEqual(BrokerEvent.objects.count(), 0)

    def test_real_broker_authenticated_creates_event(self):
        ev = ingestion.ingest_message(self._msg("payments@tradersway.com", "pass"),
                                      store=self.store, provenance=Provenance.REAL)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.trading_account_id, self.acct.id)
        self.assertEqual(ev.provenance, Provenance.REAL)
        self.assertEqual(ev.event_type, "WITHDRAWAL_REQUESTED")

    @override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="")
    def test_empty_allowlist_fail_closed_stores_nothing(self):
        ev = ingestion.ingest_message(self._msg("payments@tradersway.com", "pass"),
                                      store=self.store, provenance=Provenance.REAL)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 0)

    def test_synthetic_fixture_is_exempt_from_real_gates(self):
        # SYNTHETIC fixtures are controlled test data, never live personal mail -> the REAL-only gates do not apply, so
        # the raw is still stored (the parser just declines a non-broker sender, leaving evidence retained, no event).
        ev = ingestion.ingest_message(self._msg("mum@familymail.example", None),
                                      store=self.store, provenance=Provenance.SYNTHETIC)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 1)
