"""MULTI_ACCOUNT_PUBLIC_METRICS §D — per-delivery de-dup + owner-scoped idempotency + cross-inbox collapse (DARK).

Corrected per the PD review of #511. Three layers, de-duplicated independently and proven here:
  * DELIVERY — a genuine RE-FETCH of the SAME message from the SAME inbox (``(connected_mailbox,
    provider_message_id)``) reuses the event; TWO different inboxes are TWO preserved deliveries (never suppressed).
  * BROKER-EVENT — owner-scoped content-addressed idempotency: two DIFFERENT owners who receive byte-identical bytes
    share ONE EvidenceBlob yet each keep their OWN event (no cross-owner leak, no dropped delivery). UNCONDITIONAL.
  * WITHDRAWAL — the same logical ref-less email delivered to two inboxes collapses to ONE Withdrawal at the
    correlation layer (RFC822 Message-ID used only as a non-authoritative hint), so duplicate deliveries do not
    inflate statistics.
The RFC822 Message-ID is NEVER a suppression key. Also proves the parser still captures the Message-ID (metadata).
"""
import shutil
import tempfile
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import ingestion, parsers
from broker_intelligence.correlation import OUT_CROSS_INBOX, run_correlation
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.gmail_source import parse_rfc822
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import (BrokerEvent, BrokerMessageObservation, ConnectedMailbox, EvidenceBlob,
                                         Provenance, TransactionCategory, Withdrawal)
from broker_intelligence.services import mint_alias_for_account
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "91%04d" % _n


class _ReflessWithdrawalParser:
    """A ref-LESS EXTERNAL_WITHDRAWAL opening parser — like the certified IS6 template, which carries no reference id
    (so the withdrawal-layer (account, reference) collapse does NOT apply and the cross-inbox collapse is load-bearing)."""
    name = "fake-refless"
    version = "v1"

    def can_parse(self, *, subject, body, from_address):
        return "withdrawal" in (subject or "").lower()

    def parse(self, *, subject, body, from_address):
        return parsers.ParsedBrokerEvent(
            event_type="WITHDRAWAL_REQUESTED", broker="TradersWay",
            transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL,
            amount=Decimal("100.50"), currency="USD", broker_reference_id="",   # REF-LESS (like IS6)
            occurred_at=timezone.now(), confidence=Decimal("0.900"))


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
        parsers.register_parser(_ReflessWithdrawalParser())
        login = _uniq()
        self.u = User.objects.create_user(username="d%s" % login, email="%s@x.invalid" % login, password="x")
        self.acct = TradingAccount.objects.create(user=self.u, name="A", broker_name="TradersWay",
                                                  account_number=login, is_demo=True, is_active=True)
        self.alias = mint_alias_for_account(self.acct)           # To this alias => resolves to self.u (non-null owner)
        self.mb = ConnectedMailbox.objects.create(user=self.u, provider=ConnectedMailbox.Provider.GMAIL,
                                                  provider_mailbox_id="m%s@gmail.com" % login,
                                                  primary_email="m%s@gmail.com" % login,
                                                  status=ConnectedMailbox.Status.CONNECTED)
        # A SECOND connected inbox for the SAME owner — the real "two inboxes" case (two connected_mailboxes).
        self.mb2 = ConnectedMailbox.objects.create(user=self.u, provider=ConnectedMailbox.Provider.GMAIL,
                                                   provider_mailbox_id="w%s@gmail.com" % login,
                                                   primary_email="w%s@gmail.com" % login,
                                                   status=ConnectedMailbox.Status.CONNECTED)

    def tearDown(self):
        parsers.clear_registry()
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)

    def _msg(self, *, mid="", raw=b"x", pmid="pm-default", to=None, subject="Withdrawal request received"):
        return MailMessage(raw_bytes=raw, to_addresses=(to or self.alias.address(),),
                           from_address="noreply@tradersway.invalid", subject=subject,
                           body="withdraw 100.50 USD", received_at=timezone.now(),
                           provider_message_id=pmid, rfc822_message_id=mid)

    def _ingest(self, msg, *, mailbox, owner_user):
        return ingestion.ingest_message(msg, store=self.store, provenance=Provenance.SYNTHETIC,
                                        owner_user=owner_user, connected_mailbox=mailbox)


