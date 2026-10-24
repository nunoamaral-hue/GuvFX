"""WP6.1 / ADR-0059 — tests for the append-only BrokerEvent classification revision + reclassify command.

All fixtures are FULLY SYNTHETIC (no genuine account/request numbers, amounts, or tokens). The external fixture
embeds a FAKE confirmation token purely to prove it NEVER reaches command output. No production data here.
"""
from __future__ import annotations

import tempfile
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.models import (BrokerEvent, BrokerEventClassificationRevision, Provenance,
                                        TransactionCategory)
from broker_intelligence.parsers_tradersway import register_default_parsers

_FAKE_TOKEN = "FAKECONFIRMTOKEN0123456789abcdef"   # synthetic; MUST never appear in command output/logs
_FAKE_REF = "9000001"                               # synthetic request number


def _raw_external(ref=_FAKE_REF, amount="250.00 USD"):
    """A genuine-SHAPED EXTERNAL withdrawal request (structured ``Payment method:`` rail + withdrawal direction, NO
    internal transfer-clause). The current certified parser derives EXTERNAL_WITHDRAWAL."""
    html = (f"<html><body><p>Received the request number #{ref} to withdraw funds from your account.</p>"
            f"<p>Amount: {amount}</p><p>Payment method: Ethereum</p>"
            f"<p>If you want to confirm the withdrawal request, click the link below:</p>"
            f"<p>https://po.tradersway.com/withdrawal/confirm?token={_FAKE_TOKEN}</p></body></html>")
    return (f"From: TradersWay <payments@tradersway.com>\r\nTo: member@example.com\r\n"
            f"Subject: Please confirm your withdrawal request\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n\r\n{html}").encode("utf-8")


def _raw_internal(ref=_FAKE_REF, src="1111111", dst="2222222"):
    """A genuine-SHAPED INTERNAL transfer (source->destination trading-account clause, NO external rail, the
    ``/confirmInner`` path). The current certified parser derives INTERNAL_TRANSFER."""
    html = (f"<html><body><p>Your account {src} in amount of 100.00 USD transferring to your trading account "
            f"{dst}.</p><p>Your withdrawal request number is: {ref}.</p>"
            f"<p>https://po.tradersway.com/withdrawal/confirmInner?token={_FAKE_TOKEN}</p></body></html>")
    return (f"From: TradersWay <payments@tradersway.com>\r\nTo: member@example.com\r\n"
            f"Subject: Please confirm your withdrawal request\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n\r\n{html}").encode("utf-8")


