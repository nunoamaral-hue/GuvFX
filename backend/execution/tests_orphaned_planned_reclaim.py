"""Tests — orphaned-PLANNED-plan concurrency-gate leak fix (Parts A/B/C + W1–W5 + monitoring).

Covers: PromotionRejected lifecycle, PLANNED→VOIDED transition, concurrency release, compare-and-set
race safety, stale-threshold enforcement (W1), created_at basis (W5), reclaim idempotency, the gated
monitor-chain self-heal (Part C), the saturation alert, and regression protection (fresh/PROMOTED/CLOSED
plans untouched; the PlanRejected branch never voids/crashes; the deferral audit is still recorded).
"""
import types
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution import execution_health
from execution.execution_health import (
    detect_saturated_concurrency_gates,
    reclaim_orphaned_planned_plans,
    sweep_execution_health,
)
from execution.models import (
    PLAN_MAX_CONCURRENT_GROUPS,
    SIGNAL_MAX_AGE_SECONDS,
    PlanAuditEvent,
    SignalExecutionPlan,
    SignalSourceConfig,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()
TI = "ti_signals"
SYM = "XAUUSD"


class _Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="opr", email="opr@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="OPR1", is_demo=True, broker_name="DemoBroker")
        SignalSourceConfig.objects.create(source=TI, auto_demo_execution_enabled=True)

    def _plan(self, mid, *, age_s, status=SignalExecutionPlan.Status.PLANNED, symbol=SYM, source=TI,
              sig_age_s=None, account=None):
        """Create a plan, then backdate created_at (auto_now_add) to simulate age."""
        appr = PendingSignalApproval.objects.create(
            source=source, message_id=mid, symbol=symbol, direction="BUY", stop_loss="4000",
            take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)
        now = timezone.now()
        sig_ts = now - timezone.timedelta(seconds=sig_age_s if sig_age_s is not None else age_s)
        p = SignalExecutionPlan.objects.create(
            approval=appr, account=account or self.acct, source=source, message_id=mid, symbol=symbol,
            direction="BUY", is_demo=True, signal_timestamp=sig_ts, status=status)
        created = now - timezone.timedelta(seconds=age_s)
        SignalExecutionPlan.objects.filter(id=p.id).update(created_at=created)
        return SignalExecutionPlan.objects.get(id=p.id)


class ReclaimCoreTests(_Base):
    def test_dry_run_lists_but_never_mutates(self):
        p = self._plan("m1", age_s=1000)
        rep = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=False)
        self.assertEqual(rep["scanned"], 1)
        self.assertEqual(rep["reclaimed"], 0)
        self.assertFalse(rep["apply"])
        self.assertEqual(rep["candidates"][0]["plan_id"], p.id)
        # unchanged
        self.assertEqual(SignalExecutionPlan.objects.get(id=p.id).status,
                         SignalExecutionPlan.Status.PLANNED)
        self.assertFalse(PlanAuditEvent.objects.filter(plan_id=p.id).exists())

    def test_apply_transitions_planned_to_voided_and_audits(self):
        p = self._plan("m1", age_s=1000)
        rep = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        self.assertEqual(rep["reclaimed"], 1)
        p.refresh_from_db()
        self.assertEqual(p.status, SignalExecutionPlan.Status.VOIDED)
        self.assertEqual(p.hold_reason, "orphaned_planned_reclaim")
        audit = PlanAuditEvent.objects.get(plan_id=p.id, event=PlanAuditEvent.Event.PLAN_VOIDED)
        self.assertEqual(audit.detail["reason"], "orphaned_planned_reclaim")

    def test_apply_releases_concurrency_slot(self):
        for i in range(PLAN_MAX_CONCURRENT_GROUPS):
            self._plan(f"c{i}", age_s=1000)
        self.assertEqual(SignalExecutionPlan.count_active(self.acct.id, SYM), PLAN_MAX_CONCURRENT_GROUPS)
        rep = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        self.assertEqual(rep["reclaimed"], PLAN_MAX_CONCURRENT_GROUPS)
        self.assertEqual(SignalExecutionPlan.count_active(self.acct.id, SYM), 0)

    def test_w1_threshold_must_exceed_staleness(self):
        with self.assertRaises(ValueError):
            reclaim_orphaned_planned_plans(older_than_seconds=SIGNAL_MAX_AGE_SECONDS, apply=True)
        with self.assertRaises(ValueError):
            reclaim_orphaned_planned_plans(older_than_seconds=SIGNAL_MAX_AGE_SECONDS - 1, apply=False)
        # a value just above the floor is accepted
        reclaim_orphaned_planned_plans(older_than_seconds=SIGNAL_MAX_AGE_SECONDS + 1, apply=False)

    def test_fresh_planned_is_not_reclaimed(self):
        self._plan("fresh", age_s=60)   # younger than the 900s threshold
        rep = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        self.assertEqual(rep["scanned"], 0)
        self.assertEqual(rep["reclaimed"], 0)

    def test_w5_uses_created_at_not_signal_timestamp(self):
        # OLD created_at but FRESH signal_timestamp → still reclaimed (created_at basis, W5).
        p = self._plan("cx", age_s=1000, sig_age_s=5)
        rep = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        self.assertEqual(rep["reclaimed"], 1)
        self.assertEqual(SignalExecutionPlan.objects.get(id=p.id).status,
                         SignalExecutionPlan.Status.VOIDED)
        # FRESH created_at but OLD signal_timestamp → NOT reclaimed (never targets a young row).
        q = self._plan("cy", age_s=60, sig_age_s=99999)
        rep2 = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        self.assertEqual(rep2["reclaimed"], 0)
        self.assertEqual(SignalExecutionPlan.objects.get(id=q.id).status,
                         SignalExecutionPlan.Status.PLANNED)

    def test_non_planned_states_untouched(self):
        prom = self._plan("p", age_s=1000, status=SignalExecutionPlan.Status.PROMOTED)
        clos = self._plan("c", age_s=1000, status=SignalExecutionPlan.Status.CLOSED)
        void = self._plan("v", age_s=1000, status=SignalExecutionPlan.Status.VOIDED)
        rep = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        self.assertEqual(rep["reclaimed"], 0)
        for x, st in ((prom, "PROMOTED"), (clos, "CLOSED"), (void, "VOIDED")):
            self.assertEqual(SignalExecutionPlan.objects.get(id=x.id).status, st)

    def test_idempotent(self):
        self._plan("m1", age_s=1000)
        r1 = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        r2 = reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        self.assertEqual(r1["reclaimed"], 1)
        self.assertEqual(r2["reclaimed"], 0)

    def test_compare_and_set_no_op_on_concurrently_promoted(self):
        # The CAS invariant: a plan promoted between selection and update matches zero rows → untouched.
        p = self._plan("m1", age_s=1000)
        SignalExecutionPlan.objects.filter(id=p.id).update(status=SignalExecutionPlan.Status.PROMOTED)
        n = SignalExecutionPlan.objects.filter(
            id=p.id, status=SignalExecutionPlan.Status.PLANNED
        ).update(status=SignalExecutionPlan.Status.VOIDED)
        self.assertEqual(n, 0)
        self.assertEqual(SignalExecutionPlan.objects.get(id=p.id).status,
                         SignalExecutionPlan.Status.PROMOTED)

    def test_scope_by_account_and_symbol(self):
        other = TradingAccount.objects.create(
            user=self.user, name="D2", account_number="OPR2", is_demo=True, broker_name="DemoBroker")
        self._plan("a", age_s=1000)                       # acct1 / XAUUSD
        self._plan("b", age_s=1000, symbol="EURUSD")      # acct1 / EURUSD
        self._plan("c", age_s=1000, account=other)        # acct2 / XAUUSD
        rep = reclaim_orphaned_planned_plans(older_than_seconds=900, account_id=self.acct.id,
                                             symbol=SYM, apply=True)
        self.assertEqual(rep["reclaimed"], 1)  # only acct1/XAUUSD


