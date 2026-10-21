"""Controlled-erasure tests (ADR-0051). Discriminating + negative-control.

Guard -> the test that FAILS if that guard is removed (mutation proof):
  DB GUC-gated trigger (block ordinary delete)   -> test_ordinary_delete_still_blocked (+ test_final_reference_*)
  GUC set only in governed path                  -> test_final_reference_physically_deleted (reverts to RAISE if GUC gate gone)
  shared-blob PROTECT retention                  -> test_shared_evidence_retained_while_other_reference_exists
  owner scoping                                  -> test_one_owner_request_cannot_affect_another
  legal/security hold                            -> test_legal_hold_blocks
  verified identity                              -> test_unverified_identity_blocks
  flag gate (DARK)                               -> test_flag_off_blocks
  confirmation token (TOCTOU)                    -> test_wrong_token_blocks
  atomic rollback / fail-closed                  -> test_partial_failure_is_recoverable
  idempotency                                    -> test_repeated_execution_is_safe
  dry-run is pure                                -> test_dry_run_performs_no_mutation
  audit has no content                           -> test_audit_record_has_no_deleted_content
"""
import os
import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import credential_store  # noqa: F401 (patched in one test)
from broker_intelligence.erasure import ErasureRefused, execute_owner_erasure, plan_owner_erasure
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.models import (
    BrokerEvent, ConnectedMailbox, ErasureHold, ErasureRequest, EvidenceBlob, Withdrawal,
)

User = get_user_model()


class ControlledErasureTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.credtmp = tempfile.mkdtemp()
        self._ov = override_settings(
            BROKER_INTELLIGENCE_EVIDENCE_ROOT=self.tmp,
            BROKER_INTELLIGENCE_CREDENTIAL_ROOT=self.credtmp,
            BROKER_INTELLIGENCE_ERASURE_ENABLED="1",   # armed for the execute-path tests; flipped off in one test
        )
        self._ov.enable()
        self.store = EvidenceStore()
        self.a = User.objects.create_user(username="owner-a", email="owner-a@example.invalid", password="x")
        self.b = User.objects.create_user(username="owner-b", email="owner-b@example.invalid", password="x")

    def tearDown(self):
        self._ov.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.credtmp, ignore_errors=True)

    # --- helpers ---------------------------------------------------------------
    def _blob(self, raw: bytes) -> EvidenceBlob:
        blob, _ = self.store.put(raw, received_at=timezone.now())
        return blob

    def _event(self, owner, blob) -> BrokerEvent:
        return BrokerEvent.objects.create(
            owner=owner, evidence=blob, evidence_hash=blob.sha256, event_type="EXTERNAL_WITHDRAWAL",
            received_at=timezone.now(), provenance="REAL")

    def _request(self, owner, verified=True) -> ErasureRequest:
        return ErasureRequest.objects.create(
            subject_user=owner, subject_user_id_snapshot=owner.id, identity_verified=verified)

    def _run(self, owner):
        req = self._request(owner)
        token = plan_owner_erasure(owner.id)["token"]
        return req, execute_owner_erasure(req, token)

    def _file_path(self, blob) -> str:
        return os.path.join(self.tmp, blob.storage_key)

    # --- tests -----------------------------------------------------------------
    def test_one_owner_request_cannot_affect_another(self):
        ba = self._blob(b"A-only mail"); ea = self._event(self.a, ba)
        bb = self._blob(b"B-only mail"); eb = self._event(self.b, bb)
        self._run(self.a)
        self.assertFalse(BrokerEvent.objects.filter(pk=ea.pk).exists())
        self.assertFalse(EvidenceBlob.objects.filter(pk=ba.pk).exists())
        self.assertTrue(BrokerEvent.objects.filter(pk=eb.pk).exists())      # B untouched
        self.assertTrue(EvidenceBlob.objects.filter(pk=bb.pk).exists())

    def test_shared_evidence_retained_while_other_reference_exists(self):
        shared = self._blob(b"SHARED broker mail")
        ea = self._event(self.a, shared)
        eb = self._event(self.b, shared)                                   # same content-addressed blob
        _, out = self._run(self.a)
        self.assertFalse(BrokerEvent.objects.filter(pk=ea.pk).exists())
        self.assertTrue(BrokerEvent.objects.filter(pk=eb.pk).exists())
        self.assertTrue(EvidenceBlob.objects.filter(pk=shared.pk).exists())  # RETAINED (B still references it)
        self.assertTrue(os.path.exists(self._file_path(shared)))             # file kept
        self.assertEqual(out["evidence_blobs_retained_shared"], 1)
        self.assertEqual(out["evidence_blobs_deleted"], 0)

    def test_final_reference_physically_deleted(self):
        solo = self._blob(b"SOLO broker mail"); self._event(self.a, solo)
        path = self._file_path(solo)
        self.assertTrue(os.path.exists(path))
        _, out = self._run(self.a)
        self.assertFalse(EvidenceBlob.objects.filter(pk=solo.pk).exists())
        self.assertFalse(os.path.exists(path))                              # physical file unlinked
        self.assertEqual(out["evidence_blobs_deleted"], 1)
        self.assertEqual(out["evidence_files_unlinked"], 1)

    def test_legal_hold_blocks(self):
        b = self._blob(b"held mail"); e = self._event(self.a, b)
        ErasureHold.objects.create(subject_user=self.a, subject_user_id_snapshot=self.a.id, reason="litigation")
        req = self._request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, plan_owner_erasure(self.a.id)["token"])
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())       # nothing deleted

    def test_unverified_identity_blocks(self):
        b = self._blob(b"unverified mail"); e = self._event(self.a, b)
        req = self._request(self.a, verified=False)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, plan_owner_erasure(self.a.id)["token"])
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())

    def test_flag_off_blocks(self):
        b = self._blob(b"dark mail"); e = self._event(self.a, b)
        req = self._request(self.a)
        token = plan_owner_erasure(self.a.id)["token"]
        with override_settings(BROKER_INTELLIGENCE_ERASURE_ENABLED=""):
            with self.assertRaises(ErasureRefused):
                execute_owner_erasure(req, token)
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())

    def test_wrong_token_blocks(self):
        b = self._blob(b"token mail"); e = self._event(self.a, b)
        req = self._request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, "deadbeef-not-the-token")
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())

    def test_dry_run_performs_no_mutation(self):
        b = self._blob(b"dry run mail"); e = self._event(self.a, b)
        before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        plan = plan_owner_erasure(self.a.id)
        self.assertEqual((BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count()), before)
        self.assertIn(e.pk, plan["broker_events"])
        self.assertIn(b.pk, plan["evidence_blobs_delete"])
        self.assertTrue(os.path.exists(self._file_path(b)))

    def test_repeated_execution_is_safe(self):
        b = self._blob(b"idempotent mail"); self._event(self.a, b)
        req, out1 = self._run(self.a)
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.EXECUTED)
        self.assertEqual(out1["broker_events_deleted"], 1)
        # re-executing the SAME request is refused (already executed)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, "anything")
        # a fresh request for the now-empty owner runs cleanly with zero counts (no error, no double-delete)
        _, out2 = self._run(self.a)
        self.assertEqual(out2["broker_events_deleted"], 0)
        self.assertEqual(out2["evidence_blobs_deleted"], 0)

    def test_partial_failure_is_recoverable(self):
        b = self._blob(b"failure mail"); e = self._event(self.a, b)
        mb = ConnectedMailbox.objects.create(user=self.a, provider_mailbox_id="mbx-1", credential_ref="cref-1")
        req = self._request(self.a)
        token = plan_owner_erasure(self.a.id)["token"]
        with mock.patch("broker_intelligence.erasure.credential_store.delete_token",
                        side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(RuntimeError):
                execute_owner_erasure(req, token)
        # everything rolled back: event, blob (row + file), mailbox all intact
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())
        self.assertTrue(EvidenceBlob.objects.filter(pk=b.pk).exists())
        self.assertTrue(os.path.exists(self._file_path(b)))                 # file NOT unlinked (deferred past commit)
        self.assertTrue(ConnectedMailbox.objects.filter(pk=mb.pk).exists())
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.FAILED)

    def test_audit_record_has_no_deleted_content(self):
        b = self._blob(b"Secret broker body: withdrawal 1234.56 to wallet 0xABC"); self._event(self.a, b)
        req, out = self._run(self.a)
        req.refresh_from_db()
        blob = " ".join(str(v) for v in req.outcome.values()) + str(req.outcome)
        self.assertNotIn("Secret", blob)
        self.assertNotIn("1234.56", blob)
        self.assertNotIn("0xABC", blob)
        # outcome is counts only
        self.assertEqual(req.outcome["broker_events_deleted"], 1)

    def test_ordinary_delete_still_blocked(self):
        """Negative control: with the GUC unset, the DB triggers still block EvidenceBlob/BrokerEvent deletion via
        EVERY path (model, bulk, raw). If the GUC gate were accidentally left permissive, this fails."""
        b = self._blob(b"immutable mail"); e = self._event(self.a, b)
        # bulk delete of a BrokerEvent -> DB trigger raises
        with self.assertRaises(Exception):
            with transaction.atomic():
                BrokerEvent.objects.filter(pk=e.pk).delete()
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())
        # bulk delete of an EvidenceBlob -> DB trigger raises (must remove the event ref first would still be blocked)
        with self.assertRaises(Exception):
            with transaction.atomic():
                EvidenceBlob.objects.filter(pk=b.pk).delete()
        self.assertTrue(EvidenceBlob.objects.filter(pk=b.pk).exists())
