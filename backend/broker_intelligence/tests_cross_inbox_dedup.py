"""MULTI_ACCOUNT_PUBLIC_METRICS §D — cross-inbox withdrawal de-duplication (DARK).

Proves that the SAME logical broker email (same RFC822 Message-ID) observed through TWO connected inboxes — a
DIFFERENT raw blob each time, so content-hashing cannot collapse them — does NOT create a second BrokerEvent when the
feature is armed, and is byte-identical (two events, table untouched) when OFF. Also proves the parser captures the
Message-ID.
"""
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import ingestion, parsers
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.gmail_source import parse_rfc822
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import (BrokerEvent, BrokerMessageObservation, ConnectedMailbox, Provenance)
from broker_intelligence.services import mint_alias_for_account
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "91%04d" % _n


class _WithdrawalParser:
    name = "fake"
    version = "v1"

    def can_parse(self, *, subject, body, from_address):
        return "withdrawal" in (subject or "").lower()

    def parse(self, *, subject, body, from_address):
        return parsers.ParsedBrokerEvent(
            event_type="WITHDRAWAL_REQUESTED", broker="TradersWay",
            amount=Decimal("100.50"), currency="USD", broker_reference_id="ABC123", confidence=Decimal("0.900"))


class ParseMessageId(TestCase):
    def test_parse_rfc822_captures_and_normalises_message_id(self):
        raw = b"Message-ID: <abc123@mail.tradersway.com>\r\nSubject: x\r\nFrom: a@b.c\r\nTo: c@d.e\r\n\r\nbody"
        self.assertEqual(parse_rfc822(raw)["rfc822_message_id"], "abc123@mail.tradersway.com")  # <> stripped

    def test_missing_message_id_is_empty(self):
        raw = b"Subject: x\r\nFrom: a@b.c\r\nTo: c@d.e\r\n\r\nbody"
        self.assertEqual(parse_rfc822(raw)["rfc822_message_id"], "")


class _Base(TestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="bi-dedup-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()
        parsers.register_parser(_WithdrawalParser())
        login = _uniq()
        self.u = User.objects.create_user(username="d%s" % login, email="%s@x.invalid" % login, password="x")
        self.acct = TradingAccount.objects.create(user=self.u, name="A", broker_name="TradersWay",
                                                  account_number=login, is_demo=True, is_active=True)
        self.alias = mint_alias_for_account(self.acct)           # To this alias => resolves to self.u (non-null owner)
        self.mb = ConnectedMailbox.objects.create(user=self.u, provider=ConnectedMailbox.Provider.GMAIL,
                                                  provider_mailbox_id="m%s@gmail.com" % login,
                                                  primary_email="m%s@gmail.com" % login,
                                                  status=ConnectedMailbox.Status.CONNECTED)

    def tearDown(self):
        parsers.clear_registry()
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)

    def _msg(self, mid, raw):
        return MailMessage(raw_bytes=raw, to_addresses=(self.alias.address(),),
                           from_address="noreply@tradersway.invalid", subject="Withdrawal request received",
                           body="Ref ABC123 100.50 USD", received_at=timezone.now(),
                           provider_message_id="p-" + raw.decode()[-3:], rfc822_message_id=mid)

    def _ingest(self, msg):
        return ingestion.ingest_message(msg, store=self.store, provenance=Provenance.SYNTHETIC,
                                        owner_user=self.u, connected_mailbox=self.mb)