class OwnerScopedIdempotency(_Base):
    """UNCONDITIONAL (no flag): the cross-owner leak fix + owner-scoped content-addressed idempotency."""

    def test_shared_raw_bytes_two_owners_each_keeps_own_event(self):
        # The core leak fix: two DIFFERENT owners receive BYTE-IDENTICAL bytes -> ONE shared content-addressed blob,
        # but each keeps their OWN owner-attributed event. Neither is handed the other's (security.md), neither
        # authentic delivery is dropped (data.md).
        login = _uniq()
        other = User.objects.create_user(username="o%s" % login, email="%s@x.invalid" % login, password="x")
        mb_other = ConnectedMailbox.objects.create(user=other, provider=ConnectedMailbox.Provider.GMAIL,
                                                   provider_mailbox_id="o%s@gmail.com" % login,
                                                   primary_email="o%s@gmail.com" % login,
                                                   status=ConnectedMailbox.Status.CONNECTED)
        raw = b"identical-broker-bytes-no-per-recipient-variance"
        eA = self._ingest(self._msg(raw=raw, pmid="pA", to="stranger@x.invalid"), mailbox=self.mb, owner_user=self.u)
        eB = self._ingest(self._msg(raw=raw, pmid="pB", to="stranger@x.invalid"), mailbox=mb_other, owner_user=other)
        self.assertIsNotNone(eA)
        self.assertIsNotNone(eB)
        self.assertNotEqual(eA.pk, eB.pk)                         # two distinct owner-scoped events
        self.assertEqual(eA.owner_id, self.u.id)
        self.assertEqual(eB.owner_id, other.id)
        self.assertEqual(EvidenceBlob.objects.count(), 1)         # ONE shared immutable blob
        self.assertEqual(BrokerEvent.objects.count(), 2)

    def test_same_owner_same_blob_is_idempotent(self):
        raw = b"identical-bytes-same-owner"
        e1 = self._ingest(self._msg(raw=raw, pmid="p1", to="stranger@x.invalid"), mailbox=self.mb, owner_user=self.u)
        e2 = self._ingest(self._msg(raw=raw, pmid="p2", to="stranger@x.invalid"), mailbox=self.mb, owner_user=self.u)
        self.assertEqual(e2.pk, e1.pk)                            # same (owner, blob) -> the SAME event
        self.assertEqual(BrokerEvent.objects.count(), 1)

    def test_live_event_owner_is_mailbox_owner_even_when_account_unresolved(self):
        # owner is the TRUSTED mailbox owner (owner_key), not the fail-closed recipient resolution — so an event is
        # owner-attributed even when the transaction trading-account cannot be resolved.
        e = self._ingest(self._msg(raw=b"acct-unresolved", to="stranger@x.invalid"), mailbox=self.mb, owner_user=self.u)
        self.assertIsNotNone(e)
        self.assertEqual(e.owner_id, self.u.id)
        self.assertIsNone(e.trading_account_id)

    def test_second_event_same_owner_same_blob_raises(self):
        e1 = self._ingest(self._msg(raw=b"blob-for-constraint"), mailbox=self.mb, owner_user=self.u)
        with self.assertRaises(IntegrityError):               # uniq_brokerevent_evidence_owner fires
            with transaction.atomic():
                BrokerEvent.objects.create(
                    owner=self.u, evidence=e1.evidence, evidence_hash=e1.evidence.sha256,
                    event_type="WITHDRAWAL_REQUESTED", received_at=timezone.now(),
                    transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL, provenance=Provenance.SYNTHETIC)

    def test_user_deletion_with_shared_blob_events_succeeds(self):
        # The owner-null hard unique is deliberately OMITTED: once two owners' events share one blob, deleting BOTH
        # users SET_NULLs their owner and must NOT deadlock (no per-blob owner-null collision). Proves the omission.
        la, lb = _uniq(), _uniq()
        uA = User.objects.create_user(username="ua%s" % la, email="%s@x.invalid" % la, password="x")
        uB = User.objects.create_user(username="ub%s" % lb, email="%s@x.invalid" % lb, password="x")
        raw = b"shared-identical-bytes-for-deletion"
        eA = self._ingest(self._msg(raw=raw, to="stranger@x.invalid"), mailbox=None, owner_user=uA)
        eB = self._ingest(self._msg(raw=raw, to="stranger@x.invalid"), mailbox=None, owner_user=uB)
        self.assertNotEqual(eA.pk, eB.pk)
        self.assertEqual(EvidenceBlob.objects.count(), 1)
        uA.delete()
        uB.delete()                                              # must not raise
        self.assertEqual(BrokerEvent.objects.count(), 2)
        self.assertTrue(all(e.owner_id is None for e in BrokerEvent.objects.all()))


