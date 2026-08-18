"""ADR-0048 — stale pre-activation order reconciler + exposure-cascade regression.

Proves the reconciler safely neutralises never-claimed PENDING PLACE_ORDER jobs (so they can never
fire when a node is later activated), releases the ``account_exposure_exceeded`` cascade they cause,
is idempotent + account-scoped + fail-closed, refuses Customer Zero / account 18, and NEVER touches
a claimed/filled order. No order is ever placed.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan
from execution.risk_controls import evaluate_promotion_risk
from execution.stale_reconcile import (
    ProtectedAccountError,
    reconcile_stale_preactivation_orders,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()
PENDING = ExecutionJob.Status.PENDING
RUNNING = ExecutionJob.Status.RUNNING
SUCCESS = ExecutionJob.Status.SUCCESS
FAILED = ExecutionJob.Status.FAILED
PLACE_ORDER = ExecutionJob.JobType.PLACE_ORDER
PROMOTED = SignalExecutionPlan.Status.PROMOTED
CLOSED = SignalExecutionPlan.Status.CLOSED


def _acct(pk, number, user):
    return TradingAccount.objects.create(
        id=pk, user=user, name=number, account_number=number, broker_name="DemoBroker",
        is_demo=True, is_active=True, password_enc="enc")


class StalePreactivationReconcileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@x.invalid", password="x")
        # Non-protected working account (id 100) + a bystander (id 101). Protected ids {1,18}.
        self.acct = _acct(100, "A100", self.user)
        self.other = _acct(101, "A101", self.user)

    _seq = 0

    def _promoted_plan(self, account, *, symbol="XAUUSD", n_legs=3, lot_each="0.40",
                       job_status=PENDING, worker_id="", job_type=PLACE_ORDER):
        type(self)._seq += 1
        mid = f"m-{type(self)._seq}"  # unique per plan: (source, chat_id, message_id, account) is UNIQUE
        appr = PendingSignalApproval.objects.create(
            source="ti_signals", message_id=mid, symbol=symbol, direction="BUY", entry="4400",
            stop_loss="4390", take_profit="4410", take_profits=["4410"],
            status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=account, source="ti_signals", message_id=mid, symbol=symbol,
            direction="BUY", entry="4400", stop_loss="4390", total_lot=Decimal(lot_each) * n_legs,
            is_demo=True, status=PROMOTED)
        jobs = []
        for i in range(1, n_legs + 1):
            job = ExecutionJob.objects.create(
                job_type=job_type, account=account, status=job_status,
                worker_id=worker_id, started_at=(timezone.now() if job_status != PENDING else None),
                payload={"symbol": symbol, "side": "BUY", "lots": lot_each, "plan_id": plan.id,
                         "leg_index": i})
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit="4410", stop_loss="4390",
                lot_size=Decimal(lot_each), execution_job=job, status=ProposedOrderLeg.Status.PROMOTED)
            jobs.append(job)
        return plan, jobs

    # ── core: cancel + close, no order ────────────────────────────────────────────────────────────
    def test_apply_cancels_pending_jobs_and_closes_plan(self):
        plan, jobs = self._promoted_plan(self.acct)
        r = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        self.assertEqual(r["jobs_cancelled"], 3)
        self.assertEqual(r["plans_closed"], 1)
        for j in jobs:
            j.refresh_from_db()
            self.assertEqual(j.status, FAILED)          # terminal, never SUCCESS (no fill)
            self.assertTrue(j.recovered)                 # durable forensic trail
            self.assertEqual(j.recovery_reason, "stale_preactivation_reconcile")
            self.assertIsNotNone(j.finished_at)
        plan.refresh_from_db()
        self.assertEqual(plan.status, CLOSED)            # PROMOTED→CLOSED releases exposure

    def test_dry_run_mutates_nothing(self):
        plan, jobs = self._promoted_plan(self.acct)
        r = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=False)
        self.assertEqual(len(r["candidates"]), 1)
        self.assertEqual(r["jobs_cancelled"], 0)
        for j in jobs:
            j.refresh_from_db(); self.assertEqual(j.status, PENDING)
        plan.refresh_from_db(); self.assertEqual(plan.status, PROMOTED)

    # ── the exposure cascade regression (req 6) ───────────────────────────────────────────────────
    def test_exposure_cascade_released_by_reconcile(self):
        # Two stale PROMOTED plans = 6 legs × 0.40 = 2.40 lots = MAX_ACCOUNT_EXPOSURE_LOT. A third
        # incoming plan (+1.20) would exceed the cap → account_exposure_exceeded. After reconcile the
        # two stale plans are CLOSED, so the incoming plan promotes.
        self._promoted_plan(self.acct)
        self._promoted_plan(self.acct)
        incoming, inc_jobs = self._promoted_plan(self.acct)
        incoming.status = SignalExecutionPlan.Status.PLANNED   # not yet promoted; the candidate
        incoming.save(update_fields=["status"])
        inc_legs = list(incoming.legs.all())

        before = evaluate_promotion_risk(incoming, inc_legs)
        self.assertEqual(before, "account_exposure_exceeded")

        reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)

        after = evaluate_promotion_risk(incoming, inc_legs)
        self.assertNotEqual(after, "account_exposure_exceeded")   # cascade released

    # ── safety: idempotent, scoped, refuses sacred accounts, never touches live orders ────────────
    def test_idempotent(self):
        self._promoted_plan(self.acct)
        r1 = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        r2 = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        self.assertEqual(r1["jobs_cancelled"], 3)
        self.assertEqual(r2["jobs_cancelled"], 0)      # nothing left PENDING
        self.assertEqual(r2["scanned_promoted_plans"], 0)  # plan already CLOSED

    def test_account_scoped_does_not_touch_bystander(self):
        _, mine = self._promoted_plan(self.acct)
        oplan, theirs = self._promoted_plan(self.other)
        reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        oplan.refresh_from_db()
        self.assertEqual(oplan.status, PROMOTED)        # bystander untouched
        for j in theirs:
            j.refresh_from_db(); self.assertEqual(j.status, PENDING)

    def test_refuses_customer_zero_and_account_18(self):
        for pk in (1, 18):
            with self.assertRaises(ProtectedAccountError):
                reconcile_stale_preactivation_orders(account_id=pk, apply=True)

    def test_skips_plan_with_running_leg(self):
        # A RUNNING leg (possibly already order_send'd — PLACE_ORDER is non-idempotent) must NEVER be
        # blind-cancelled; the whole plan is skipped.
        plan, jobs = self._promoted_plan(self.acct)
        jobs[0].status = RUNNING
        jobs[0].worker_id = "mt5-w"
        jobs[0].started_at = timezone.now()
        jobs[0].save(update_fields=["status", "worker_id", "started_at"])
        r = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        self.assertEqual(r["jobs_cancelled"], 0)
        self.assertEqual(len(r["candidates"]), 0)
        self.assertTrue(any(s["plan_id"] == plan.id for s in r["skipped"]))
        for j in jobs:
            j.refresh_from_db()
            self.assertIn(j.status, (RUNNING, PENDING))   # nothing forced to FAILED
        plan.refresh_from_db(); self.assertEqual(plan.status, PROMOTED)

    def test_skips_plan_with_success_leg(self):
        plan, jobs = self._promoted_plan(self.acct)
        jobs[0].status = SUCCESS
        jobs[0].save(update_fields=["status"])
        r = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        self.assertEqual(r["jobs_cancelled"], 0)
        plan.refresh_from_db(); self.assertEqual(plan.status, PROMOTED)

    def test_command_dry_run_default_no_mutation(self):
        plan, jobs = self._promoted_plan(self.acct)
        call_command("reconcile_stale_preactivation_orders", "--account-id", str(self.acct.id),
                     "--older-than-seconds", "0")
        for j in jobs:
            j.refresh_from_db(); self.assertEqual(j.status, PENDING)   # default is dry-run

    def test_respects_older_than_window(self):
        # A brand-new plan is NOT reconciled under the default staleness window.
        self._promoted_plan(self.acct)
        r = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=1800, apply=True)
        self.assertEqual(r["scanned_promoted_plans"], 0)
        self.assertEqual(r["jobs_cancelled"], 0)

    def test_skips_plan_whose_legs_are_not_place_order(self):
        # MEDIUM-2 fix: a PROMOTED plan whose legs are PLACE_ORDER_SHADOW (not PLACE_ORDER) must be
        # SKIPPED, never listed as a candidate — the cancel path only fails PLACE_ORDER, so listing it
        # would cancel 0 and leak its exposure forever.
        plan, jobs = self._promoted_plan(self.acct, job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        r = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        self.assertEqual(len(r["candidates"]), 0)
        self.assertEqual(r["jobs_cancelled"], 0)
        self.assertTrue(any(s["plan_id"] == plan.id for s in r["skipped"]))
        plan.refresh_from_db(); self.assertEqual(plan.status, PROMOTED)   # untouched

    def test_refuses_apply_when_live_claimant_present(self):
        # LOW-b fix: enforce reconcile-BEFORE-activation. If the account's node already has a live
        # eligible order worker, --apply is refused and nothing is mutated.
        from execution.models import TerminalNode, WorkerIdentity
        node = TerminalNode.objects.create(hostname="guvfx-beta-node-1",
                                           order_bridge_base_url="http://10.0.0.1:8789")
        self.acct.terminal_node = node
        self.acct.save(update_fields=["terminal_node"])
        WorkerIdentity.objects.create(worker_id="w-live", worker_secret_hash="x",
                                      worker_permissions={"authorized_nodes": ["guvfx-beta-node-1"]},
                                      last_seen=timezone.now())
        plan, jobs = self._promoted_plan(self.acct)
        r = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        self.assertEqual(r.get("refused"), "live_claimant_present")
        self.assertEqual(r["jobs_cancelled"], 0)
        for j in jobs:
            j.refresh_from_db(); self.assertEqual(j.status, PENDING)      # untouched
        plan.refresh_from_db(); self.assertEqual(plan.status, PROMOTED)

    def test_refuses_apply_when_registered_but_unseen_worker_present(self):
        # Adversarial MEDIUM-1 fix: a JUST-COMMISSIONED node worker has last_seen=NULL, so it is NOT a
        # "live" eligible claimant — yet the claim seam authorizes it to claim+dispatch on its very first
        # poll. Reconciling while such a worker is registered would race a register→first-poll claim and
        # could strand a cross-leg partial fill. So --apply must refuse as soon as ANY ACTIVE node-aware
        # worker exists for the node, not only a recently-seen one. Enforces reconcile-BEFORE-commission.
        from execution.models import TerminalNode, WorkerIdentity
        node = TerminalNode.objects.create(hostname="guvfx-beta-node-1",
                                           order_bridge_base_url="http://10.0.0.1:8789")
        self.acct.terminal_node = node
        self.acct.save(update_fields=["terminal_node"])
        WorkerIdentity.objects.create(worker_id="w-fresh", worker_secret_hash="x",
                                      worker_permissions={"authorized_nodes": ["guvfx-beta-node-1"]},
                                      last_seen=None)  # registered, never polled — still claim-capable
        plan, jobs = self._promoted_plan(self.acct)
        r = reconcile_stale_preactivation_orders(account_id=self.acct.id, older_than_seconds=0, apply=True)
        self.assertEqual(r.get("refused"), "node_worker_registered")
        self.assertEqual(r["jobs_cancelled"], 0)
        for j in jobs:
            j.refresh_from_db(); self.assertEqual(j.status, PENDING)      # untouched
        plan.refresh_from_db(); self.assertEqual(plan.status, PROMOTED)
