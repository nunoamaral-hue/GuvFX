"""WP2 — evidence store + BrokerEvent (append-only) + Withdrawal (durable) schema.

Covers: content-addressed write-once store (dedup + tamper detection), pointer-not-inline, append-only enforcement
at BOTH the app layer (save/delete) AND the DB trigger layer (QuerySet.update / raw), money stored as Decimal,
provenance defaulting to SYNTHETIC (never REAL), and idempotent withdrawal correlation."""
import os
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.utils import InternalError, ProgrammingError
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence.evidence import EvidenceStore, EvidenceStoreError, evidence_root
from broker_intelligence.models import BrokerEvent, EvidenceBlob, Provenance, Withdrawal
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "91%04d" % _n


def _acct(**kw):
    login = _uniq()
    u = User.objects.create_user(username="wp2%s" % login, email="%s@x.invalid" % login, password="x")
    defaults = dict(user=u, name="A", broker_name="B", account_number=login, is_demo=True, is_active=True)
    defaults.update(kw)
    return TradingAccount.objects.create(**defaults)


class _TmpRootTest(TestCase):
    """Base: a configurable, isolated evidence root per test class (never a home/hard-coded path)."""

    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp(prefix="bi-evidence-test-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()

    def tearDown(self):
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)
        super().tearDown()


class EvidenceStoreTests(_TmpRootTest):
    RAW = b"From: broker@example.invalid\r\nSubject: Withdrawal request received\r\n\r\nRef ABC123, 100.50 USD\r\n"

    def test_configurable_root_is_honoured(self):
        self.assertEqual(evidence_root(), self._root)

    def test_put_stores_and_is_content_addressed(self):
        blob, created = self.store.put(self.RAW)
        self.assertTrue(created)
        self.assertEqual(len(blob.sha256), 64)
        self.assertEqual(blob.byte_size, len(self.RAW))
        self.assertEqual(blob.storage_backend, "file")
        # Pointer, not payload: the row carries no raw body field, and the key is sharded on the hash only.
        self.assertTrue(blob.storage_key.endswith(blob.sha256))
        self.assertFalse(hasattr(blob, "body"))
        self.assertFalse(hasattr(blob, "raw"))
        # The payload lives on disk under the configured root, not in the DB.
        self.assertTrue(os.path.exists(os.path.join(self._root, blob.storage_key)))

    def test_put_is_idempotent_dedup(self):
        b1, c1 = self.store.put(self.RAW)
        b2, c2 = self.store.put(self.RAW)
        self.assertTrue(c1)
        self.assertFalse(c2)                         # de-dup: identical bytes → same blob, not created again
        self.assertEqual(b1.pk, b2.pk)
        self.assertEqual(EvidenceBlob.objects.count(), 1)

    def test_distinct_content_distinct_blobs(self):
        b1, _ = self.store.put(self.RAW)
        b2, _ = self.store.put(self.RAW + b"different")
        self.assertNotEqual(b1.sha256, b2.sha256)
        self.assertNotEqual(b1.storage_key, b2.storage_key)

    def test_read_roundtrips_and_verifies(self):
        blob, _ = self.store.put(self.RAW)
        self.assertEqual(self.store.read(blob), self.RAW)
        self.assertTrue(self.store.verify(blob))

    def test_tamper_detection(self):
        blob, _ = self.store.put(self.RAW)
        with open(os.path.join(self._root, blob.storage_key), "wb") as fh:
            fh.write(b"tampered")
        self.assertFalse(self.store.verify(blob))
        with self.assertRaises(EvidenceStoreError):
            self.store.read(blob)

    def test_evidenceblob_app_immutable(self):
        blob, _ = self.store.put(self.RAW)
        blob.content_type = "text/plain"
        with self.assertRaises(ValidationError):
            blob.save()
        with self.assertRaises(ValidationError):
            blob.delete()

    def test_evidenceblob_db_trigger_blocks_update(self):
        blob, _ = self.store.put(self.RAW)
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                EvidenceBlob.objects.filter(pk=blob.pk).update(content_type="text/plain")

    def test_evidenceblob_db_trigger_blocks_bulk_delete(self):
        # The instance delete() guard is bypassed by the ORM bulk path; the BEFORE-DELETE trigger must still
        # refuse it (raw evidence is quarantined, never destroyed — and this closes the delete+re-put rewrite).
        blob, _ = self.store.put(self.RAW)
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                EvidenceBlob.objects.filter(pk=blob.pk).delete()
        self.assertTrue(EvidenceBlob.objects.filter(pk=blob.pk).exists())