class DeliveryDedup(_Base):
    """Flag ON: delivery de-dup keyed on the TRUSTED (connected_mailbox, provider_message_id)."""

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_same_inbox_refetch_same_pmid_reuses_event(self):
        # Same inbox + same provider_message_id, DIFFERENT raw bytes (different sha256, so blob idempotency misses) ->
        # a genuine re-fetch => ONE event, re-fetch counted.
        e1 = self._ingest(self._msg(mid="MID-1@tw", raw=b"fetch-AAAA", pmid="pm-1"), mailbox=self.mb, owner_user=self.u)
        e2 = self._ingest(self._msg(mid="MID-1@tw", raw=b"fetch-DIFFERENT-BBBB", pmid="pm-1"),
                          mailbox=self.mb, owner_user=self.u)
        self.assertEqual(e2.pk, e1.pk)
        self.assertEqual(BrokerEvent.objects.count(), 1)
        obs = BrokerMessageObservation.objects.get()
        self.assertEqual(obs.connected_mailbox_id, self.mb.id)
        self.assertEqual(obs.provider_message_id, "pm-1")
        self.assertEqual(obs.event_id, e1.pk)
        self.assertEqual(obs.duplicate_count, 1)

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_two_inboxes_two_events_preserved(self):
        # The KEY INVERSION vs the superseded Message-ID suppression: the SAME logical email (same Message-ID) through
        # TWO different inboxes is TWO different connected_mailboxes => TWO preserved owner-scoped events (collapsed
        # only at the withdrawal layer), NEVER suppressed here.
        e1 = self._ingest(self._msg(mid="MID-1@tw", raw=b"inbox1-AAAA", pmid="pm-a"), mailbox=self.mb, owner_user=self.u)
        e2 = self._ingest(self._msg(mid="MID-1@tw", raw=b"inbox2-BBBB", pmid="pm-b"), mailbox=self.mb2, owner_user=self.u)
        self.assertNotEqual(e1.pk, e2.pk)
        self.assertEqual(BrokerEvent.objects.count(), 2)
        self.assertEqual(BrokerMessageObservation.objects.count(), 2)

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_distinct_deliveries_not_deduped(self):
        e1 = self._ingest(self._msg(mid="MID-A@tw", raw=b"d-AAAA", pmid="pm-1"), mailbox=self.mb, owner_user=self.u)
        e2 = self._ingest(self._msg(mid="MID-B@tw", raw=b"d-BBBB", pmid="pm-2"), mailbox=self.mb, owner_user=self.u)
        self.assertNotEqual(e1.pk, e2.pk)
        self.assertEqual(BrokerMessageObservation.objects.count(), 2)

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_empty_provider_message_id_skips_delivery_write(self):
        # No trusted delivery identity => NO suppression row (mirrors 'no owner => skip'); never drop evidence.
        e1 = self._ingest(self._msg(mid="MID-1@tw", raw=b"empty-pmid-AAAA", pmid=""), mailbox=self.mb, owner_user=self.u)
        self.assertIsNotNone(e1)
        self.assertEqual(BrokerMessageObservation.objects.count(), 0)

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_crash_before_event_no_suppressing_delivery_row(self):
        # A first copy that produces NO event (unparseable) must NOT write a delivery row (no tombstone), so a later
        # parseable re-fetch of the SAME (mailbox, pmid) is processed normally — the withdrawal is NOT lost.
        unparseable = self._msg(subject="Account statement", raw=b"unparseable-1", pmid="pm-x")   # no parser matches
        e_first = self._ingest(unparseable, mailbox=self.mb, owner_user=self.u)
        self.assertIsNone(e_first)
        self.assertEqual(BrokerMessageObservation.objects.count(), 0)
        e_second = self._ingest(self._msg(subject="Withdrawal request received", raw=b"parseable-2", pmid="pm-x"),
                                mailbox=self.mb, owner_user=self.u)
        self.assertIsNotNone(e_second)
        self.assertEqual(BrokerEvent.objects.count(), 1)
        self.assertEqual(BrokerMessageObservation.objects.get().event_id, e_second.pk)

    def test_dark_default_does_not_touch_the_table(self):
        # Flag OFF (default): delivery de-dup skipped; different bytes => different blobs => two events; the delivery
        # table is NEVER touched, so an unmigrated production DB is unaffected.
        e1 = self._ingest(self._msg(mid="MID-1@tw", raw=b"off-AAAA", pmid="pm-1"), mailbox=self.mb, owner_user=self.u)
        e2 = self._ingest(self._msg(mid="MID-1@tw", raw=b"off-BBBB", pmid="pm-1"), mailbox=self.mb, owner_user=self.u)
        self.assertNotEqual(e1.pk, e2.pk)
        self.assertEqual(BrokerEvent.objects.count(), 2)
        self.assertEqual(BrokerMessageObservation.objects.count(), 0)


