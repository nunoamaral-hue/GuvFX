"""TB-1 / ADR-0020 — migration 0025 (SignalExecutionPlan.approval OneToOne -> FK) evidence.

Proves the Sponsor controls for the certified-plane migration:
  * forward preserves every existing plan (no loss / duplication / reinterpretation);
  * forward installs the new invariant — one plan per (approval, account) — and now permits a second
    plan for the SAME approval on a DIFFERENT account (fan-out);
  * reverse restores the OneToOne(approval) invariant and preserves data while single-tenant
    (<=1 plan per approval — guaranteed while the flag has never been enabled).

Seeds with the HISTORICAL models matching the currently-applied schema (via the migration state's
apps registry), NOT the current models: a single-target migrate to ``execution`` prunes each app back
within execution's dependency closure, so any column added by a LATER migration outside that closure
(e.g. WP1A ``trading.disconnected_at``, or Phase-A ``SignalExecutionPlan.strategy_assignment``) is
absent from the DB. Seeding via the current models would emit those columns and fail; the historical
models emit only the columns that exist, keeping this an execution-only migration test.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from signal_intake.models import PendingSignalApproval

APP = "execution"
FROM = "0024_protection_stage_db_default"
TO = "0025_remove_signalexecutionplan_uniq_plan_source_chat_message_and_more"
U = get_user_model()


class PlanMigration0025Tests(TransactionTestCase):
    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([(APP, target)])
        executor.loader.build_graph()

    def _hist(self, app, model):
        """The historical model matching the currently-applied migration state, so inserts/selects
        emit ONLY the columns that exist at that state (no current-model field skew)."""
        loader = MigrationExecutor(connection).loader
        state = loader.project_state(nodes=list(loader.applied_migrations.keys()))
        return state.apps.get_model(app, model)

    def _mk_account(self, user_id, number):
        Hist = self._hist("trading", "TradingAccount")
        return Hist.objects.create(
            user_id=user_id, name="A", account_number=number, is_demo=True, broker_name="DemoBroker")

    def tearDown(self):
        # Leave the schema at the latest migrations for the rest of the suite.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _seed_plan(self, *, suffix="1", account=None):
        u = U.objects.create(username=f"mu{suffix}", email=f"mu{suffix}@x.invalid", password="x")
        appr = PendingSignalApproval.objects.create(source="ti_signals", message_id=f"mm{suffix}")
        acct = account or self._mk_account(u.pk, f"A{suffix}")
        Plan = self._hist("execution", "SignalExecutionPlan")
        plan = Plan.objects.create(
            approval_id=appr.pk, account_id=acct.pk, source="ti_signals", message_id=f"mm{suffix}",
            symbol="EURUSD", direction="BUY", is_demo=True)
        return plan, appr, acct

    def test_forward_preserves_plan_and_installs_per_account_invariant(self):
        self._migrate(FROM)
        plan, appr, acct = self._seed_plan()
        pid, aid, acid = plan.pk, appr.pk, acct.pk

        self._migrate(TO)   # forward: OneToOne -> FK
        Plan = self._hist("execution", "SignalExecutionPlan")

        p = Plan.objects.get(pk=pid)                    # row preserved
        self.assertEqual(p.approval_id, aid)            # association intact, not reinterpreted
        self.assertEqual(p.account_id, acid)
        self.assertEqual(Plan.objects.count(), 1)       # no duplication

        # NEW invariant: a 2nd plan for the SAME (approval, account) is refused.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Plan.objects.create(
                    approval_id=aid, account_id=acid, source="ti_signals", message_id="mm1",
                    symbol="EURUSD", direction="BUY", is_demo=True)

        # ...but the SAME approval on a DIFFERENT account is now allowed (fan-out).
        acct2 = self._mk_account(acct.user_id, "A1b")
        Plan.objects.create(
            approval_id=aid, account_id=acct2.pk, source="ti_signals", message_id="mm1",
            symbol="EURUSD", direction="BUY", is_demo=True)
        self.assertEqual(Plan.objects.filter(approval_id=aid).count(), 2)

    def test_reverse_restores_onetoone_and_preserves_data(self):
        self._migrate(TO)
        plan, appr, acct = self._seed_plan(suffix="r")
        pid = plan.pk

        self._migrate(FROM)   # reverse: FK -> OneToOne (succeeds while single-tenant)
        Plan = self._hist("execution", "SignalExecutionPlan")

        self.assertEqual(Plan.objects.filter(pk=pid).count(), 1)   # preserved
        self.assertEqual(Plan.objects.get(pk=pid).approval_id, appr.pk)
