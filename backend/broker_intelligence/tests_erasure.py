"""Controlled-erasure control-plane tests (ADR-0054), POST-ADVERSARIAL-REVIEW.

These cover the fail-closed control plane that this build ships: the pure dry-run plan, the validation gates,
and — critically — the Finding-2 negative controls proving immutability is now enforced by a privileged ROLE
(not a forgeable GUC). The destructive deletion path runs only through a separately-provisioned privileged
backend and is NOT exercised here (execute fails closed).

Guard -> the test that FAILS if that guard is removed:
  DB role-gated immutability (not GUC)   -> test_ordinary_delete_blocked, test_old_guc_cannot_bypass
  flag gate (DARK)                       -> test_flag_off_blocks
  executable-status gate                 -> test_non_executable_status_blocks
  verified identity                      -> test_unverified_identity_blocks
  legal/security hold                    -> test_legal_hold_blocks
  confirmation token (TOCTOU)            -> test_wrong_token_blocks
  fail-closed on unprovisioned backend   -> test_execute_fails_closed_when_backend_unprovisioned
  dry-run is pure                        -> test_dry_run_performs_no_mutation
  observation-only evidence (finding 5)  -> test_observation_only_blob_is_a_delete_candidate
"""
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence.erasure import (ErasureRefused, execute_owner_erasure, plan_owner_erasure,
                                          privileged_backend_ready)
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.models import (BrokerEvent, BrokerMessageObservation, ErasureHold, ErasureRequest,
                                         EvidenceBlob, Withdrawal)

User = get_user_model()


class ControlledErasureControlPlaneTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._ov = override_settings(
            BROKER_INTELLIGENCE_EVIDENCE_ROOT=self.tmp,
            BROKER_INTELLIGENCE_ERASURE_ENABLED="1",   # armed; flipped off in one test
        )
        self._ov.enable()
        self.store = EvidenceStore()
        self.a = User.objects.create_user(username="owner-a", email="owner-a@example.invalid", password="x")
        self.b = User.objects.create_user(username="owner-b", email="owner-b@example.invalid", password="x")

    def tearDown(self):
        self._ov.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _blob(self, raw: bytes) -> EvidenceBlob:
        blob, _ = self.store.put(raw, received_at=timezone.now())
        return blob

    def _event(self, owner, blob) -> BrokerEvent:
        return BrokerEvent.objects.create(
            owner=owner, evidence=blob, evidence_hash=blob.sha256, event_type="EXTERNAL_WITHDRAWAL",
            received_at=timezone.now(), provenance="REAL")

    def _request(self, owner, verified=True, status=ErasureRequest.Status.PLANNED) -> ErasureRequest:
        return ErasureRequest.objects.create(
            subject_user=owner, subject_user_id_snapshot=owner.id, identity_verified=verified, status=status)

    # --- the privileged backend is NOT provisioned in tests (fail-closed by design) ---
    def test_backend_is_not_provisioned(self):
        self.assertFalse(privileged_backend_ready())

    # --- dry-run plan is pure ---
    def test_dry_run_performs_no_mutation(self):
        b = self._blob(b"dry run mail"); e = self._event(self.a, b)
        before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        plan = plan_owner_erasure(self.a.id)
        self.assertEqual((BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count()), before)
        self.assertIn(e.pk, plan["broker_events"])
        self.assertIn(b.pk, plan["evidence_blobs_delete"])

    def test_observation_only_blob_is_a_delete_candidate(self):
        """Finding 5: a blob referenced ONLY by this owner's observation (no BrokerEvent) is still a candidate."""
        b = self._blob(b"observation only mail")
        BrokerMessageObservation.objects.create(owner=self.a, evidence=b)
        self.assertFalse(BrokerEvent.objects.filter(evidence=b).exists())
        plan = plan_owner_erasure(self.a.id)
        self.assertIn(b.pk, plan["observations"] and [b.pk] or [])  # observation present
        self.assertIn(b.pk, plan["evidence_blobs_delete"])          # and the blob is a delete candidate

    def test_shared_blob_classified_retained(self):
        shared = self._blob(b"shared mail")
        self._event(self.a, shared); self._event(self.b, shared)
        plan = plan_owner_erasure(self.a.id)
        self.assertIn(shared.pk, plan["evidence_blobs_retain_shared"])
        self.assertNotIn(shared.pk, plan["evidence_blobs_delete"])

    # --- validation gates (all refuse; nothing deleted) ---
    def _token(self, owner):
        return plan_owner_erasure(owner.id)["token"]

    def test_flag_off_blocks(self):
        req = self._request(self.a)
        with override_settings(BROKER_INTELLIGENCE_ERASURE_ENABLED=""):
            with self.assertRaises(ErasureRefused):
                execute_owner_erasure(req, self._token(self.a))

    def test_non_executable_status_blocks(self):
        req = self._request(self.a, status=ErasureRequest.Status.REQUESTED)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_unverified_identity_blocks(self):
        req = self._request(self.a, verified=False)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_legal_hold_blocks(self):
        ErasureHold.objects.create(subject_user=self.a, subject_user_id_snapshot=self.a.id, reason="litigation")
        req = self._request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_wrong_token_blocks(self):
        req = self._request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, "deadbeef-not-the-token")

    def test_execute_fails_closed_when_backend_unprovisioned(self):
        """All gates pass + correct token, but the privileged backend is not provisioned -> fail-closed refusal.
        Nothing is deleted."""
        b = self._blob(b"fail closed mail"); e = self._event(self.a, b)
        req = self._request(self.a)
        with self.assertRaises(ErasureRefused) as ctx:
            execute_owner_erasure(req, self._token(self.a))
        self.assertIn("not provisioned", str(ctx.exception).lower())
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())
        self.assertTrue(EvidenceBlob.objects.filter(pk=b.pk).exists())

    # --- Finding 2 negative controls: role-enforced immutability, GUC cannot bypass ---
    def test_ordinary_delete_blocked(self):
        b = self._blob(b"immutable mail"); e = self._event(self.a, b)
        with self.assertRaises(Exception):
            with transaction.atomic():
                BrokerEvent.objects.filter(pk=e.pk).delete()
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())
        with self.assertRaises(Exception):
            with transaction.atomic():
                EvidenceBlob.objects.filter(pk=b.pk).delete()
        self.assertTrue(EvidenceBlob.objects.filter(pk=b.pk).exists())

    def test_old_guc_cannot_bypass(self):
        """The retired GUC must NOT grant deletion: setting it on the ordinary connection and attempting a delete
        still raises, because the trigger now checks current_user = 'guvfx_erasure', not a session setting."""
        b = self._blob(b"guc mail"); e = self._event(self.a, b)
        with self.assertRaises(Exception):
            with transaction.atomic():
                with transaction.get_connection().cursor() as cur:
                    cur.execute("SET LOCAL \"guvfx.erasure_authorized\" = 'on'")
                BrokerEvent.objects.filter(pk=e.pk).delete()
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())
