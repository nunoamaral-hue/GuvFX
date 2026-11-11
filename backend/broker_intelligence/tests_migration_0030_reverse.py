"""GFX-WITHDRAWAL-CI-HYGIENE-051 — regression: migration 0030 (Vantage activation) must REVERSE and REAPPLY cleanly.

CI/test-reliability only — this asserts nothing about Withdrawal Intelligence product behaviour; it guards the
migration mechanics that hung the backend CI job.

Before the fix, 0030's reverse (``_retire``) filtered ``BrokerProfileAudit`` by a historical model INSTANCE
(``broker_profile=bp``). When the ``--parallel`` runner reverses a RANGE of migrations, Django renders ``BrokerProfile``
from the historical migration state and ``bp``'s rendered class could differ from the class the
``BrokerProfileAudit.broker_profile`` FK expects -> ``ValueError: Cannot query ...: Must be a BrokerProfile instance.``.
That exception's traceback cannot be pickled, so the parallel worker could never report the failure and the whole
backend job hung with no timeout. The fix filters by the integer pk instead.

Two conditions are needed to exercise the defect, and this test guarantees BOTH, durably:
  1. 0030 must be the FIRST migration reversed in a >=2-migration backward range. The mismatch only manifests on the
     first migration of a multi-step reverse (a single-step reverse renders the state once and stays consistent; a
     migration reversed second or later does not mis-render — e.g. 0027/0026 share the same instance-filter pattern yet
     never raise, because they are never reversed first). So the test does NOT reverse from the moving app leaf (which
     would make 0030 a non-first op, silently disarming the guard, once a later migration such as 0031 exists). Instead
     it normalises down to 0030, then reverses 0030 -> 0028, anchoring 0030 as the first-reversed op regardless of what
     is added above it.
  2. The Vantage profile row must exist, or 0030's ``_retire`` early-returns (``bp is None``) and never reaches the
     filter -> ``setUp`` idempotently ensures the row exists, because a TransactionTestCase truncates every table on
     teardown and does NOT re-run data migrations, so an earlier TransactionTestCase in the same (parallel) worker can
     leave the migration-seeded Vantage row absent.

The reverse stops at 0028, ABOVE the 0026 seed migration, so the Vantage row persists and 0026's append-only reverse
guard is never involved. The final active state is produced by the REAPPLIED 0030 (_activate), so the assertions do
not depend on any pre-existing seed. It is a ``TransactionTestCase`` because schema/data DDL cannot run inside the
single outer transaction of a ``TestCase``; it always restores the schema to the app's current leaf afterwards, so it
cannot corrupt the shared DB for later tests in the same worker.
"""
from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class Migration0030ReverseReapply(TransactionTestCase):
    _APP = "broker_intelligence"
    _V0028 = "0028_generic_withdrawal_detection"      # reverse target: 0030 -> 0029 -> 0028 (0030 first, >=2 range)
    _V0030 = "0030_activate_vantage_profile"

    def _migrate(self, target):
        MigrationExecutor(connection).migrate([(self._APP, target)])

    def _restore_latest(self):
        # Restore to the app's CURRENT leaf (not a hardcoded version) so this shared-DB TransactionTestCase cannot hide
        # any table/column added after the leaf from later tests in the same worker.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes(self._APP))

    def _ensure_vantage_active(self):
        # Idempotently ensure a Vantage BrokerProfile row exists and is in the activated state 0030 leaves it, so the
        # reverse's 0030._retire reaches the filter under test (with the row absent it early-returns) and its
        # deactivation is observable. A prior TransactionTestCase in the same worker may have truncated the
        # migration-seeded row. Use a queryset .update() (not the audit-appending model save()) so no stray audit rows
        # are introduced. BrokerProfile requires only name + slug.
        from broker_intelligence.models import BrokerProfile
        bp, _ = BrokerProfile.objects.get_or_create(slug="vantage", defaults={"name": "Vantage"})
        BrokerProfile.objects.filter(pk=bp.pk).update(is_active=True, provenance="OPERATOR_VERIFIED")

    def setUp(self):
        self._restore_latest()                  # normalise the schema to the leaf
        self.addCleanup(self._restore_latest)   # and ALWAYS restore to it, even if an assertion fails
        self._ensure_vantage_active()

    # --- raw-SQL probes (valid at any migration state; immune to the real model's current column set) ---

    def _vantage_state(self):
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass('broker_intelligence_brokerprofile')")
            if cur.fetchone()[0] is None:
                return None   # the table itself does not exist at this depth
            cur.execute("SELECT id, is_active, provenance FROM broker_intelligence_brokerprofile WHERE slug=%s",
                        ["vantage"])
            return cur.fetchone()   # (id, is_active, provenance) or None

    def _forward_audit_rows(self, profile_id):
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM broker_intelligence_brokerprofileaudit "
                        "WHERE broker_profile_id=%s AND changed_by LIKE %s", [profile_id, "migration 0030%"])
            return cur.fetchone()[0]

    def test_0030_reverses_and_reapplies_without_type_mismatch(self):
        # Precondition: the Vantage row exists (setUp ensured it), so the reverse reaches 0030._retire's filter.
        pre = self._vantage_state()
        self.assertIsNotNone(pre, "setUp must leave a Vantage row for the reverse to act on")

        # Anchor 0030 as the FIRST-reversed migration: first unapply anything above it (e.g. a future 0031), then
        # reverse 0030 -> 0028. Reaching the assertions below proves 0030._retire did not raise.
        self._migrate(self._V0030)
        self._migrate(self._V0028)
        row = self._vantage_state()
        self.assertIsNotNone(row, "Vantage persists at 0028 (the 0026 seed is still applied)")
        pid, is_active, provenance = row
        self.assertFalse(is_active, "0030 reverse must deactivate Vantage (restore the SEED state)")
        self.assertEqual(provenance, "SEED")
        self.assertEqual(self._forward_audit_rows(pid), 0, "0030 reverse must remove the forward audit row it wrote")

        # REAPPLY up to the real leaf -> 0030._activate re-activates Vantage and writes exactly one forward audit row.
        self._restore_latest()
        row = self._vantage_state()
        pid, is_active, provenance = row
        self.assertTrue(is_active, "reapply must re-activate Vantage")
        self.assertEqual(provenance, "OPERATOR_VERIFIED")
        self.assertEqual(self._forward_audit_rows(pid), 1, "reapply must write exactly one forward audit row")