def _raw_marketing():
    """A generic marketing email mentioning 'withdrawal' in boilerplate — no withdrawal LIFECYCLE, so the parser
    declines (returns None)."""
    html = ("<html><body><p>Our weekly market update. Trade responsibly.</p>"
            "<p>You can request a withdrawal any time from your dashboard.</p></body></html>")
    return ("From: TradersWay <news@tradersway.com>\r\nTo: member@example.com\r\n"
            "Subject: Weekly market update\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html).encode("utf-8")


@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com")
class ClassificationRevisionTests(TestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="bi-rev-ev-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        register_default_parsers()
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", email="owner@example.com", password="x")
        self.other = User.objects.create_user(username="other", email="other@example.com", password="x")

    def tearDown(self):
        self._ctx.disable()

    def _event(self, raw, *, owner=None, category=TransactionCategory.UNKNOWN, reference=_FAKE_REF,
               evidence_hash=None, event_type="WITHDRAWAL_CONFIRMATION_REQUIRED"):
        """Create a stored BrokerEvent referencing ``raw`` with a (possibly STALE) ``category`` — simulating an event
        ingested under an older parser. The original row is what the command must never mutate."""
        blob, _ = self.store.put(raw, source=BrokerEvent.source.field.default)
        return BrokerEvent.objects.create(
            event_type=event_type, transaction_category=category, broker="TradersWay", source="EMAIL",
            received_at=timezone.now(), owner=(owner or self.owner), evidence=blob,
            evidence_hash=(evidence_hash if evidence_hash is not None else blob.sha256),
            parser_name="tradersway", parser_version="v1", broker_reference_id=reference, provenance=Provenance.REAL)

    def _run(self, ev, owner=None, **kw):
        out = StringIO()
        call_command("reclassify_broker_event", "--owner", str((owner or self.owner).id),
                     "--event", str(ev.id), stdout=out, **kw)
        return out.getvalue()

    # 1. Event-2 structure: stale UNKNOWN, current parser derives EXTERNAL_WITHDRAWAL -> revision appended.
    def test_external_stale_unknown_gets_revision(self):
        ev = self._event(_raw_external(), category=TransactionCategory.UNKNOWN)
        self._run(ev)
        rev = ev.classification_revisions.get()
        self.assertEqual(rev.derived_category, TransactionCategory.EXTERNAL_WITHDRAWAL)
        self.assertEqual(rev.original_category, TransactionCategory.UNKNOWN)
        self.assertEqual(ev.effective_transaction_category(), TransactionCategory.EXTERNAL_WITHDRAWAL)

    # 2. Internal /confirmInner message remains INTERNAL_TRANSFER (no revision when derived == effective).
    def test_internal_stays_internal_no_revision(self):
        ev = self._event(_raw_internal(), category=TransactionCategory.INTERNAL_TRANSFER)
        self._run(ev)
        self.assertEqual(ev.classification_revisions.count(), 0)
        self.assertEqual(ev.effective_transaction_category(), TransactionCategory.INTERNAL_TRANSFER)

    # 3. Revision appends WITHOUT mutating the original event (category + stored fields unchanged).
    def test_original_event_unchanged(self):
        ev = self._event(_raw_external(), category=TransactionCategory.UNKNOWN)
        self._run(ev)
        ev.refresh_from_db()
        self.assertEqual(ev.transaction_category, TransactionCategory.UNKNOWN)   # original immutable, intact
        self.assertEqual(ev.classification_revisions.count(), 1)

    # 4. Idempotent rerun: running the correction twice yields exactly one revision.
    def test_idempotent_rerun(self):
        ev = self._event(_raw_external(), category=TransactionCategory.UNKNOWN)
        self._run(ev)
        out2 = self._run(ev)
        self.assertEqual(ev.classification_revisions.count(), 1)
        self.assertIn("NO_CHANGE", out2)   # second run: derived == effective

    # 5. Concurrent rerun: a duplicate idempotency key cannot persist a second revision.
    def test_concurrent_duplicate_idempotency_key_rejected(self):
        ev = self._event(_raw_external(), category=TransactionCategory.UNKNOWN)
        self._run(ev)
        rev = ev.classification_revisions.get()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerEventClassificationRevision.objects.create(
                    event=ev, derived_category=TransactionCategory.EXTERNAL_WITHDRAWAL,
                    original_category=TransactionCategory.UNKNOWN, parser_name="tradersway", parser_version="v1",
                    classification_rule_version="ext-rail-v1", reason="dup", source_evidence_sha256=ev.evidence_hash,
                    actor="test", idempotency_key=rev.idempotency_key)

    # 6. Rollback/failure leaves no partial revision (a refused run writes nothing).
    def test_refusal_leaves_no_partial_revision(self):
        ev = self._event(_raw_external(), category=TransactionCategory.UNKNOWN, evidence_hash="deadbeef" * 8)
        with self.assertRaises(CommandError):
            self._run(ev)
        self.assertEqual(ev.classification_revisions.count(), 0)

    # 7. Tenant isolation / 8. wrong owner rejected.
    def test_wrong_owner_rejected(self):
        ev = self._event(_raw_external(), owner=self.owner, category=TransactionCategory.UNKNOWN)
        with self.assertRaises(CommandError):
            self._run(ev, owner=self.other)
        self.assertEqual(ev.classification_revisions.count(), 0)

    # 9. Evidence mismatch rejected (recorded hash != recomputed).
    def test_evidence_mismatch_rejected(self):
        ev = self._event(_raw_external(), category=TransactionCategory.UNKNOWN, evidence_hash="0" * 64)
        with self.assertRaises(CommandError):
            self._run(ev)
        self.assertEqual(ev.classification_revisions.count(), 0)

    # 10. Parser failure / decline rejected (no revision for a non-withdrawal message).
    def test_parser_decline_rejected(self):
        ev = self._event(_raw_marketing(), category=TransactionCategory.UNKNOWN, event_type="OTHER")
        with self.assertRaises(CommandError):
            self._run(ev)
        self.assertEqual(ev.classification_revisions.count(), 0)

    # 11. Projection prefers the valid latest revision: after correction, correlation projects a Withdrawal.
    def test_projection_uses_effective_category(self):
        from broker_intelligence.correlation import run_correlation
        from broker_intelligence.models import Withdrawal
        ev = self._event(_raw_external(), category=TransactionCategory.UNKNOWN)
        self.assertEqual(Withdrawal.objects.count(), 0)
        self._run(ev)
        run_correlation()
        self.assertEqual(Withdrawal.objects.filter(owner=self.owner).count(), 1)

    # 12. No revision -> original category (an internal event never projects a Withdrawal).
    def test_no_revision_falls_back_to_original(self):
        from broker_intelligence.correlation import run_correlation
        from broker_intelligence.models import Withdrawal
        self._event(_raw_internal(), category=TransactionCategory.INTERNAL_TRANSFER)
        run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 0)

    # 13. Dry-run writes nothing. / Sensitive URL token never enters command output.
    def test_dry_run_and_no_token_in_output(self):
        ev = self._event(_raw_external(), category=TransactionCategory.UNKNOWN)
        out = self._run(ev, **{"dry_run": True})
        self.assertEqual(ev.classification_revisions.count(), 0)
        self.assertIn("DRY_RUN", out)
        # the fake confirmation token and request number must NEVER appear in output
        self.assertNotIn(_FAKE_TOKEN, out)
        out2 = self._run(ev)
        self.assertNotIn(_FAKE_TOKEN, out2)
        self.assertNotIn(_FAKE_REF, out2)
