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

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence.erasure import (ErasureRefused, approve_request, erasure_executor_alias,
                                          execute_owner_erasure, plan_owner_erasure, privileged_backend_readiness,
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
            approved_by=self.approver, assigned_executor=self.executor, approved_at=now,
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

    def _assert_required_migration(self, migration_name, surviving_table):
        """Discriminating control: readiness keys on the EXACT migration RECORD, not on the table/columns the
        migration created. With the record present, no 'not applied' reason names it; delete ONLY the record
        (the table/columns remain) and readiness must report it missing."""
        from django.db import connection
        def _not_applied_reason_present():
            return any(migration_name in r and "not applied" in r for r in privileged_backend_readiness()[1])
        self.assertFalse(_not_applied_reason_present())            # present (migrated) -> not flagged
        with connection.cursor() as cur:
            cur.execute("DELETE FROM django_migrations WHERE app='broker_intelligence' AND name=%s",
                        [migration_name])
            cur.execute("SELECT to_regclass(%s)", [surviving_table])
            self.assertIsNotNone(cur.fetchone()[0])                # the created table/columns STILL exist
        ready, reasons = privileged_backend_readiness()
        self.assertFalse(ready)
        self.assertTrue(_not_applied_reason_present(),             # record absent -> readiness names it NOT READY
                        f"readiness did not flag missing migration {migration_name}; reasons={reasons}")

    def test_readiness_requires_migration_0021_grant(self):
        self._assert_required_migration("0021_erasure_execution_grant",
                                        "broker_intelligence_erasureexecutiongrant")

    def test_readiness_requires_migration_0022_generation(self):
        self._assert_required_migration("0022_connectedmailbox_generation",
                                        "broker_intelligence_connectedmailbox")

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
            execute_owner_erasure(req, token_before)

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
        approve_request(req, self.approver, self.executor)
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.APPROVED)
        self.assertEqual(req.approved_by_id, self.approver.id)
        self.assertIsNotNone(req.approval_expires_at)

    def test_self_approval_blocked(self):
        """The approver cannot be the requester (two-person rule)."""
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            identity_verified=True, verified_by=self.verifier, verified_at=timezone.now(),
            verification_method="signed-request", plan={"x": 1}, confirm_token="t",
            status=ErasureRequest.Status.PLANNED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.requester, self.executor)            # approver == requester
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.PLANNED)   # not advanced

    def test_approve_requires_verified_identity(self):
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            identity_verified=False, plan={"x": 1}, confirm_token="t", status=ErasureRequest.Status.PLANNED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.approver, self.executor)

    def test_approve_requires_a_plan(self):
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            identity_verified=True, verified_by=self.verifier, verified_at=timezone.now(),
            verification_method="signed-request", status=ErasureRequest.Status.REQUESTED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.approver, self.executor)

    def test_verify_rejects_unknown_method(self):
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            status=ErasureRequest.Status.REQUESTED)
        with self.assertRaises(ErasureRefused):
            verify_identity(req, self.verifier, "banana-not-a-method")
        req.refresh_from_db()
        self.assertFalse(req.identity_verified)

    def test_verify_rejects_verifier_equals_requester(self):
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            status=ErasureRequest.Status.REQUESTED)
        with self.assertRaises(ErasureRefused):
            verify_identity(req, self.requester, "signed-request")   # verifier == requester

    def test_approve_rejects_approver_equals_verifier(self):
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            identity_verified=True, verified_by=self.verifier, verified_at=timezone.now(),
            verification_method="signed-request", plan={"x": 1}, confirm_token="t",
            status=ErasureRequest.Status.PLANNED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.verifier, self.executor)                      # approver == verifier

    def test_db_check_blocks_verified_without_method(self):
        """Finding A: the DB CHECK refuses a verified row with no method category (defense in depth)."""
        from django.db import IntegrityError, transaction as _txn
        with self.assertRaises(IntegrityError):
            with _txn.atomic():
                ErasureRequest.objects.create(
                    subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
                    identity_verified=True, verification_method="", status=ErasureRequest.Status.REQUESTED)

    def test_approve_blocked_when_no_requester(self):
        """Round-3 adversarial HIGH: a request with NO recorded requester cannot be approved — the two-person
        rule would otherwise be vacuous (approver != None short-circuits)."""
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=None,
            identity_verified=True, verified_by=self.verifier, verified_at=timezone.now(),
            verification_method="signed-request", plan={"x": 1}, confirm_token="t",
            status=ErasureRequest.Status.PLANNED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.approver, self.executor)
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.PLANNED)   # not advanced

    def test_execute_blocked_when_no_requester(self):
        """Even a contrived APPROVED row with no requester must refuse at execute (re-checked under the lock)."""
        req = self._approved_request(self.a, requested_by=None)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

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
                execute_owner_erasure(req, self._token(self.a))

    def test_non_executable_status_blocks(self):
        req = self._approved_request(self.a, status=ErasureRequest.Status.REQUESTED)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_planned_but_unapproved_blocks(self):
        req = self._approved_request(self.a, status=ErasureRequest.Status.PLANNED,
                                     approved_by=None, approved_at=None, approval_expires_at=None)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_unverified_identity_cannot_reach_approved_state(self):
        """Finding A: the DB CHECK makes an APPROVED request with unverified identity impossible to persist, so
        execution can never see one (defense in depth beyond the gate's own identity check)."""
        from django.db import IntegrityError, transaction as _txn
        with self.assertRaises(IntegrityError):
            with _txn.atomic():
                self._approved_request(self.a, identity_verified=False)

    def test_revoked_approval_blocks(self):
        req = self._approved_request(self.a, approval_revoked=True)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_expired_approval_blocks(self):
        req = self._approved_request(self.a, approval_expires_at=timezone.now() - timezone.timedelta(minutes=1))
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_approver_equals_requester_blocks_exec(self):
        """Even if a row is contrived with approver == requester, execute re-checks and refuses."""
        req = self._approved_request(self.a, approved_by=self.requester)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_legal_hold_blocks(self):
        ErasureHold.objects.create(subject_user=self.a, subject_user_id_snapshot=self.a.id, reason="litigation")
        req = self._approved_request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    def test_wrong_token_blocks(self):
        req = self._approved_request(self.a)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, "deadbeef-not-the-token")

    def test_execute_fails_closed_when_executor_unconfigured(self):
        """All lifecycle gates pass + correct token, but the dedicated executor connection is NOT configured in
        this environment (Finding C) -> fail-closed refusal. Nothing is deleted. (The full provisioned path is
        exercised against synthetic data in the disposable PG15 rehearsal, not in unit tests.)"""
        b = self._blob(b"fail closed mail"); e = self._event(self.a, b)
        req = self._approved_request(self.a)
        self.assertIsNone(erasure_executor_alias())      # no ERASURE_EXECUTOR_DB_* in the unit-test environment
        with self.assertRaises(ErasureRefused) as ctx:
            execute_owner_erasure(req, self._token(self.a))
        self.assertIn("fail-closed", str(ctx.exception).lower())
        self.assertTrue(BrokerEvent.objects.filter(pk=e.pk).exists())
        self.assertTrue(EvidenceBlob.objects.filter(pk=b.pk).exists())

    def test_approve_rejects_assigned_executor_equals_approver(self):
        """Two-person (P0): the ASSIGNED executor must differ from the approver (checked at approval)."""
        req = ErasureRequest.objects.create(
            subject_user=self.a, subject_user_id_snapshot=self.a.id, requested_by=self.requester,
            identity_verified=True, verified_by=self.verifier, verified_at=timezone.now(),
            verification_method="signed-request", plan={"x": 1}, confirm_token="t",
            status=ErasureRequest.Status.PLANNED)
        with self.assertRaises(ErasureRefused):
            approve_request(req, self.approver, self.approver)        # assigned executor == approver

    def test_execute_blocked_when_no_assigned_executor(self):
        """The gate refuses a request with no assigned executor (the governed procedure also re-checks it)."""
        req = self._approved_request(self.a, assigned_executor=None)
        with self.assertRaises(ErasureRefused):
            execute_owner_erasure(req, self._token(self.a))

    # --- durable file-deletion OUTBOX worker (Findings 3/4) ---------------
    def test_worker_removes_files_and_completes_request(self):
        """A worker drains PENDING tasks, removes the real files, and advances an EXECUTED request to COMPLETED."""
        import hashlib
        import os
        # Write the content-addressed evidence file WITHOUT a blob row — the governed procedure deletes the
        # EvidenceBlob row before the outbox task is drained, so the worker's "no live blob re-references this
        # content" guard must permit the unlink.
        raw = b"to be physically erased"
        sha = hashlib.sha256(raw).hexdigest()
        evidence_path = self.store._abs(f"{sha[:2]}/{sha[2:4]}/{sha}")
        os.makedirs(os.path.dirname(evidence_path), exist_ok=True)
        with open(evidence_path, "wb") as fh:
            fh.write(raw)
        cred_ref = "crworkertest0001"
        cred_path = os.path.join(self.cred_root, f"{cred_ref}.enc")
        with open(cred_path, "wb") as fh:
            fh.write(b"ciphertext")
        self.assertTrue(os.path.exists(evidence_path) and os.path.exists(cred_path))

        req = self._approved_request(self.a, status=ErasureRequest.Status.EXECUTED)
        DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.EVIDENCE_FILE, target_ref=sha)
        DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.CREDENTIAL_FILE, target_ref=cred_ref)

        result = process_deletion_tasks()
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(os.path.exists(evidence_path))
        self.assertFalse(os.path.exists(cred_path))
        self.assertFalse(DeletionTask.objects.exclude(status=DeletionTask.Status.COMPLETED).exists())
        req.refresh_from_db()
        self.assertEqual(req.status, ErasureRequest.Status.COMPLETED)

    def test_worker_retains_file_still_referenced_by_live_blob(self):
        """Re-ingestion race guard: if a LIVE EvidenceBlob now re-uses the task's content hash, the worker must
        NOT unlink the file (it backs a live row). Task still COMPLETES, with a note; file survives."""
        import os
        blob = self._blob(b"content re-ingested by another owner")   # creates a live row + file
        path = self.store._abs(blob.storage_key)
        req = self._approved_request(self.a, status=ErasureRequest.Status.EXECUTED)
        DeletionTask.objects.create(request=req, kind=DeletionTask.Kind.EVIDENCE_FILE, target_ref=blob.sha256)
        res = process_deletion_tasks()
        self.assertEqual(res["failed"], 0)
        self.assertTrue(os.path.exists(path))                        # file retained — a live blob references it
        task = DeletionTask.objects.get(request=req)
        self.assertEqual(task.status, DeletionTask.Status.COMPLETED)
        self.assertIn("re-referenced", task.last_error)

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


