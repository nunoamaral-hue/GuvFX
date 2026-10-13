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

        new = self._migrate(AT_0011)
        Appr2 = new.get_model(APP, "PendingSignalApproval")
        Acq2 = new.get_model(APP, "AcquiredMessage")

        self.assertEqual(Appr2.objects.get(message_id="5").chat_id, "1001")  # from evidence
        self.assertEqual(Appr2.objects.get(message_id="6").chat_id, "")      # provider NOT inferred
        self.assertEqual(Appr2.objects.get(message_id="L").chat_id, "")      # legacy, unresolved

        # New trust flag exists and defaults False on migrated rows.
        self.assertFalse(Acq2.objects.get(message_id="5").chat_identity_refused)

        # Constraint enforcement on migrated data: a duplicate GENUINE row is rejected,
        # but a REFUSAL row may share the tuple (partitioned by chat_identity_refused).
        with self.assertRaises(IntegrityError), transaction.atomic():
            Acq2.objects.create(provider_id=prov.id, chat_id="1001", message_id="5",
                                outcome="INTAKEN", chat_identity_refused=False)
        Acq2.objects.create(provider_id=prov.id, chat_id="1001", message_id="5",
                            outcome="QUARANTINED", chat_identity_refused=True)  # coexists
        self.assertEqual(Acq2.objects.filter(message_id="5").count(), 2)

    def test_forward_rollback_reapply(self):
        self._migrate(AT_0011)
        # Rollback the whole chain to 0009: chat_id + chat_identity_refused are dropped.
        old = self._migrate(AT_0009)
        Appr = old.get_model(APP, "PendingSignalApproval")
        Acq = old.get_model(APP, "AcquiredMessage")
        self.assertNotIn("chat_id", {f.name for f in Appr._meta.get_fields()})
        self.assertNotIn("chat_identity_refused", {f.name for f in Acq._meta.get_fields()})
        # Reapply forward (safe, pre-switch).
        new = self._migrate(AT_0011)
        Appr2 = new.get_model(APP, "PendingSignalApproval")
        Acq2 = new.get_model(APP, "AcquiredMessage")
        self.assertIn("chat_id", {f.name for f in Appr2._meta.get_fields()})
        self.assertIn("chat_identity_refused", {f.name for f in Acq2._meta.get_fields()})