class CrossInboxWithdrawalCollapse(_Base):
    """Flag ON: two preserved ref-less deliveries of ONE logical email collapse to ONE Withdrawal (no inflation)."""

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_refless_two_inbox_copies_collapse_to_one_withdrawal(self):
        e1 = self._ingest(self._msg(mid="MID-SAME@tw", raw=b"inbox1-AAAA", pmid="pm-a"),
                          mailbox=self.mb, owner_user=self.u)
        e2 = self._ingest(self._msg(mid="MID-SAME@tw", raw=b"inbox2-BBBB", pmid="pm-b"),
                          mailbox=self.mb2, owner_user=self.u)
        self.assertNotEqual(e1.pk, e2.pk)                         # two preserved events
        self.assertEqual(BrokerEvent.objects.count(), 2)
        summary = run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)           # duplicate deliveries collapse to ONE withdrawal
        self.assertEqual(summary.get(OUT_CROSS_INBOX), 1)

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_refless_two_genuine_distinct_requests_stay_two(self):
        # Distinct Message-IDs => genuinely distinct withdrawals; the hint never falsely merges them.
        self._ingest(self._msg(mid="MID-A@tw", raw=b"r1-AAAA", pmid="pm-1"), mailbox=self.mb, owner_user=self.u)
        self._ingest(self._msg(mid="MID-B@tw", raw=b"r2-BBBB", pmid="pm-2"), mailbox=self.mb, owner_user=self.u)
        run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 2)

    @override_settings(BROKER_CROSS_INBOX_DEDUP_ENABLED="1")
    def test_shared_message_id_never_merges_across_owners(self):
        # A FORGED/reused Message-ID shared by TWO owners must NEVER collapse across owners. The collapse is doubly
        # owner-scoped: the sibling-observation lookup filters owner_id, AND the adoption Withdrawal filter is
        # trading_account-scoped (user-scoped accounts). Pins the "never cross-owner" invariant named in the spec.
        login = _uniq()
        other = User.objects.create_user(username="x%s" % login, email="%s@x.invalid" % login, password="x")
        acct_o = TradingAccount.objects.create(user=other, name="B", broker_name="TradersWay",
                                               account_number=login, is_demo=True, is_active=True)
        alias_o = mint_alias_for_account(acct_o)
        mb_o = ConnectedMailbox.objects.create(user=other, provider=ConnectedMailbox.Provider.GMAIL,
                                               provider_mailbox_id="x%s@gmail.com" % login,
                                               primary_email="x%s@gmail.com" % login,
                                               status=ConnectedMailbox.Status.CONNECTED)
        self._ingest(self._msg(mid="FORGED-SHARED@tw", raw=b"owner-u-AAAA", pmid="pm-u"),
                     mailbox=self.mb, owner_user=self.u)
        other_msg = MailMessage(raw_bytes=b"owner-o-BBBB", to_addresses=(alias_o.address(),),
                                from_address="noreply@tradersway.invalid", subject="Withdrawal request received",
                                body="withdraw 100.50 USD", received_at=timezone.now(),
                                provider_message_id="pm-o", rfc822_message_id="FORGED-SHARED@tw")
        ingestion.ingest_message(other_msg, store=self.store, provenance=Provenance.SYNTHETIC,
                                 owner_user=other, connected_mailbox=mb_o)
        summary = run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 2)          # NEVER merged across owners
        self.assertFalse(summary.get(OUT_CROSS_INBOX))           # neither adopted a foreign sibling


class CorrelationArmingGuard(_Base):
    def test_correlation_refuses_without_cross_inbox_dedup(self):
        out = StringIO()
        with override_settings(BROKER_WITHDRAWAL_CORRELATION_ENABLED="1", BROKER_CROSS_INBOX_DEDUP_ENABLED="0"):
            call_command("run_withdrawal_correlation", stdout=out)
        self.assertIn("refusing", out.getvalue().lower())        # arming correlation alone is refused (fail-closed)

    def test_correlation_runs_with_both_flags(self):
        out = StringIO()
        with override_settings(BROKER_WITHDRAWAL_CORRELATION_ENABLED="1", BROKER_CROSS_INBOX_DEDUP_ENABLED="1"):
            call_command("run_withdrawal_correlation", stdout=out)
        self.assertIn("complete", out.getvalue().lower())

    def test_force_bypasses_both_gates(self):
        out = StringIO()
        call_command("run_withdrawal_correlation", "--force", stdout=out)   # both flags OFF
        self.assertIn("complete", out.getvalue().lower())