class ReadinessAppRolePostureTests(TestCase):
    """P0 superuser blocker: privileged_backend_readiness() must refuse whenever the ORDINARY application role
    carries an elevated cluster attribute (SUPERUSER / CREATEROLE / CREATEDB / BYPASSRLS) or is a member of /
    can assume a privileged role. Each forbidden condition is proven INDEPENDENTLY, with a positive control
    (a clean least-privilege role produces NONE of these reasons), so the checks cannot pass vacuously.

    Mechanism: these catalog checks evaluate the role NAMED by DATABASES['default']['USER']; the test creates a
    role with a specific posture and points that name at it (the live connection stays the test superuser). Roles
    are created in-transaction and removed on cleanup/rollback.
    """
    _PRIV = ("guvfx_schema_owner", "guvfx_erasure", "guvfx_erasure_exec", "guvfx_migrate")

    def setUp(self):
        # These checks CREATE ROLE ... SUPERUSER (to exercise the superuser/transitive posture), which requires
        # the test DB login to be an actual SUPERUSER. On any less-privileged test cluster, skip rather than
        # ERROR (evidence-rule portability); on a superuser CI DB they run.
        with connection.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            row = cur.fetchone()
        if not (row and row[0]):
            self.skipTest("test DB role is not SUPERUSER; readiness-posture checks verified on a superuser DB")

    def _mkrole(self, name, opts):
        with connection.cursor() as cur:
            cur.execute(f"DROP ROLE IF EXISTS {name}")
            cur.execute(f"CREATE ROLE {name} {opts}")
        self.addCleanup(self._drop, name)

    def _drop(self, name):
        with connection.cursor() as cur:
            cur.execute(f"DROP ROLE IF EXISTS {name}")

    def _readiness_as(self, rolename):
        orig = settings.DATABASES["default"]["USER"]
        settings.DATABASES["default"]["USER"] = rolename
        try:
            return privileged_backend_readiness()
        finally:
            settings.DATABASES["default"]["USER"] = orig

    def _has(self, reasons, substr):
        return any(substr in r for r in reasons)

    # --- attribute checks (run regardless of provisioning state) ----------------------------------------
    def test_refuses_superuser_app_role(self):
        self._mkrole("relp_super", "NOLOGIN SUPERUSER")
        ready, reasons = self._readiness_as("relp_super")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "must be NOSUPERUSER"), reasons)
        # independence: the other attribute reasons must NOT be raised
        for s in ("NOCREATEROLE", "NOCREATEDB", "NOBYPASSRLS"):
            self.assertFalse(self._has(reasons, s), f"unexpected {s}: {reasons}")

    def test_refuses_createrole_app_role(self):
        self._mkrole("relp_cr", "NOLOGIN NOSUPERUSER CREATEROLE")
        ready, reasons = self._readiness_as("relp_cr")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "NOCREATEROLE"), reasons)
        self.assertFalse(self._has(reasons, "must be NOSUPERUSER"), reasons)

    def test_refuses_createdb_app_role(self):
        self._mkrole("relp_cd", "NOLOGIN NOSUPERUSER CREATEDB")
        ready, reasons = self._readiness_as("relp_cd")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "NOCREATEDB"), reasons)
        self.assertFalse(self._has(reasons, "NOCREATEROLE"), reasons)

    def test_refuses_bypassrls_app_role(self):
        self._mkrole("relp_br", "NOLOGIN NOSUPERUSER BYPASSRLS")
        ready, reasons = self._readiness_as("relp_br")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "NOBYPASSRLS"), reasons)
        self.assertFalse(self._has(reasons, "NOCREATEDB"), reasons)

    def test_clean_app_role_raises_no_posture_reasons(self):
        """Positive control: a least-privilege role triggers NONE of the attribute reasons (the DB is still
        unprovisioned, so readiness is False for OTHER reasons — just not these)."""
        self._mkrole("relp_clean", "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS")
        ready, reasons = self._readiness_as("relp_clean")
        self.assertFalse(ready)
        for s in ("must be NOSUPERUSER", "NOCREATEROLE", "NOCREATEDB", "NOBYPASSRLS", "able to assume",
                  "must own nothing"):
            self.assertFalse(self._has(reasons, s), f"unexpected posture reason {s}: {reasons}")

    def test_refuses_app_role_that_owns_an_object(self):
        # F-8: the runtime app role must own nothing in 'public'. A role that still owns an object -> NOT READY.
        self._mkrole("relp_owner", "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS")
        with connection.cursor() as cur:
            cur.execute("CREATE TABLE public._relp_owned_tbl (i int)")
            cur.execute("ALTER TABLE public._relp_owned_tbl OWNER TO relp_owner")
        self.addCleanup(lambda: self._exec("DROP TABLE IF EXISTS public._relp_owned_tbl"))
        ready, reasons = self._readiness_as("relp_owner")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "must own nothing"), reasons)

    def test_refuses_app_role_that_can_execute_a_security_definer_routine(self):
        # F-2.8: no SECURITY DEFINER routine in 'public' may be EXECUTE-able by the runtime app role. A runtime
        # role that could run a definer-rights routine would run with that owner's privileges, bypassing the
        # REVOKEs. (PUBLIC's default EXECUTE is revoked first so this proves the app-role-specific path.)
        self._mkrole("relp_secdef", "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS")
        with connection.cursor() as cur:
            cur.execute("CREATE FUNCTION public._relp_sd() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'")
            cur.execute("REVOKE EXECUTE ON FUNCTION public._relp_sd() FROM PUBLIC")
            cur.execute("GRANT EXECUTE ON FUNCTION public._relp_sd() TO relp_secdef")
        self.addCleanup(lambda: self._exec("DROP FUNCTION IF EXISTS public._relp_sd()"))
        ready, reasons = self._readiness_as("relp_secdef")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "can EXECUTE SECURITY DEFINER"), reasons)
        # positive control: once the grant is removed, the escalation reason is gone (PUBLIC still cannot execute).
        self._exec("REVOKE EXECUTE ON FUNCTION public._relp_sd() FROM relp_secdef")
        _, reasons2 = self._readiness_as("relp_secdef")
        self.assertFalse(self._has(reasons2, "can EXECUTE SECURITY DEFINER"), reasons2)
        self.assertFalse(self._has(reasons2, "PUBLIC can EXECUTE a SECURITY DEFINER"), reasons2)

    def _exec(self, sql):
        with connection.cursor() as cur:
            cur.execute(sql)

    # --- membership / assume checks (require the privileged roles to exist so roles_ok is True) ----------
    def test_refuses_app_role_that_can_assume_a_privileged_role(self):
        for rn in self._PRIV:
            self._mkrole(rn, "NOLOGIN")
        self._mkrole("relp_member", "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS")
        with connection.cursor() as cur:
            cur.execute("GRANT guvfx_erasure TO relp_member")
        ready, reasons = self._readiness_as("relp_member")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "able to assume 'guvfx_erasure'"), reasons)
        # positive control: a clean role (not granted) raises NO assume reason for guvfx_erasure
        self._mkrole("relp_nomember", "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS")
        _, reasons2 = self._readiness_as("relp_nomember")
        self.assertFalse(self._has(reasons2, "able to assume 'guvfx_erasure'"), reasons2)

    def test_refuses_app_role_member_of_migrate_role(self):
        for rn in self._PRIV:
            self._mkrole(rn, "NOLOGIN")
        self._mkrole("relp_mig", "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS")
        with connection.cursor() as cur:
            cur.execute("GRANT guvfx_migrate TO relp_mig")
        ready, reasons = self._readiness_as("relp_mig")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "able to assume 'guvfx_migrate'"), reasons)

    def test_refuses_app_role_that_can_assume_an_elevated_role_transitively(self):
        # C3: the app role's OWN attributes are clean, but it is a member of some OTHER role that carries an
        # elevated attribute (here a SUPERUSER role) -> it could SET ROLE and escalate -> must be NOT READY.
        self._mkrole("relp_elev", "NOLOGIN SUPERUSER")
        self._mkrole("relp_trans", "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS")
        with connection.cursor() as cur:
            cur.execute("GRANT relp_elev TO relp_trans")
        ready, reasons = self._readiness_as("relp_trans")
        self.assertFalse(ready)
        self.assertTrue(self._has(reasons, "can assume elevated role"), reasons)
        # positive control: without the grant, no transitive-escalation reason
        self._mkrole("relp_trans_clean", "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS")
        _, reasons2 = self._readiness_as("relp_trans_clean")
        self.assertFalse(self._has(reasons2, "can assume elevated role"), reasons2)
