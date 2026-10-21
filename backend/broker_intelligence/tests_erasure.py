"""Controlled-erasure control-plane tests (ADR-0054), POST-ADVERSARIAL-REVIEW.

These cover the fail-closed control plane this build ships — the pure dry-run plan, the enhanced TOCTOU token
(credential-ref drift), the two-person separation-of-duties lifecycle, the comprehensive readiness probe, the
validation gates, the idempotent file-deletion outbox worker, and the Finding-2 negative controls proving
immutability is enforced by a privileged ROLE (not a forgeable GUC). The destructive DB deletion runs only
through the separately-provisioned privileged backend and is NOT exercised here (execute fails closed).

Guard -> the test that FAILS if that guard is removed:
  DB role-gated immutability (not GUC)   -> test_ordinary_delete_blocked, test_old_guc_cannot_bypass
  flag gate (DARK)                       -> test_flag_off_blocks
  executable-status gate (APPROVED)      -> test_non_executable_status_blocks, test_planned_but_unapproved_blocks
  verified identity                      -> test_unverified_identity_blocks
  two-person approval (approver!=req)    -> test_self_approval_blocked, test_approver_equals_requester_blocks_exec
  two-person needs a recorded requester  -> test_approve_blocked_when_no_requester, test_execute_blocked_when_no_requester
  no verifier overwrite after approval   -> test_verify_blocked_after_approval
  approval validity                      -> test_revoked_approval_blocks, test_expired_approval_blocks
  outbox dead-letters a poison task      -> test_dead_letter_after_max_attempts
  legal/security hold                    -> test_legal_hold_blocks
  confirmation token (TOCTOU)            -> test_wrong_token_blocks
  credential-ref drift invalidates token -> test_credential_ref_drift_invalidates_token
  fail-closed on unprovisioned backend   -> test_execute_fails_closed_when_backend_unprovisioned
  readiness reports structured reasons   -> test_readiness_reports_reasons_when_unprovisioned
  dry-run is pure                        -> test_dry_run_performs_no_mutation
  observation-only evidence (finding 5)  -> test_observation_only_blob_is_a_delete_candidate
  outbox worker drains + completes       -> test_worker_removes_files_and_completes_request
  worker idempotency / missing file      -> test_worker_missing_file_is_success, test_worker_rerun_is_noop

DEFERRED to the provisioned PostgreSQL-15 rehearsal (require superuser role creation + ownership moves, which
the app role cannot do; see ops/erasure/provision_erasure_privilege.sql and WV1_ERASURE_REWORK_PACKET.md):
provisioning runs clean + is idempotent; the governed procedure is the ONLY path that deletes; the app role and
PUBLIC cannot EXECUTE it; only guvfx_erasure_exec can; a spoofed caller gains no authority; TRUNCATE is blocked
by the revocation; trigger DISABLE/DROP is denied to the app role; two competing executes yield one terminal
outcome; a crash leaves a durable PENDING task; audit PII is anonymised in-procedure.
"""
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence.erasure import (ErasureRefused, approve_request, execute_owner_erasure,
                                          plan_owner_erasure, privileged_backend_readiness,
                                          privileged_backend_ready, process_deletion_tasks, verify_identity)
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.models import (BrokerEvent, BrokerMessageObservation, ConnectedMailbox, DeletionTask,
                                         ErasureHold, ErasureRequest, EvidenceBlob, Withdrawal)

User = get_user_model()


class ControlledErasureControlPlaneTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cred_root = tempfile.mkdtemp()
        self._ov = override_settings(
            BROKER_INTELLIGENCE_EVIDENCE_ROOT=self.tmp,
            BROKER_INTELLIGENCE_CREDENTIAL_ROOT=self.cred_root,
            BROKER_INTELLIGENCE_ERASURE_ENABLED="1",   # armed; flipped off in one test
        )
        self._ov.enable()
        self.store = EvidenceStore()
        self.a = User.objects.create_user(username="owner-a", email="owner-a@example.invalid", password="x")
        self.b = User.objects.create_user(username="owner-b", email="owner-b@example.invalid", password="x")
        # Distinct staff operators for the separation-of-duties lifecycle.
        self.requester = User.objects.create_user(username="req-op", email="req@example.invalid", password="x")
        self.verifier = User.objects.create_user(username="ver-op", email="ver@example.invalid", password="x")
        self.approver = User.objects.create_user(username="appr-op", email="appr@example.invalid", password="x")
        self.executor = User.objects.create_user(username="exec-op", email="exec@example.invalid", password="x")

    def tearDown(self):
        self._ov.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.cred_root, ignore_errors=True)

    # --- fixtures ---------------------------------------------------------
    def _blob(self, raw: bytes) -> EvidenceBlob:
        blob, _ = self.store.put(raw, received_at=timezone.now())
        return blob

    def _event(self, owner, blob) -> BrokerEvent:
        return BrokerEvent.objects.create(
            owner=owner, evidence=blob, evidence_hash=blob.sha256, event_type="EXTERNAL_WITHDRAWAL",
            received_at=timezone.now(), provenance="REAL")

    def _token(self, owner):
        return plan_owner_erasure(owner.id)["token"]

    def _approved_request(self, owner, **overrides) -> ErasureRequest:
        """A request at APPROVED with a valid two-person approval (approver != requester). Overrides let a single
        gate be broken for a negative test."""
        now = timezone.now()
        fields = dict(
            subject_user=owner, subject_user_id_snapshot=owner.id, requested_by=self.requester,
            identity_verified=True, verified_by=self.verifier, verified_at=now, verification_method="signed-request",
            approved_by=self.approver, approved_at=now,
            approval_expires_at=now + timezone.timedelta(hours=1), approval_revoked=False,
            status=ErasureRequest.Status.APPROVED)
        fields.update(overrides)
        return ErasureRequest.objects.create(**fields)

    # --- the privileged backend is NOT provisioned in tests (fail-closed by design) ---
    def test_backend_is_not_provisioned(self):
        self.assertFalse(privileged_backend_ready())

    def test_readiness_reports_reasons_when_unprovisioned(self):
        ready, reasons = privileged_backend_readiness()
        self.assertFalse(ready)
        self.assertTrue(reasons)                                   # structured, non-empty diagnostics
        self.assertTrue(any("guvfx_erasure" in r for r in reasons))

    # --- dry-run plan is pure ---------------------------------------------
    def test_dry_run_performs_no_mutation(self):
        b = self._blob(b"dry run mail"); e = self._event(self.a, b)
        before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
        plan = plan_owner_erasure(self.a.id)
        self.assertEqual((BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count()), before)
        self.assertIn(e.pk, plan["broker_events"])
        self.assertIn(b.pk, plan["evidence_blobs_delete"])

    def test_observation_only_blob_is_a_delete_candidate(self):
        """Finding 5a: a blob referenced ONLY by this owner's observation (no BrokerEvent) is still a candidate."""
        b = self._blob(b"observation only mail")
        obs = BrokerMessageObservation.objects.create(owner=self.a, evidence=b)
        self.assertFalse(BrokerEvent.objects.filter(evidence=b).exists())
        plan = plan_owner_erasure(self.a.id)
        self.assertIn(obs.pk, plan["observations"])            # the observation row is in scope
        self.assertIn(b.pk, plan["evidence_blobs_delete"])     # and its only-referenced blob is a delete candidate

    def test_shared_blob_classified_retained(self):
        shared = self._blob(b"shared mail")
        self._event(self.a, shared); self._event(self.b, shared)
        plan = plan_owner_erasure(self.a.id)
        self.assertIn(shared.pk, plan["evidence_blobs_retain_shared"])
        self.assertNotIn(shared.pk, plan["evidence_blobs_delete"])

    def test_credential_ref_drift_invalidates_token(self):
        """Finding 5b: if the subject re-authenticates (credential_ref changes) between plan and execute, the
        token must change even though every id set is identical."""
        mbx = ConnectedMailbox.objects.create(user=self.a, provider_mailbox_id="m1", credential_ref="croldref0001")
        token_before = plan_owner_erasure(self.a.id)["token"]
        mbx.credential_ref = "crnewref0002"            # re-auth: same mailbox row, new credential file
        mbx.save(update_fields=["credential_ref"])
        token_after = plan_owner_erasure(self.a.id)["token"]
        self.assertNotEqual(token_before, token_after)
        # And execute with the stale token is refused.
        req = self._approved_request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, token_before, executor=self.executor)

    # --- two-person SoD lifecycle -----------------------------------------
    def test_verify_then_plan_then_approve_happy_path(self):
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            status=ErasureRequest.Status.REQUESTED)
        verify_identity(req, self.verifier, "signed-request")
        req.refresh_from_db()
        self.assertTrue(req.identity_verified)
        self.assertEqual(req.verified_by_id, self.verifier.id)
        # produce a plan (PLANNED)
        plan = plan_owner_erasure(self.a.id)
        req.plan = plan; req.confirm_token = plan["token"]; req.status = ErasureRequest.Status.PLANNED
        req.save(update_fields=["plan", "confirm_token", "status"])
        approve_request(req, self.approver)
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.APPROVED)
        self.assertEqual(req.approved_by_id, self.approver.id)
        self.assertIsNotNone(req.approval_expires_at)

    def test_self_approval_blocked(self):
        """The approver cannot be the requester (two-person rule)."""
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            identity_verified=True, verified_by=self.verifier, verified_at=timezone.now(),
            plan={"x": 1}, confirm_token="t", status=ErasureRequest.Status.PLANNED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.requester)            # approver == requester
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.PLANNED)   # not advanced

    def test_approve_requires_verified_identity(self):
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            identity_verified=False, plan={"x": 1}, confirm_token="t", status=ErasureRequest.Status.PLANNED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.approver)

    def test_approve_requires_a_plan(self):
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            identity_verified=True, status=ErasureRequest.Status.REQUESTED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.approver)

    def test_approve_blocked_when_no_requester(self):
        """Round-3 adversarial HIGH: a request with NO recorded requester cannot be approved — the two-person
        rule would otherwise be vacuous (approver != None short-circuits)."""
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=None,
            identity_verified=True, verified_by=self.verifier, verified_at=timezone.now(),
            plan={"x": 1}, confirm_token="t", status=ErasureRequest.Status.PLANNED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.approver)
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.PLANNED)   # not advanced

    def test_execute_blocked_when_no_requester(self):
        """Even a contrived APPROVED row with no requester must refuse at execute (re-checked under the lock)."""
        req = self._approved_request(self.a, requested_by=None)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_verify_blocked_after_approval(self):
        """Identity verification cannot be re-recorded once the request is APPROVED (no verifier overwrite)."""
        req = self._approved_request(self.a)
        with self.assertRaises(ErasureRefused):
            verify_identity(req, self.executor, "tampered")

    # --- validation gates on execute (all refuse; nothing deleted) --------
    def test_flag_off_blocks(self):
        req = self._approved_request(self.a)
        with override_settings(BROKER_INTELLIGENCE_ERASURE_ENABLED=""):
            with self.assertRaises(ErasureRefused):
                execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_non_executable_status_blocks(self):
        req = self._approved_request(self.a, status=ErasureRequest.Status.REQUESTED)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_planned_but_unapproved_blocks(self):
        req = self._approved_request(self.a, status=ErasureRequest.Status.PLANNED,
                                     approved_by=None, approved_at=None, approval_expires_at=None)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_unverified_identity_blocks(self):
        req = self._approved_request(self.a, identity_verified=False)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_revoked_approval_blocks(self):
        req = self._approved_request(self.a, approval_revoked=True)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_expired_approval_blocks(self):
        req = self._approved_request(self.a, approval_expires_at=timezone.now() - timezone.timedelta(minutes=1))
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_approver_equals_requester_blocks_exec(self):
        """Even if a row is contrived with approver == requester, execute re-checks and refuses."""
        req = self._approved_request(self.a, approved_by=self.requester)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_legal_hold_blocks(self):
        ErasureHold.objects.create(subject_user=self.a, subject_user_id_snapshot=self.a.id, reason="litigation")
        req = self._approved_request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)

    def test_wrong_token_blocks(self):
        req = self._approved_request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, "deadbeef-not-the-token", executor=self.executor)

    def test_execute_fails_closed_when_backend_unprovisioned(self):
        """All gates pass + correct token, but the privileged backend is not provisioned -> fail-closed refusal.
        Nothing is deleted."""
        b = self._blob(b"fail closed mail"); e = self._event(self.a, b)
        req = self._approved_request(self.a)
        with self.assertRaises(ErasureRefused) as ctx:
            execute_owner_erasure(req, self._token(self.a), executor=self.executor)
        self.assertIn("not ready", str(ctx.exception).lower())
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())
        self.assertTrue(EvidenceBlob.objects.filter(pk=b.pk).exists())

    # --- durable file-deletion OUTBOX worker (Findings 3/4) ---------------
    def test_worker_removes_files_and_completes_request(self):
        """A worker drains PENDING tasks, removes the real files, and advances an EXECUTED request to COMPLETED."""
        blob = self._blob(b"to be physically erased")
        evidence_path = self.store._abs(blob.storage_key)
        import os
        cred_ref = "crworkertest0001"
        cred_path = os.path.join(self.cred_root, f"{cred_ref}.enc")
        with open(cred_path, "wb") as fh:
            fh.write(b"ciphertext")
        self.assertTrue(os.path.exists(evidence_path) and os.path.exists(cred_path))

        req = self._approved_request(self.a, status=ErasureRequest.Status.EXECUTED)
        DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.EVIDENCE_FILE, target_ref=blob.sha256)
        DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.CREDENTIAL_FILE, target_ref=cred_ref)

        result = process_deletion_tasks()
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(os.path.exists(evidence_path))
        self.assertFalse(os.path.exists(cred_path))
        self.assertFalse(DeletionTask.objects.exclude(status=DeletionTask.Status.COMPLETED).exists())
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.COMPLETED)

    def test_worker_missing_file_is_success(self):
        """A target file that is already gone is idempotent success (COMPLETED), not a failure."""
        req = self._approved_request(self.a, status=ErasureRequest.Status.EXECUTED)
        DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.EVIDENCE_FILE,
                                    target_ref="0" * 64)                      # no such evidence file
        DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.CREDENTIAL_FILE,
                                    target_ref="crmissingref0001")            # no such credential file
        result = process_deletion_tasks()
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["completed"], 2)
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.COMPLETED)

    def test_worker_rerun_is_noop(self):
        req = self._approved_request(self.a, status=ErasureRequest.Status.EXECUTED)
        DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.EVIDENCE_FILE, target_ref="1" * 64)
        process_deletion_tasks()
        again = process_deletion_tasks()
        self.assertEqual(again, {"completed": 0, "failed": 0, "dead": 0})

    def test_dead_letter_after_max_attempts(self):
        """A poison task (unlink always errors) is retried then DEAD-lettered; the request moves to FAILED (not
        COMPLETED and not a perpetual retry storm)."""
        req = self._approved_request(self.a, status=ErasureRequest.Status.EXECUTED)
        # A credential_ref containing '/' makes credential_store._path_for raise every attempt (poison task).
        task = DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.CREDENTIAL_FILE, target_ref="bad/ref")
        for _ in range(5):
            process_deletion_tasks()
        task.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(task.status, DeletionTask.Status.DEAD)
        self.assertGreaterEqual(task.attempts, 5)
        self.assertEqual(req.status, ErasureRequest.Status.FAILED)       # needs intervention, never COMPLETED
        # A further run does not resurrect the dead task.
        self.assertEqual(process_deletion_tasks(), {"completed": 0, "failed": 0, "dead": 0})

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

    # NOTE (adversarial-review HIGH): row-level BEFORE-DELETE triggers do NOT cover TRUNCATE, and the ordinary
    # app role (table owner, pre-provisioning) can TRUNCATE / DISABLE / DROP triggers via DDL. Those owner-level
    # vectors are closed by the SEPARATE superuser provisioning (ops/erasure/provision_erasure_privilege.sql),
    # which moves object ownership to guvfx_schema_owner + REVOKEs DELETE/TRUNCATE from the app role — not by this
    # app-role migration. A trigger-level TRUNCATE block was rejected: an owner bypasses any trigger, and it breaks
    # Django's test flush. The migration here closes exactly the DML DELETE vectors the forgeable GUC once gated.