class MonitorChainReclaimTests(_Base):
    def test_self_heal_inert_unless_flag_enabled(self):
        self._plan("m1", age_s=1000)
        with mock.patch.object(execution_health, "ORPHANED_PLANNED_RECLAIM_ENABLED", False):
            res = sweep_execution_health(limit=100)
        self.assertEqual(res["planned_reclaimed"], 0)
        self.assertEqual(SignalExecutionPlan.objects.get(message_id="m1").status,
                         SignalExecutionPlan.Status.PLANNED)

    def test_self_heal_reclaims_when_enabled(self):
        self._plan("m1", age_s=1000)
        with mock.patch.object(execution_health, "ORPHANED_PLANNED_RECLAIM_ENABLED", True):
            res = sweep_execution_health(limit=100)
        self.assertEqual(res["planned_reclaimed"], 1)
        self.assertEqual(SignalExecutionPlan.objects.get(message_id="m1").status,
                         SignalExecutionPlan.Status.VOIDED)


class SaturationAlertTests(_Base):
    def test_alert_raised_at_cap_then_resolved(self):
        from reliability.models import AlertEvent
        for i in range(PLAN_MAX_CONCURRENT_GROUPS):
            self._plan(f"c{i}", age_s=1000)
        now = timezone.now()
        r = detect_saturated_concurrency_gates(now)
        self.assertEqual(r["saturation_alerted"], 1)
        al = AlertEvent.objects.get(dedup_key=f"concurrency_saturation:{self.acct.id}:{SYM}")
        self.assertEqual(al.status, AlertEvent.Status.OPEN)
        self.assertEqual(al.severity, AlertEvent.Severity.CRITICAL)  # at cap
        # Idempotent: no duplicate while OPEN.
        self.assertEqual(detect_saturated_concurrency_gates(timezone.now())["saturation_alerted"], 0)
        # Reclaim frees the slots → next detect resolves the alert.
        reclaim_orphaned_planned_plans(older_than_seconds=900, apply=True)
        r2 = detect_saturated_concurrency_gates(timezone.now())
        self.assertEqual(r2["saturation_resolved"], 1)
        al.refresh_from_db()
        self.assertEqual(al.status, AlertEvent.Status.RESOLVED)

    def test_alert_counts_disabled_source_orphans_source_agnostic(self):
        # W-A: the gate is source-AGNOSTIC (count_active counts all PLANNED per account+symbol). Orphans from a
        # non-tradeable source ('wayond' has no enabled SignalSourceConfig here) still saturate it and MUST
        # trigger the alert — the system has a tradeable source (TI, from setUp) so trading is possible.
        from reliability.models import AlertEvent
        for i in range(PLAN_MAX_CONCURRENT_GROUPS):
            self._plan(f"w{i}", age_s=1000, source="wayond")
        r = detect_saturated_concurrency_gates(timezone.now())
        self.assertEqual(r["saturation_alerted"], 1)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"concurrency_saturation:{self.acct.id}:{SYM}",
            status=AlertEvent.Status.OPEN).exists())