class BrokerEventAppendOnlyTests(_TmpRootTest):
    def _event(self, **kw):
        a = _acct()
        blob, _ = self.store.put(b"raw-%s" % _uniq().encode())
        defaults = dict(
            trading_account=a, broker="TradersWay", event_type="WITHDRAWAL_REQUESTED",
            received_at=timezone.now(), amount=Decimal("100.50"), currency="USD",
            evidence=blob, evidence_hash=blob.sha256, parser_name="tradersway", parser_version="v1")
        defaults.update(kw)
        return BrokerEvent.objects.create(**defaults)

    def test_amount_is_decimal_not_float(self):
        e = self._event()
        e.refresh_from_db()
        self.assertIsInstance(e.amount, Decimal)
        self.assertEqual(e.amount, Decimal("100.50"))

    def test_provenance_defaults_synthetic_never_real(self):
        e = self._event()
        self.assertEqual(e.provenance, Provenance.SYNTHETIC)
        self.assertNotEqual(e.provenance, Provenance.REAL)

    def test_app_layer_rejects_evidential_edit(self):
        e = self._event()
        e.amount = Decimal("999.99")
        with self.assertRaises(ValidationError):
            e.save()

    def test_app_layer_allows_correlation_status_advance(self):
        e = self._event()
        e.correlation_status = BrokerEvent.CorrelationStatus.CORRELATED
        e.save()                                      # the ONE allowed mutation
        e.refresh_from_db()
        self.assertEqual(e.correlation_status, BrokerEvent.CorrelationStatus.CORRELATED)

    def test_delete_refused(self):
        e = self._event()
        with self.assertRaises(ValidationError):
            e.delete()

    def test_db_trigger_blocks_bulk_delete(self):
        # ORM bulk delete bypasses the instance delete(); the BEFORE-DELETE trigger must refuse it too.
        e = self._event()
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                BrokerEvent.objects.filter(pk=e.pk).delete()
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())

    def test_db_trigger_blocks_evidential_update(self):
        e = self._event()
        with self.assertRaises((InternalError, ProgrammingError)):
            with transaction.atomic():
                BrokerEvent.objects.filter(pk=e.pk).update(amount=Decimal("1.00"))

    def test_db_trigger_allows_correlation_status_update(self):
        e = self._event()
        n = BrokerEvent.objects.filter(pk=e.pk).update(
            correlation_status=BrokerEvent.CorrelationStatus.AMBIGUOUS)   # bookkeeping-only update is permitted
        self.assertEqual(n, 1)
        e.refresh_from_db()
        self.assertEqual(e.correlation_status, BrokerEvent.CorrelationStatus.AMBIGUOUS)


class WithdrawalTests(TestCase):
    def test_durable_status_advances(self):
        a = _acct()
        w = Withdrawal.objects.create(trading_account=a, broker_reference_id="REF1",
                                      amount=Decimal("250.00"), currency="USD",
                                      requested_at=timezone.now(), status=Withdrawal.Status.REQUESTED)
        w.status = Withdrawal.Status.COMPLETED
        w.completed_at = timezone.now()
        w.save()                                      # durable (not append-only)
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.COMPLETED)

    def test_reference_id_correlation_is_idempotent(self):
        a = _acct()
        Withdrawal.objects.create(trading_account=a, broker_reference_id="DUP", status=Withdrawal.Status.REQUESTED)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Withdrawal.objects.create(trading_account=a, broker_reference_id="DUP",
                                          status=Withdrawal.Status.REQUESTED)

    def test_blank_reference_allows_multiple(self):
        a = _acct()
        Withdrawal.objects.create(trading_account=a, broker_reference_id="", status=Withdrawal.Status.PENDING)
        Withdrawal.objects.create(trading_account=a, broker_reference_id="", status=Withdrawal.Status.PENDING)
        self.assertEqual(Withdrawal.objects.filter(trading_account=a).count(), 2)

    def test_duration_seconds(self):
        a = _acct()
        t0 = timezone.now()
        w = Withdrawal.objects.create(trading_account=a, requested_at=t0, status=Withdrawal.Status.PENDING)
        self.assertIsNone(w.duration_seconds())       # no completion yet
        w.completed_at = t0 + timezone.timedelta(seconds=3600)
        self.assertEqual(w.duration_seconds(), 3600)

    def test_provenance_defaults_synthetic(self):
        a = _acct()
        w = Withdrawal.objects.create(trading_account=a, status=Withdrawal.Status.PENDING)
        self.assertEqual(w.provenance, Provenance.SYNTHETIC)
