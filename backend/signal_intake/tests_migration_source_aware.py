"""
AMBER-1 — populated HISTORICAL migration certification for the source-aware identity
chain (0009 → 0010 → 0011), driven by Django's MigrationExecutor against real legacy
rows created in the pre-migration schema state (NOT the current app registry).

Proves: the evidence-based backfill (approval chat taken from the linked AcquiredMessage,
never inferred from mutable provider metadata; unresolved provenance left ''), the new
uniqueness constraints enforce on migrated data, and forward/rollback/reapply behave.
"""

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

APP = "signal_intake"
AT_0009 = [(APP, "0009_providercommand")]
AT_0011 = [(APP, "0011_acquired_message_chat_identity_refused")]


class HistoricalMigrationTests(TransactionTestCase):
    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Restore the shared test DB to the latest migration state for other tests.
        call_command("migrate", APP, verbosity=0)

    def test_evidence_based_backfill_and_constraint_enforcement(self):
        old = self._migrate(AT_0009)
        PP = old.get_model(APP, "ParserProfile")
        Prov = old.get_model(APP, "SignalProvider")
        Acq = old.get_model(APP, "AcquiredMessage")
        Appr = old.get_model(APP, "PendingSignalApproval")

        pp = PP.objects.create(slug="wayond_v1")
        prov = Prov.objects.create(slug="wayond", telegram_chat_id="1001",
                                   parser_profile=pp, status="ARMED")

        # (a) approval WITH persisted acquisition evidence → backfilled FROM the evidence.
        appr_ev = Appr.objects.create(source="wayond", message_id="5",
                                      provider=prov, status="APPROVED")
        Acq.objects.create(provider=prov, chat_id="1001", message_id="5",
                           outcome="INTAKEN", approval=appr_ev)
        # (b) approval with a PROVIDER but NO linked evidence → stays '' (provider metadata
        #     is NOT treated as historical proof — the AMBER-1 correction).
        Appr.objects.create(source="wayond", message_id="6", provider=prov, status="APPROVED")
        # (c) legacy file-intake approval, no provider, no evidence → stays ''.
        Appr.objects.create(source="WAYOND_TELEGRAM", message_id="L", status="QUARANTINED")
        # (d) a historical DROPPED_NOT_ARMED row (recorded while disabled) → R2 HIGH-A: the
        #     0011 backfill must mark it non_genuine=True so it cannot poison a re-armed genuine.
        Acq.objects.create(provider=prov, chat_id="1001", message_id="100",
                           outcome="DROPPED_NOT_ARMED")

        new = self._migrate(AT_0011)
        Appr2 = new.get_model(APP, "PendingSignalApproval")
        Acq2 = new.get_model(APP, "AcquiredMessage")

        self.assertEqual(Appr2.objects.get(message_id="5").chat_id, "1001")  # from evidence
        self.assertEqual(Appr2.objects.get(message_id="6").chat_id, "")      # provider NOT inferred
        self.assertEqual(Appr2.objects.get(message_id="L").chat_id, "")      # legacy, unresolved

        # New flag: an INTAKEN row is genuine; a historical DROPPED_NOT_ARMED row is not.
        self.assertFalse(Acq2.objects.get(message_id="5").non_genuine)
        self.assertTrue(Acq2.objects.get(message_id="100").non_genuine)   # R2 HIGH-A backfill

        # Constraint enforcement on migrated data: a duplicate GENUINE row is rejected,
        # but a REFUSAL row may share the tuple (partitioned by non_genuine).
        with self.assertRaises(IntegrityError), transaction.atomic():
            Acq2.objects.create(provider_id=prov.id, chat_id="1001", message_id="5",
                                outcome="INTAKEN", non_genuine=False)
        Acq2.objects.create(provider_id=prov.id, chat_id="1001", message_id="5",
                            outcome="QUARANTINED", non_genuine=True)  # coexists
        self.assertEqual(Acq2.objects.filter(message_id="5").count(), 2)

    def test_forward_rollback_reapply_preserves_populated_history(self):
        """R2 MEDIUM: rollback/reapply with REPRESENTATIVE persisted relationships. Proves
        forward preserves evidence + the approval↔acquired association + content and
        backfills deterministically; pre-cutover rollback preserves rows/associations/content
        (never rewrites original content); reapply succeeds."""
        old0 = self._migrate(AT_0009)
        PP = old0.get_model(APP, "ParserProfile")
        Prov = old0.get_model(APP, "SignalProvider")
        Acq = old0.get_model(APP, "AcquiredMessage")
        Appr = old0.get_model(APP, "PendingSignalApproval")
        pp = PP.objects.create(slug="wayond_v1")
        prov = Prov.objects.create(slug="wayond", telegram_chat_id="1001",
                                   parser_profile=pp, status="ARMED")
        appr = Appr.objects.create(source="wayond", message_id="7", provider=prov,
                                   status="APPROVED", symbol="XAUUSD",
                                   raw_payload={"raw_text": "orig"})
        acq = Acq.objects.create(provider=prov, chat_id="1001", message_id="7",
                                 outcome="INTAKEN", approval=appr, raw_payload={"text": "orig"})
        drop = Acq.objects.create(provider=prov, chat_id="1001", message_id="200",
                                  outcome="DROPPED_NOT_ARMED")
        appr_id, acq_id, drop_id = appr.id, acq.id, drop.id

        # Forward: evidence preserved + backfilled, association intact, content untouched.
        new = self._migrate(AT_0011)
        A = new.get_model(APP, "PendingSignalApproval")
        M = new.get_model(APP, "AcquiredMessage")
        self.assertEqual(A.objects.get(id=appr_id).chat_id, "1001")               # backfilled from evidence
        self.assertEqual(M.objects.get(id=acq_id).approval_id, appr_id)           # association intact
        self.assertEqual(M.objects.get(id=acq_id).raw_payload.get("text"), "orig")  # content untouched
        self.assertTrue(M.objects.get(id=drop_id).non_genuine)                    # drop classified

        # Rollback to 0009: new columns dropped, but ROWS + ASSOCIATIONS + CONTENT preserved.
        old = self._migrate(AT_0009)
        A0 = old.get_model(APP, "PendingSignalApproval")
        M0 = old.get_model(APP, "AcquiredMessage")
        self.assertNotIn("chat_id", {f.name for f in A0._meta.get_fields()})
        self.assertNotIn("non_genuine", {f.name for f in M0._meta.get_fields()})
        self.assertTrue(A0.objects.filter(id=appr_id).exists())
        self.assertEqual(M0.objects.get(id=acq_id).approval_id, appr_id)          # association survived
        self.assertEqual(M0.objects.get(id=acq_id).raw_payload.get("text"), "orig")  # never rewritten
        self.assertEqual(M0.objects.filter(id=drop_id).count(), 1)                # drop row preserved

        # Reapply forward: succeeds, deterministic again.
        new2 = self._migrate(AT_0011)
        A2 = new2.get_model(APP, "PendingSignalApproval")
        M2 = new2.get_model(APP, "AcquiredMessage")
        self.assertEqual(A2.objects.get(id=appr_id).chat_id, "1001")
        self.assertTrue(M2.objects.get(id=drop_id).non_genuine)

    def test_cross_chat_rows_coexist_and_rollback_limit_is_documented(self):
        """R2 MEDIUM (documented limit): post-cutover, two GENUINE rows for the same
        (provider, message_id) in DIFFERENT chats legitimately coexist under 0010/0011
        (keyed with chat_id). A rollback PAST 0010 to 0009 would re-impose the old
        ``uniq_provider_message`` (provider, message_id) key, which these rows VIOLATE —
        so rollback after a genuine cross-channel collision is prohibited by the old
        uniqueness contract and must NOT be forced (we never weaken constraints to allow
        an unsafe rollback). Here we only assert the safe forward coexistence; the unsafe
        rollback is documented, not executed (it would abort mid-migration by design)."""
        new = self._migrate(AT_0011)
        PP = new.get_model(APP, "ParserProfile")
        Prov = new.get_model(APP, "SignalProvider")
        M = new.get_model(APP, "AcquiredMessage")
        pp = PP.objects.create(slug="wayond_v1")
        prov = Prov.objects.create(slug="wayond", telegram_chat_id="2002",
                                   parser_profile=pp, status="ARMED")
        M.objects.create(provider=prov, chat_id="1001", message_id="5", outcome="INTAKEN")
        M.objects.create(provider=prov, chat_id="2002", message_id="5", outcome="INTAKEN")
        # Both genuine rows coexist under the chat-scoped key — the T1 goal.
        self.assertEqual(
            M.objects.filter(provider_id=prov.id, message_id="5", non_genuine=False).count(), 2)