class PartBTransitionOnRejectTests(_Base):
    def test_void_helper_voids_planned_and_audits(self):
        from execution.auto_router import _void_rejected_plan
        p = self._plan("m1", age_s=1)
        _void_rejected_plan(p, "daily_drawdown_hit")
        p.refresh_from_db()
        self.assertEqual(p.status, SignalExecutionPlan.Status.VOIDED)
        self.assertTrue(p.hold_reason.startswith("promotion_rejected:daily_drawdown_hit"))
        self.assertTrue(PlanAuditEvent.objects.filter(
            plan_id=p.id, event=PlanAuditEvent.Event.PLAN_VOIDED).exists())

    def test_void_helper_does_not_touch_promoted(self):
        from execution.auto_router import _void_rejected_plan
        p = self._plan("m1", age_s=1, status=SignalExecutionPlan.Status.PROMOTED)
        _void_rejected_plan(p, "x")
        self.assertEqual(SignalExecutionPlan.objects.get(id=p.id).status,
                         SignalExecutionPlan.Status.PROMOTED)

    def test_promotion_rejected_in_flow_voids_the_plan(self):
        from execution import auto_router
        from execution.signal_promotion import PromotionRejected
        from signal_intake.models import SignalAuditEvent
        appr = PendingSignalApproval.objects.create(
            source=TI, message_id="flow1", symbol=SYM, direction="BUY", stop_loss="4000",
            take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)
        plan = self._plan("flow1p", age_s=1)  # a real PLANNED plan the (mocked) planner returns

        def _raise(_plan, **_kw):
            raise PromotionRejected("daily_drawdown_hit", "blocked")

        target = types.SimpleNamespace(account=self.acct)
        with mock.patch("execution.auto_router.plan_demo_execution", return_value=plan):
            auto_router._plan_and_promote_one(appr, target, self.user, timezone.now(), _raise, "demo")
        self.assertEqual(SignalExecutionPlan.objects.get(id=plan.id).status,
                         SignalExecutionPlan.Status.VOIDED)
        # Regression: the durable deferral audit is still recorded.
        self.assertTrue(SignalAuditEvent.objects.filter(
            event=SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED, approval=appr).exists())

    def test_plan_rejected_branch_never_voids_or_crashes(self):
        # W2: PlanRejected is raised INSIDE plan_demo_execution (plan unbound) → must not attempt a void.
        from execution import auto_router
        from execution.signal_planning import PlanRejected
        appr = PendingSignalApproval.objects.create(
            source=TI, message_id="flow2", symbol=SYM, direction="BUY", stop_loss="4000",
            take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)
        target = types.SimpleNamespace(account=self.acct)
        with mock.patch("execution.auto_router.plan_demo_execution",
                        side_effect=PlanRejected("source_not_enabled", "no")):
            # Must not raise (NameError on unbound `plan`) and must create no plan.
            auto_router._plan_and_promote_one(appr, target, self.user, timezone.now(),
                                              lambda *a, **k: None, "demo")
        self.assertFalse(SignalExecutionPlan.objects.filter(approval=appr).exists())


class CommandTests(_Base):
    def test_command_dry_run_default_does_not_mutate(self):
        p = self._plan("m1", age_s=1000)
        out = StringIO()
        call_command("reclaim_orphaned_planned_plans", "--account", str(self.acct.id),
                     "--symbol", SYM, stdout=out)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertEqual(SignalExecutionPlan.objects.get(id=p.id).status,
                         SignalExecutionPlan.Status.PLANNED)

    def test_command_apply_reclaims(self):
        p = self._plan("m1", age_s=1000)
        out = StringIO()
        call_command("reclaim_orphaned_planned_plans", "--account", str(self.acct.id),
                     "--symbol", SYM, "--apply", stdout=out)
        self.assertIn("RECLAIMED 1", out.getvalue())
        self.assertEqual(SignalExecutionPlan.objects.get(id=p.id).status,
                         SignalExecutionPlan.Status.VOIDED)

    def test_command_rejects_invalid_threshold(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("reclaim_orphaned_planned_plans",
                         "--older-than-seconds", str(SIGNAL_MAX_AGE_SECONDS))
