"""DISPOSABLE PostgreSQL-15 REHEARSAL exerciser for controlled erasure (ADR-0054). REHEARSAL-ONLY TOOLING.

This command seeds SYNTHETIC data and exercises the governed erasure integration path (the real SECURITY DEFINER
procedure, the executor connection, the outbox worker, audit anonymisation, concurrency, crash-recovery,
dead-letter, shared/observation evidence). It is driven by ops/erasure/rehearse_erasure.sh against a throwaway
container. It REFUSES to run unless explicitly acknowledged AND the database name looks like a rehearsal DB, so
it can never touch production. It performs REAL deletes — only ever of the synthetic rows it created.

It is never imported by the app and is DARK with respect to production (no prod connectivity, no real data).
"""
from __future__ import annotations

import os
import threading

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.utils import timezone

from broker_intelligence import erasure
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.models import (
    BrokerEmailIdentity, BrokerEmailIdentityAudit, BrokerEvent, BrokerMessageObservation, ConnectedMailbox,
    DeletionTask, ErasureHold, ErasureRequest, EvidenceBlob, TradingAccountBrokerContact,
    TradingAccountBrokerContactAudit, Withdrawal,
)
from core.models import AuditEvent

User = get_user_model()
EV = "EVIDENCE"


class Command(BaseCommand):
    help = "Exercise the controlled-erasure integration path against a DISPOSABLE PG15 rehearsal DB (synthetic)."

    def add_arguments(self, parser):
        parser.add_argument("--phase", choices=["preprovision", "postprovision"], required=True)
        parser.add_argument("--i-understand-this-is-a-disposable-rehearsal", action="store_true", dest="ack")

    # ---- helpers ---------------------------------------------------------
    def _ok(self, step, detail=""):
        self.stdout.write(f"{EV}|{step}|PASS|{detail}")

    def _fail(self, step, detail=""):
        self.stdout.write(f"{EV}|{step}|FAIL|{detail}")
        self._failures += 1

    def _expect_raises(self, step, fn, needle=""):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).splitlines()[0]
            if needle and needle.lower() not in msg.lower():
                self._fail(step, f"raised but wrong error: {msg[:120]}")
            else:
                self._ok(step, f"refused: {msg[:120]}")
            return
        self._fail(step, "did NOT raise (expected a refusal)")

    def _guard(self, opts):
        if not opts["ack"]:
            raise CommandError("refusing: pass --i-understand-this-is-a-disposable-rehearsal")
        name = connections["default"].settings_dict["NAME"]
        if "rehears" not in name.lower() and "disposable" not in name.lower():
            raise CommandError(f"refusing: database name {name!r} does not look like a disposable rehearsal DB")

    def _cred_path(self, ref):
        from broker_intelligence import credential_store
        return credential_store._path_for(ref)

    def _staff(self, *names):
        return [User.objects.get_or_create(username=n, defaults={"email": f"{n}@rehearsal.invalid"})[0]
                for n in names]

    def _build_approved(self, subject, tag):
        """Full governed lifecycle for `subject` with distinct staff -> an APPROVED request. Returns the request."""
        req_op, ver_op, apr_op = self._staff(f"rh-req-{tag}", f"rh-ver-{tag}", f"rh-apr-{tag}")
        req = ErasureRequest.objects.create(
            subject_user=subject, subject_user_id_snapshot=subject.id, requested_by=req_op,
            status=ErasureRequest.Status.REQUESTED)
        erasure.verify_identity(req, ver_op, "signed-request")
        req.refresh_from_db()                         # verify_identity updated the row; avoid clobbering it
        plan = erasure.plan_owner_erasure(subject.id)
        req.plan = plan; req.confirm_token = plan["token"]; req.status = ErasureRequest.Status.PLANNED
        req.planned_at = timezone.now()
        req.save(update_fields=["plan", "confirm_token", "status", "planned_at"])
        erasure.approve_request(req, apr_op)
        req.refresh_from_db()
        return req

    def _seed_owner(self, username):
        return User.objects.get_or_create(username=username, defaults={"email": f"{username}@rehearsal.invalid"})[0]

    def _audit_for(self, subject):
        a1 = BrokerEmailIdentityAudit.Action.values[0]
        a2 = TradingAccountBrokerContactAudit.Action.values[0]
        et = AuditEvent.EventType.values[0] if hasattr(AuditEvent, "EventType") else "ACCESS"
        BrokerEmailIdentityAudit.objects.create(
            subject_user=subject, email=f"{subject.username}@broker.invalid", action=a1,
            proofs={"address": "10 Secret St", "ip": "203.0.113.9"})
        TradingAccountBrokerContactAudit.objects.create(
            subject_user=subject, action=a2, proofs={"address": "10 Secret St"})
        AuditEvent.objects.create(
            user=subject, event_type=et, ip_address="203.0.113.9", user_agent="Mozilla/5.0 rehearsal",
            metadata={"email": f"{subject.username}@broker.invalid"})

    # ---- phases ----------------------------------------------------------
    def handle(self, *args, **opts):
        self._failures = 0
        self._guard(opts)
        store = EvidenceStore()
        if opts["phase"] == "preprovision":
            self._preprovision(store)
        else:
            self._postprovision(store)
        if self._failures:
            raise CommandError(f"REHEARSAL phase '{opts['phase']}' had {self._failures} failure(s)")
        self.stdout.write(f"{EV}|phase-{opts['phase']}|PASS|all assertions held")

    def _preprovision(self, store):
        # Negative control: readiness must be NOT READY on an unprovisioned DB.
        ready, reasons = erasure.privileged_backend_readiness("default")
        (self._ok if not ready else self._fail)("01-negative-control-readiness",
            f"ready={ready} reasons={len(reasons)}")

        subject = self._seed_owner("rh-subject")
        other = self._seed_owner("rh-other")
        # shared blob (subject + other), observation-only blob (subject), subject-only event blob.
        shared = store.put(b"shared-evidence-rehearsal")[0]
        obs_only = store.put(b"observation-only-rehearsal")[0]
        solo = store.put(b"subject-only-rehearsal")[0]
        BrokerEvent.objects.create(owner=subject, evidence=shared, evidence_hash=shared.sha256,
                                   event_type="EXTERNAL_WITHDRAWAL", received_at=timezone.now(), provenance="REAL")
        BrokerEvent.objects.create(owner=other, evidence=shared, evidence_hash=shared.sha256,
                                   event_type="EXTERNAL_WITHDRAWAL", received_at=timezone.now(), provenance="REAL")
        BrokerEvent.objects.create(owner=subject, evidence=solo, evidence_hash=solo.sha256,
                                   event_type="EXTERNAL_WITHDRAWAL", received_at=timezone.now(), provenance="REAL")
        BrokerMessageObservation.objects.create(owner=subject, evidence=obs_only)
        Withdrawal.objects.create(owner=subject)
        idn = BrokerEmailIdentity.objects.create(user=subject, email="rh-subject@broker.invalid")
        # A broker-contact BINDING for the subject (its PROTECT FK blocks identity deletion unless erasure
        # handles it). status PENDING so the VERIFIED-only consistency trigger is a no-op. ORM create applies the
        # model's field defaults; wrapped so any TradingAccount side-effect surfaces as evidence, not a crash.
        try:
            from trading.models import TradingAccount
            ta = TradingAccount.objects.create(
                user=subject, name="rh-acct", public_display_name="rh", broker_name="RehearsalBroker",
                account_number="900001", broker_password="x", password_enc="x")
            TradingAccountBrokerContact.objects.create(
                trading_account=ta, broker_email_identity=idn,
                status=TradingAccountBrokerContact.Status.PENDING)
            self._ok("03-contact-binding-seeded", f"trading_account={ta.id}")
        except Exception as exc:  # noqa: BLE001
            self._fail("03-contact-binding-seeded", f"could not seed binding: {str(exc).splitlines()[0][:140]}")
        # a real credential file for the subject's mailbox
        ref = "crsubjectrehearsal01"
        self._cred_path(ref).write_bytes(b"ciphertext-rehearsal")
        ConnectedMailbox.objects.create(user=subject, provider_mailbox_id="m-subject", credential_ref=ref)
        self._audit_for(subject)
        self._audit_for(other)

        # Negative control: ordinary delete of immutable evidence is blocked by the role-enforced trigger.
        # Use an UNREFERENCED blob so the DELETE actually reaches the DB (a referenced one would trip Django's
        # PROTECT collector first and never exercise the trigger).
        nc = store.put(b"neg-control-unreferenced-rehearsal")[0]
        from django.db import transaction as _t
        def _del():
            with _t.atomic():
                EvidenceBlob.objects.filter(pk=nc.pk).delete()
        self._expect_raises("02-ordinary-delete-blocked", _del, needle="immutable")

        # Build the main APPROVED governed request + extra subjects for hold/drift/poison.
        main = self._build_approved(subject, "main")
        self._ok("03-lifecycle-approved", f"request={main.id} token={main.confirm_token[:12]}..")

        hold_subj = self._seed_owner("rh-hold")
        Withdrawal.objects.create(owner=hold_subj)
        self._audit_for(hold_subj)
        hreq = self._build_approved(hold_subj, "hold")
        ErasureHold.objects.create(subject_user=hold_subj, subject_user_id_snapshot=hold_subj.id, reason="litigation")
        self._ok("03-hold-request", f"request={hreq.id}")

        drift_subj = self._seed_owner("rh-drift")
        Withdrawal.objects.create(owner=drift_subj)
        dreq = self._build_approved(drift_subj, "drift")
        self._ok("03-drift-request", f"request={dreq.id}")

        poison_subj = self._seed_owner("rh-poison")
        ConnectedMailbox.objects.create(user=poison_subj, provider_mailbox_id="m-poison", credential_ref="bad/ref")
        preq = self._build_approved(poison_subj, "poison")
        self._ok("03-poison-request", f"request={preq.id}")

    def _postprovision(self, store):
        alias = erasure.erasure_executor_alias()
        if alias is None:
            self._fail("04-executor-alias", "executor alias not configured (set ERASURE_EXECUTOR_DB_*)"); return
        self._ok("04-executor-alias", f"alias={alias}")

        ready, reasons = erasure.privileged_backend_readiness(alias)
        (self._ok if ready else self._fail)("05-readiness-ready",
            "READY" if ready else f"NOT READY: {reasons}")

        # DB boundary: the ordinary app connection must NOT be able to call the procedure.
        def _app_calls_proc():
            with connections["default"].cursor() as cur:
                cur.execute("SELECT bi_governed_erase_owner(1, 1, 'x')")
        self._expect_raises("06-app-cannot-call-proc", _app_calls_proc, needle="permission denied")

        # DB boundary: the app role must not be able to TRUNCATE or DROP-trigger the evidence table post-provision.
        def _app_truncates():
            with connections["default"].cursor() as cur:
                cur.execute("TRUNCATE broker_intelligence_evidenceblob")
        self._expect_raises("06-app-cannot-truncate", _app_truncates)
        def _app_drops_trigger():
            with connections["default"].cursor() as cur:
                cur.execute("DROP TRIGGER bi_evidenceblob_no_delete ON broker_intelligence_evidenceblob")
        self._expect_raises("06-app-cannot-drop-trigger", _app_drops_trigger)

        # HIGH (round 4): the app role must not be able to DROP SCHEMA public CASCADE (owner-level destruction).
        def _app_drops_schema():
            with connections["default"].cursor() as cur:
                cur.execute("DROP SCHEMA public CASCADE")
        self._expect_raises("06-app-cannot-drop-schema", _app_drops_schema)

        subject = User.objects.get(username="rh-subject")
        other = User.objects.get(username="rh-other")
        main = ErasureRequest.objects.filter(subject_user_id_snapshot=subject.id,
                                             status=ErasureRequest.Status.APPROVED).latest("id")
        exec_op = self._staff("rh-executor-main")[0]

        # snapshot before
        shared_sha = (BrokerEvent.objects.filter(owner=other).first().evidence.sha256)
        solo_blob = EvidenceBlob.objects.exclude(sha256=shared_sha).filter(
            events__owner=subject).first()
        obs_blob = EvidenceBlob.objects.filter(message_observations__owner=subject).first()
        cred_ref = ConnectedMailbox.objects.get(user=subject).credential_ref
        cred_path = self._cred_path(cred_ref)
        self._ok("07-pre-execute-state",
                 f"shared={shared_sha[:8]} solo={solo_blob.sha256[:8]} obs={obs_blob.sha256[:8]} cred_file={cred_path.exists()}")

        # Hold refusal (over the real proc).
        hold_req = ErasureRequest.objects.filter(subject_user__username="rh-hold",
                                                 status=ErasureRequest.Status.APPROVED).latest("id")
        self._expect_raises("08-hold-refused",
            lambda: erasure.execute_owner_erasure(hold_req, hold_req.confirm_token, executor=exec_op),
            needle="hold")

        # Token drift refusal: mutate the drift subject's state after the plan, then execute with the stale token.
        drift_req = ErasureRequest.objects.filter(subject_user__username="rh-drift",
                                                  status=ErasureRequest.Status.APPROVED).latest("id")
        Withdrawal.objects.create(owner_id=drift_req.subject_user_id_snapshot)   # state changes -> plan stale
        self._expect_raises("09-token-drift-refused",
            lambda: erasure.execute_owner_erasure(drift_req, drift_req.confirm_token, executor=exec_op),
            needle="TOCTOU")

        # executor == approver refused.
        self._expect_raises("10-executor-equals-approver",
            lambda: erasure.execute_owner_erasure(main, main.confirm_token,
                                                  executor=User.objects.get(pk=main.approved_by_id)),
            needle="approver")

        # REAL governed execution (synthetic subject). Rows deleted; files NOT yet (outbox).
        outcome = erasure.execute_owner_erasure(main, main.confirm_token, executor=exec_op)
        main.refresh_from_db()
        ok = (main.status == ErasureRequest.Status.EXECUTED
              and not Withdrawal.objects.filter(owner=subject).exists()
              and not BrokerEvent.objects.filter(owner=subject).exists()
              and not ConnectedMailbox.objects.filter(user=subject).exists()
              and not BrokerEmailIdentity.objects.filter(user=subject).exists())
        (self._ok if ok else self._fail)("11-governed-execute", f"status={main.status} outcome={outcome}")

        # Shared blob retained; subject-only + observation-only blobs deleted (rows).
        self._assert("12-shared-blob-retained", EvidenceBlob.objects.filter(sha256=shared_sha).exists())
        self._assert("13-solo-blob-deleted", not EvidenceBlob.objects.filter(sha256=solo_blob.sha256).exists())
        self._assert("14-observation-blob-deleted", not EvidenceBlob.objects.filter(sha256=obs_blob.sha256).exists())
        self._assert("15-other-owner-untouched",
                     BrokerEvent.objects.filter(owner=other).exists() and
                     AuditEvent.objects.filter(user=other).exists())

        # Crash-recovery: files still present + tasks PENDING + request EXECUTED (worker not yet run).
        pending = DeletionTask.objects.filter(request=main, status=DeletionTask.Status.PENDING).count()
        self._assert("16-outbox-durable-before-worker",
                     pending >= 1 and cred_path.exists() and main.status == ErasureRequest.Status.EXECUTED)

        # Drain the outbox worker: files removed, request -> COMPLETED.
        res = erasure.process_deletion_tasks()
        main.refresh_from_db()
        self._assert("17-worker-drains-and-completes",
                     (not cred_path.exists()) and main.status == ErasureRequest.Status.COMPLETED,
                     f"worker={res} status={main.status} cred_file={cred_path.exists()}")

        # Audit PII anonymised (rows kept, identifiers gone) for the subject; other owner unchanged.
        bia = BrokerEmailIdentityAudit.objects.filter(email="rh-subject@broker.invalid").exists()
        ae = AuditEvent.objects.filter(user=subject).exists()
        anon_rows = BrokerEmailIdentityAudit.objects.filter(subject_user__isnull=True, email="").exists()
        self._assert("18-audit-anonymised",
                     (not bia) and (not ae) and anon_rows,
                     f"subject_email_rows={bia} subject_ae={ae} anon_present={anon_rows}")

        # Re-execute the same request refused (idempotency / no double-run).
        self._expect_raises("19-reexecute-refused",
            lambda: erasure.execute_owner_erasure(main, main.confirm_token, executor=exec_op))

        # Dead-letter: poison credential_ref -> task retried then DEAD -> request FAILED.
        poison = ErasureRequest.objects.filter(subject_user__username="rh-poison",
                                               status=ErasureRequest.Status.APPROVED).latest("id")
        erasure.execute_owner_erasure(poison, poison.confirm_token, executor=self._staff("rh-executor-poison")[0])
        for _ in range(5):
            erasure.process_deletion_tasks()
        poison.refresh_from_db()
        dead = DeletionTask.objects.filter(request=poison, status=DeletionTask.Status.DEAD).exists()
        self._assert("20-dead-letter-and-failed",
                     dead and poison.status == ErasureRequest.Status.FAILED,
                     f"dead={dead} status={poison.status}")

        # Concurrency: two threads execute the drift request (re-approved w/ fresh plan) -> exactly one succeeds.
        self._concurrency_check(exec_op)

    def _assert(self, step, cond, detail=""):
        (self._ok if cond else self._fail)(step, detail)

    def _concurrency_check(self, exec_op):
        subj = self._seed_owner("rh-conc")
        Withdrawal.objects.create(owner=subj)
        req = self._build_approved(subj, "conc")
        results = {}
        def run(idx):
            try:
                erasure.execute_owner_erasure(req, req.confirm_token, executor=exec_op)
                results[idx] = "ok"
            except Exception as exc:  # noqa: BLE001
                results[idx] = f"refused:{str(exc).splitlines()[0][:60]}"
            finally:
                connections["default"].close()
                connections[erasure.erasure_executor_alias()].close()
        t1, t2 = threading.Thread(target=run, args=(1,)), threading.Thread(target=run, args=(2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = sum(1 for v in results.values() if v == "ok")
        self._assert("21-concurrency-one-winner", oks == 1, f"results={results}")
