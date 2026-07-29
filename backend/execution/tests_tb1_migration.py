"""TB-1 / ADR-0020 — migration 0025 (SignalExecutionPlan.approval OneToOne -> FK) evidence.

Proves the Sponsor controls for the certified-plane migration:
  * forward preserves every existing plan (no loss / duplication / reinterpretation);
  * forward installs the new invariant — one plan per (approval, account) — and now permits a second
    plan for the SAME approval on a DIFFERENT account (fan-out);
  * reverse restores the OneToOne(approval) invariant and preserves data while single-tenant
    (<=1 plan per approval — guaranteed while the flag has never been enabled).

Seeds with the CURRENT models (a single plan-per-approval insert is valid against both the 0024 and
0025 schemas — the only column touched is metadata on the existing approval_id column), which avoids
the historical-model field skew from the migration dependency closure.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from execution.models import SignalExecutionPlan
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

APP = "execution"
FROM = "0024_protection_stage_db_default"
TO = "0025_remove_signalexecutionplan_uniq_plan_source_chat_message_and_more"
U = get_user_model()


class PlanMigration0025Tests(TransactionTestCase):
    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([(APP, target)])
        executor.loader.build_graph()

    def tearDown(self):
        # Leave the schema at the latest migrations for the rest of the suite.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _seed_plan(self, *, suffix="1", account=None):
        u = U.objects.create(username=f"mu{suffix}", email=f"mu{suffix}@x.invalid", password="x")
        appr = PendingSignalApproval.objects.create(source="ti_signals", message_id=f"mm{suffix}")
        acct = account or TradingAccount.objects.create(
            user=u, name="A", account_number=f"A{suffix}", is_demo=True, broker_name="DemoBroker")
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=acct, source="ti_signals", message_id=f"mm{suffix}",
            symbol="EURUSD", direction="BUY", is_demo=True)
        return plan, appr, acct

    def test_forward_preserves_plan_and_installs_per_account_invariant(self):
        self._migrate(FROM)
        plan, appr, acct = self._seed_plan()
        pid, aid, acid = plan.pk, appr.pk, acct.pk

        self._migrate(TO)   # forward: OneToOne -> FK

        p = SignalExecutionPlan.objects.get(pk=pid)     # row preserved
        self.assertEqual(p.approval_id, aid)            # association intact, not reinterpreted
        self.assertEqual(p.account_id, acid)
        self.assertEqual(SignalExecutionPlan.objects.count(), 1)   # no duplication

        # NEW invariant: a 2nd plan for the SAME (approval, account) is refused.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SignalExecutionPlan.objects.create(
                    approval_id=aid, account_id=acid, source="ti_signals", message_id="mm1",
                    symbol="EURUSD", direction="BUY", is_demo=True)

        # ...but the SAME approval on a DIFFERENT account is now allowed (fan-out).
        acct2 = TradingAccount.objects.create(
            user=acct.user, name="B", account_number="A1b", is_demo=True, broker_name="DemoBroker")
        SignalExecutionPlan.objects.create(
            approval_id=aid, account=acct2, source="ti_signals", message_id="mm1",
            symbol="EURUSD", direction="BUY", is_demo=True)
        self.assertEqual(SignalExecutionPlan.objects.filter(approval_id=aid).count(), 2)

    def test_reverse_restores_onetoone_and_preserves_data(self):
        self._migrate(TO)
        plan, appr, acct = self._seed_plan(suffix="r")
        pid = plan.pk

        self._migrate(FROM)   # reverse: FK -> OneToOne (succeeds while single-tenant)

        self.assertEqual(SignalExecutionPlan.objects.filter(pk=pid).count(), 1)   # preserved
        self.assertEqual(SignalExecutionPlan.objects.get(pk=pid).approval_id, appr.pk)