class CrossInboxDedup(_Base):
    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_same_message_two_inboxes_dedups_to_one_event(self):
        # Same Message-ID, DIFFERENT raw bytes (different sha256) — the cross-inbox case content-hashing can't catch.
        e1 = self._ingest(self._msg("MID-1@tw", b"inbox-1-raw-headers-AAAA"))
        e2 = self._ingest(self._msg("MID-1@tw", b"inbox-2-DIFFERENT-raw-BBBB"))
        self.assertIsNotNone(e1)
        self.assertEqual(e2.pk, e1.pk)                       # 2nd inbox copy returns the SAME event (suppressed)
        self.assertEqual(BrokerEvent.objects.count(), 1)     # NEVER a second event/withdrawal
        obs = BrokerMessageObservation.objects.get()
        self.assertEqual(obs.owner_id, self.u.id)
        self.assertEqual(obs.event_id, e1.pk)
        self.assertEqual(obs.duplicate_count, 1)             # the suppressed copy is counted, never silently dropped

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_distinct_messages_are_not_deduped(self):
        e1 = self._ingest(self._msg("MID-A@tw", b"raw-A"))
        e2 = self._ingest(self._msg("MID-B@tw", b"raw-B"))   # different Message-ID => distinct logical emails
        self.assertNotEqual(e1.pk, e2.pk)
        self.assertEqual(BrokerEvent.objects.count(), 2)
        self.assertEqual(BrokerMessageObservation.objects.count(), 2)

    def test_dark_default_does_not_dedup_and_never_touches_the_table(self):
        # Flag OFF (default): byte-identical to today — two inbox copies => two events — and the new table is NEVER
        # touched, so an unmigrated production DB (migration 0013 unapplied) is unaffected.
        e1 = self._ingest(self._msg("MID-1@tw", b"inbox-1-raw-AAAA"))
        e2 = self._ingest(self._msg("MID-1@tw", b"inbox-2-raw-BBBB"))
        self.assertNotEqual(e1.pk, e2.pk)
        self.assertEqual(BrokerEvent.objects.count(), 2)
        self.assertEqual(BrokerMessageObservation.objects.count(), 0)

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_message_without_id_falls_back_to_content_hash_only(self):
        # No Message-ID => no cross-inbox key => existing content-hash behaviour (distinct blobs => distinct events).
        e1 = self._ingest(self._msg("", b"no-mid-raw-AAAA"))
        e2 = self._ingest(self._msg("", b"no-mid-raw-BBBB"))
        self.assertNotEqual(e1.pk, e2.pk)
        self.assertEqual(BrokerMessageObservation.objects.count(), 0)   # blank id is never uniqueness-bearing

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_no_event_first_copy_leaves_no_tombstone(self):
        # DEFECT-2 regression: a first copy that produces NO event (unparseable) must NOT suppress a later genuine copy
        # of the same Message-ID. Because the observation is written ONLY after a successful event, there is no
        # NULL-event tombstone, so the second (parseable) copy is processed normally — the withdrawal is NOT lost.
        unparseable = MailMessage(raw_bytes=b"first-copy-unparseable", to_addresses=(self.alias.address(),),
                                  from_address="noreply@tradersway.invalid", subject="Account statement",  # no parser
                                  body="nothing to parse", received_at=timezone.now(),
                                  provider_message_id="p-u1", rfc822_message_id="MID-SAME@tw")
        e_first = self._ingest(unparseable)
        self.assertIsNone(e_first)                                     # unparseable -> no event
        self.assertEqual(BrokerMessageObservation.objects.count(), 0)  # and NO tombstone row
        e_second = self._ingest(self._msg("MID-SAME@tw", b"second-copy-parseable"))
        self.assertIsNotNone(e_second)                                 # the genuine copy is NOT suppressed
        self.assertEqual(BrokerEvent.objects.count(), 1)
        self.assertEqual(BrokerMessageObservation.objects.get().event_id, e_second.pk)

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_unresolved_owner_skips_dedup_no_null_owner_rows(self):
        # DEFECT-1 regression: when no owner can be keyed (owner_user None AND the recipient does not resolve to a
        # member), the dedup is SKIPPED rather than writing ambiguous NULL-owner rows — no NULL-owner race, and
        # behaviour falls back to content-hash (distinct blobs -> distinct events).
        def _ingest_no_owner(raw):
            m = MailMessage(raw_bytes=raw, to_addresses=("stranger@nowhere.invalid",),
                            from_address="noreply@tradersway.invalid", subject="Withdrawal request received",
                            body="Ref ABC123 100.50 USD", received_at=timezone.now(),
                            provider_message_id="p-" + raw.decode()[-3:], rfc822_message_id="MID-NOBODY@tw")
            return ingestion.ingest_message(m, store=self.store, provenance=Provenance.SYNTHETIC)  # no owner_user
        e1 = _ingest_no_owner(b"raw-AAAA")
        e2 = _ingest_no_owner(b"raw-BBBB")
        self.assertNotEqual(e1.pk, e2.pk)                              # no dedup without an owner key
        self.assertEqual(BrokerMessageObservation.objects.count(), 0)  # NEVER a NULL-owner observation row
