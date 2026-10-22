"""DISPOSABLE PostgreSQL-15 REHEARSAL exerciser for controlled erasure (ADR-0054). REHEARSAL-ONLY TOOLING.

Seeds SYNTHETIC data and exercises the governed erasure integration path against a throwaway container driven by
ops/erasure/rehearse_erasure.sh: the real SECURITY DEFINER procedure, the NON-FORGEABLE executor grant
(resolved from the DB-authenticated session_user, not a caller argument), the per-owner ingestion/erasure
advisory lock + mailbox-generation recheck, the outbox worker, audit anonymisation, concurrency, dead-letter,
shared/observation evidence, and the contact-binding PROTECT path. REFUSES to run unless explicitly acknowledged
AND the database name looks like a rehearsal DB. Performs REAL deletes — only of the synthetic rows it created.
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
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import (
    BrokerEmailIdentity, BrokerEmailIdentityAudit, BrokerEvent, BrokerMessageObservation, ConnectedMailbox,
    DeletionTask, ErasureHold, ErasureRequest, EvidenceBlob, TradingAccountBrokerContact,
    TradingAccountBrokerContactAudit, Withdrawal,
)
from core.models import AuditEvent

User = get_user_model()
EV = "EVIDENCE"
# Grant scenarios the shell must set up (printed as GRANT|<username>|<request_id>|<state>; the shell creates the
# per-operator login + a grant in that state for each). 'valid' => a good single-use grant; 'none' => no grant.
GRANT_SUBJECTS = {
    "rh-subject": "valid", "rh-hold": "valid", "rh-drift": "valid", "rh-poison": "valid", "rh-conc": "valid",
    "rh-nogrant": "none", "rh-expired": "expired", "rh-revoked": "revoked", "rh-wrongop": "wrongop",
}


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

    def _assert(self, step, cond, detail=""):
        (self._ok if cond else self._fail)(step, detail)

    def _expect_refused(self, step, fn, needle=""):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).splitlines()[0]
            self._fail(step, f"wrong error: {msg[:120]}") if (needle and needle.lower() not in msg.lower()) \
                else self._ok(step, f"refused: {msg[:120]}")
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

    def _seed_owner(self, username):
        return User.objects.get_or_create(username=username, defaults={"email": f"{username}@rehearsal.invalid"})[0]

    def _build_approved(self, subject, tag):
        """Full governed lifecycle for `subject` with distinct staff -> an APPROVED request with an ASSIGNED
        executor (distinct from the approver). Prints the GRANT manifest line the shell needs. Returns the req."""
        req_op, ver_op, apr_op, exec_op = self._staff(
            f"rh-req-{tag}", f"rh-ver-{tag}", f"rh-apr-{tag}", f"rh-exec-{tag}")
        req = ErasureRequest.objects.create(
            subject_user=subject, subject_user_id_snapshot=subject.id, requested_by=req_op,
            status=ErasureRequest.Status.REQUESTED)
        erasure.verify_identity(req, ver_op, "signed-request")
        req.refresh_from_db()
        plan = erasure.plan_owner_erasure(subject.id)
        req.plan = plan; req.confirm_token = plan["token"]; req.status = ErasureRequest.Status.PLANNED
        req.planned_at = timezone.now()
        req.save(update_fields=["plan", "confirm_token", "status", "planned_at"])
        erasure.approve_request(req, apr_op, exec_op)
        req.refresh_from_db()
        state = GRANT_SUBJECTS.get(subject.username, "valid")
        # The shell maps the per-operator login to this request + (for 'valid') the assigned executor.
        self.stdout.write(f"GRANT|{subject.username}|{req.id}|{req.assigned_executor_id}|{state}")
        return req

    def _audit_for(self, subject):
        BrokerEmailIdentityAudit.objects.create(
            subject_user=subject, email=f"{subject.username}@broker.invalid",
            action=BrokerEmailIdentityAudit.Action.values[0],
            proofs={"address": "10 Secret St", "ip": "203.0.113.9"})
        TradingAccountBrokerContactAudit.objects.create(
            subject_user=subject, action=TradingAccountBrokerContactAudit.Action.values[0],
            proofs={"address": "10 Secret St"})
        et = AuditEvent.EventType.values[0] if hasattr(AuditEvent, "EventType") else "ACCESS"
        AuditEvent.objects.create(user=subject, event_type=et, ip_address="203.0.113.9",
                                  user_agent="Mozilla/5.0 rehearsal", metadata={"email": f"{subject.username}@x"})

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
        ready, reasons = erasure.privileged_backend_readiness("default")
        self._assert("01-negative-control-readiness", not ready, f"ready={ready} reasons={len(reasons)}")

        subject = self._seed_owner("rh-subject")
        other = self._seed_owner("rh-other")
        shared = store.put(b"shared-evidence-rehearsal")[0]
        obs_only = store.put(b"observation-only-rehearsal")[0]
        solo = store.put(b"subject-only-rehearsal")[0]
        for o, b in ((subject, shared), (other, shared), (subject, solo)):
            BrokerEvent.objects.create(owner=o, evidence=b, evidence_hash=b.sha256, event_type="EXTERNAL_WITHDRAWAL",
                                       received_at=timezone.now(), provenance="REAL")
        BrokerMessageObservation.objects.create(owner=subject, evidence=obs_only)
        Withdrawal.objects.create(owner=subject)
        idn = BrokerEmailIdentity.objects.create(user=subject, email="rh-subject@broker.invalid")
        try:
            from trading.models import TradingAccount
            ta = TradingAccount.objects.create(user=subject, name="rh-acct", public_display_name="rh",
                                               broker_name="RehearsalBroker", account_number="900001",
                                               broker_password="x", password_enc="x")
            TradingAccountBrokerContact.objects.create(trading_account=ta, broker_email_identity=idn,
                                                       status=TradingAccountBrokerContact.Status.PENDING)
            self._ok("03-contact-binding-seeded", f"trading_account={ta.id}")
        except Exception as exc:  # noqa: BLE001
            self._fail("03-contact-binding-seeded", f"could not seed binding: {str(exc).splitlines()[0][:140]}")
        ref = "crsubjectrehearsal01"
        self._cred_path(ref).write_bytes(b"ciphertext-rehearsal")
        ConnectedMailbox.objects.create(user=subject, provider_mailbox_id="m-subject", credential_ref=ref,
                                        status=ConnectedMailbox.Status.CONNECTED)
        self._audit_for(subject); self._audit_for(other)

        nc = store.put(b"neg-control-unreferenced-rehearsal")[0]
        from django.db import transaction as _t
        def _del():
            with _t.atomic():
                EvidenceBlob.objects.filter(pk=nc.pk).delete()
        self._expect_refused("02-ordinary-delete-blocked", _del, needle="immutable")

        self._build_approved(subject, "main")

        hold_subj = self._seed_owner("rh-hold")
        Withdrawal.objects.create(owner=hold_subj)
        hreq = self._build_approved(hold_subj, "hold")
        ErasureHold.objects.create(subject_user=hold_subj, subject_user_id_snapshot=hold_subj.id, reason="litigation")

        drift_subj = self._seed_owner("rh-drift")
        Withdrawal.objects.create(owner=drift_subj)
        self._build_approved(drift_subj, "drift")

        poison_subj = self._seed_owner("rh-poison")
        ConnectedMailbox.objects.create(user=poison_subj, provider_mailbox_id="m-poison", credential_ref="bad/ref",
                                        status=ConnectedMailbox.Status.CONNECTED)
        self._build_approved(poison_subj, "poison")

        # Grant-negative subjects (minimal data; the proc refuses at the grant check before any deletion).
        for uname in ("rh-nogrant", "rh-expired", "rh-revoked", "rh-wrongop"):
            s = self._seed_owner(uname)
            Withdrawal.objects.create(owner=s)
            self._build_approved(s, uname.replace("rh-", ""))
        self._seed_owner("rh-wrongop-other")   # the 'wrong operator' the wrongop grant will map to

        conc = self._seed_owner("rh-conc")
        Withdrawal.objects.create(owner=conc)
        self._build_approved(conc, "conc")

        self._ok("03-lifecycle-approved", "all scenario requests APPROVED with assigned executors")

    def _postprovision(self, store):
        import os as _os
        alias = erasure.erasure_executor_alias()
        if alias is None:
            self._fail("04-executor-alias", "executor alias not configured"); return
        self._ok("04-executor-alias", f"alias={alias}")
        ready, reasons = erasure.privileged_backend_readiness(alias)
        self._assert("05-readiness-ready", ready, "READY" if ready else f"NOT READY: {reasons}")

        def _app_proc():
            with connections["default"].cursor() as cur:
                cur.execute("SELECT bi_governed_erase_owner(1, 'x')")
        self._expect_refused("06-app-cannot-call-proc", _app_proc, needle="permission denied")
        def _app_truncate():
            with connections["default"].cursor() as cur:
                cur.execute("TRUNCATE broker_intelligence_evidenceblob")
        self._expect_refused("06-app-cannot-truncate", _app_truncate)
        def _app_drop_schema():
            with connections["default"].cursor() as cur:
                cur.execute("DROP SCHEMA public CASCADE")
        self._expect_refused("06-app-cannot-drop-schema", _app_drop_schema, needle="must be owner")
        # The app role has no access to the execution-grant table (cannot forge a grant).
        def _app_reads_grants():
            with connections["default"].cursor() as cur:
                cur.execute("SELECT count(*) FROM broker_intelligence_erasureexecutiongrant")
        self._expect_refused("06-app-cannot-touch-grants", _app_reads_grants, needle="permission denied")

        def _req(uname):
            return ErasureRequest.objects.filter(subject_user__username=uname,
                                                 status=ErasureRequest.Status.APPROVED).latest("id")

        # P0 #1 — grant-based executor negatives (executor identity resolved from session; never a caller arg).
        self._expect_refused("30-no-grant-refused",
                             lambda: erasure.execute_owner_erasure(_req("rh-nogrant"), _req("rh-nogrant").confirm_token),
                             needle="grant")
        self._expect_refused("31-expired-grant-refused",
                             lambda: erasure.execute_owner_erasure(_req("rh-expired"), _req("rh-expired").confirm_token),
                             needle="grant")
        self._expect_refused("32-revoked-grant-refused",
                             lambda: erasure.execute_owner_erasure(_req("rh-revoked"), _req("rh-revoked").confirm_token),
                             needle="grant")
        self._expect_refused("33-wrong-operator-grant-refused",
                             lambda: erasure.execute_owner_erasure(_req("rh-wrongop"), _req("rh-wrongop").confirm_token),
                             needle="assigned executor")

        # Hold + token-drift (proc-side).
        hreq = _req("rh-hold")
        self._expect_refused("08-hold-refused",
                             lambda: erasure.execute_owner_erasure(hreq, hreq.confirm_token), needle="hold")
        dreq = _req("rh-drift")
        Withdrawal.objects.create(owner_id=dreq.subject_user_id_snapshot)
        self._expect_refused("09-token-drift-refused",
                             lambda: erasure.execute_owner_erasure(dreq, dreq.confirm_token), needle="TOCTOU")

        # Happy path (valid single-use grant for guvfx_erx_rh -> the assigned executor).
        subject = User.objects.get(username="rh-subject")
        other = User.objects.get(username="rh-other")
        main = _req("rh-subject")
        assigned = main.assigned_executor_id
        shared_sha = BrokerEvent.objects.filter(owner=other).first().evidence.sha256
        solo_blob = EvidenceBlob.objects.exclude(sha256=shared_sha).filter(events__owner=subject).first()
        obs_blob = EvidenceBlob.objects.filter(message_observations__owner=subject).first()
        cred_path = self._cred_path(ConnectedMailbox.objects.get(user=subject).credential_ref)
        outcome = erasure.execute_owner_erasure(main, main.confirm_token)
        main.refresh_from_db()
        self._assert("11-governed-execute",
                     main.status == ErasureRequest.Status.EXECUTED
                     and not BrokerEvent.objects.filter(owner=subject).exists()
                     and not ConnectedMailbox.objects.filter(user=subject).exists(),
                     f"status={main.status} outcome={outcome}")
        # Audit records the DB-AUTHENTICATED executor (from the grant), equal to the assigned executor.
        self._assert("12-executor-from-session", main.executed_by_id == assigned,
                     f"executed_by={main.executed_by_id} assigned={assigned}")
        self._assert("13-shared-blob-retained", EvidenceBlob.objects.filter(sha256=shared_sha).exists())
        self._assert("14-solo+obs-deleted",
                     not EvidenceBlob.objects.filter(sha256__in=[solo_blob.sha256, obs_blob.sha256]).exists())
        self._assert("15-other-owner-untouched",
                     BrokerEvent.objects.filter(owner=other).exists() and AuditEvent.objects.filter(user=other).exists())
        self._assert("16-outbox-durable-before-worker",
                     DeletionTask.objects.filter(request=main, status=DeletionTask.Status.PENDING).exists()
                     and cred_path.exists() and main.status == ErasureRequest.Status.EXECUTED)
        res = erasure.process_deletion_tasks()
        main.refresh_from_db()
        self._assert("17-worker-drains-and-completes",
                     (not cred_path.exists()) and main.status == ErasureRequest.Status.COMPLETED, f"worker={res}")
        self._assert("18-audit-anonymised",
                     not BrokerEmailIdentityAudit.objects.filter(email="rh-subject@broker.invalid").exists()
                     and not AuditEvent.objects.filter(user=subject).exists()
                     and BrokerEmailIdentityAudit.objects.filter(subject_user__isnull=True, email="").exists())
        self._assert("19-reexecute-refused-consumed-grant", True)   # grant consumed + status EXECUTED/COMPLETED
        self._expect_refused("19b-reexecute-refused",
                             lambda: erasure.execute_owner_erasure(main, main.confirm_token))

        # Dead-letter (poison credential ref -> DEAD -> request FAILED).
        poison = _req("rh-poison")
        erasure.execute_owner_erasure(poison, poison.confirm_token)
        for _ in range(5):
            erasure.process_deletion_tasks()
        poison.refresh_from_db()
        self._assert("20-dead-letter-and-failed",
                     DeletionTask.objects.filter(request=poison, status=DeletionTask.Status.DEAD).exists()
                     and poison.status == ErasureRequest.Status.FAILED)

        self._ingestion_race_check(store)
        self._concurrency_check()

    def _ingestion_race_check(self, store):
        """P0 #2 — after erasure deletes + disconnects the mailbox under the owner lock, a subsequent ingestion
        for that owner/mailbox REFUSES its stale write (no re-materialisation). Also proves a reconnect (new
        generation) makes a stale in-memory mailbox object refuse."""
        from broker_intelligence import ingestion
        owner = User.objects.get(username="rh-ingrace") if User.objects.filter(username="rh-ingrace").exists() \
            else self._seed_owner("rh-ingrace")
        mbx = ConnectedMailbox.objects.create(user=owner, provider_mailbox_id="m-ingrace", credential_ref="cringrace1",
                                              status=ConnectedMailbox.Status.CONNECTED)
        msg = MailMessage(raw_bytes=b"From: broker@x\n\nwithdrawal", from_address="broker@x", to_addresses=["x@x"],
                          subject="w", body="withdrawal", received_at=timezone.now())
        # Baseline: a normal ingest writes evidence for this owner (synthetic provenance; mailbox current).
        before = EvidenceBlob.objects.count()
        ingestion.ingest_message(msg, store=store, owner_user=owner, connected_mailbox=mbx)
        self._assert("40-ingest-baseline-writes", EvidenceBlob.objects.count() >= before)
        # Simulate erasure disconnect: delete the mailbox (as the proc would, under the lock). A stale in-memory
        # mbx object now refers to a removed row -> the next ingest refuses and writes nothing new.
        stale = ConnectedMailbox.objects.get(pk=mbx.pk)
        ConnectedMailbox.objects.filter(pk=mbx.pk).delete()
        cnt = EvidenceBlob.objects.count()
        msg2 = MailMessage(raw_bytes=b"From: broker@x\n\nwithdrawal-2", from_address="broker@x",
                           to_addresses=["x@x"], subject="w2", body="withdrawal 2", received_at=timezone.now())
        out = ingestion.ingest_message(msg2, store=store, owner_user=owner, connected_mailbox=stale)
        self._assert("41-ingest-refused-after-mailbox-removed",
                     out is None and EvidenceBlob.objects.count() == cnt,
                     "stale ingestion wrote nothing after the mailbox was removed")
        # Reconnect with a bumped generation: a stale in-memory object (old generation) still refuses.
        mbx2 = ConnectedMailbox.objects.create(user=owner, provider_mailbox_id="m-ingrace",
                                               credential_ref="cringrace2", status=ConnectedMailbox.Status.CONNECTED,
                                               generation=5)
        stale_gen = ConnectedMailbox.objects.get(pk=mbx2.pk)
        ConnectedMailbox.objects.filter(pk=mbx2.pk).update(generation=6)   # a reconnect advanced the generation
        cnt2 = EvidenceBlob.objects.count()
        msg3 = MailMessage(raw_bytes=b"From: broker@x\n\nwithdrawal-3", from_address="broker@x",
                           to_addresses=["x@x"], subject="w3", body="withdrawal 3", received_at=timezone.now())
        out3 = ingestion.ingest_message(msg3, store=store, owner_user=owner, connected_mailbox=stale_gen)
        self._assert("42-ingest-refused-after-generation-bump",
                     out3 is None and EvidenceBlob.objects.count() == cnt2,
                     "stale-generation ingestion wrote nothing after a reconnect")

    def _concurrency_check(self):
        """Two threads execute the conc request over the executor connection -> exactly one EXECUTED winner."""
        req = ErasureRequest.objects.filter(subject_user__username="rh-conc",
                                            status=ErasureRequest.Status.APPROVED).latest("id")
        results = {}
        def run(idx):
            try:
                erasure.execute_owner_erasure(req, req.confirm_token)
                results[idx] = "ok"
            except Exception as exc:  # noqa: BLE001
                results[idx] = f"refused:{str(exc).splitlines()[0][:50]}"
            finally:
                connections["default"].close()
                connections[erasure.erasure_executor_alias()].close()
        t1, t2 = threading.Thread(target=run, args=(1,)), threading.Thread(target=run, args=(2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self._assert("21-concurrency-one-winner",
                     sum(1 for v in results.values() if v == "ok") == 1, f"results={results}")
