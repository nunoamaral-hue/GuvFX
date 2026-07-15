"""WS-B AUTO-BREAKEVEN — sweep_breakeven() unit tests.

Cover: inert-unless-armed, TP1-close triggers a move on remaining OPEN legs only, idempotency
(applied / in-flight / success-reconcile), retry + exhausted-retry alert, the fail-safe
never-increase-risk guard (BUY and SELL), and the periodic position-sync enqueue + dedup.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import breakeven
from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan
from signal_intake.models import PendingSignalApproval
from trading.models import Trade, TradingAccount

User = get_user_model()
TI = "ti_signals"


def _enable():
    """Arm the sweep for the duration of a with-block (BREAKEVEN_ENABLED is env-gated)."""
    return mock.patch.object(breakeven, "breakeven_enabled", return_value=True)


class BreakevenSweepTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="be", email="be@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="BE1", is_demo=True)
        # Positions carry a windows_username via the account's mt5_instance; patch the trivial
        # getattr-chain so tests need no full Mt5Instance fixture.
        p = mock.patch.object(breakeven, "_windows_username", return_value="mt5user")
        p.start()
        self.addCleanup(p.stop)

    def _plan(self, *, direction="BUY", entry="4005", sl="4000",
              tps=("4010", "4020", "4030"), closed=(False, False, False)):
        mid = f"m{SignalExecutionPlan.objects.count()}"
        appr = PendingSignalApproval.objects.create(
            source=TI, message_id=mid, symbol="XAUUSD", direction=direction, stop_loss=sl,
            take_profits=list(tps), status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=self.acct, source=TI, message_id=mid, symbol="XAUUSD",
            direction=direction, stop_loss=sl, is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PROMOTED)
        legs = []
        for i, (tp, is_closed) in enumerate(zip(tps, closed), start=1):
            leg = ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit=tp, stop_loss=sl,
                lot_size=Decimal("0.40"), status=ProposedOrderLeg.Status.PROMOTED)
            Trade.objects.create(
                account=self.acct, symbol="XAUUSD", side=direction, volume=Decimal("0.40"),
                ticket=f"pos{plan.id}{i}", open_time=timezone.now(), open_price=Decimal(entry),
                close_time=timezone.now() if is_closed else None,
                comment=f"WAY{plan.id}L{i}")
            legs.append(leg)
        return plan, legs

    # -- arm switch -----------------------------------------------------------
    def test_disabled_returns_noop(self):
        self._plan(closed=(True, False, False))
        res = breakeven.sweep_breakeven()
        self.assertEqual(res, {"enabled": False})
        self.assertFalse(ExecutionJob.objects.filter(job_type="MODIFY_POSITION").exists())

    # -- core trigger ---------------------------------------------------------
    def test_tp1_closed_moves_remaining_open_legs(self):
        plan, legs = self._plan(closed=(True, False, False))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 2)
        jobs = list(ExecutionJob.objects.filter(job_type="MODIFY_POSITION").order_by("id"))
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.assertEqual(job.payload["sl"], 4005.0)          # SL → entry
            self.assertTrue(job.payload["ticket"].startswith("pos"))
            self.assertEqual(job.payload["reason"], "auto_breakeven_tp1")
        legs[1].refresh_from_db()
        self.assertIsNotNone(legs[1].breakeven_job)
        self.assertEqual(legs[1].breakeven_attempts, 1)

    def test_tp1_open_no_breakeven(self):
        self._plan(closed=(False, False, False))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)

    def test_closed_remaining_leg_not_modified(self):
        # TP1 + TP2 closed, only TP3 open → exactly one move.
        self._plan(closed=(True, True, False))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 1)

    # -- idempotency ----------------------------------------------------------
    def test_applied_not_reenqueued(self):
        plan, legs = self._plan(closed=(True, False, False))
        for leg in legs[1:]:
            leg.breakeven_applied_at = timezone.now()
            leg.save(update_fields=["breakeven_applied_at"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)

    def test_inflight_not_reenqueued(self):
        plan, legs = self._plan(closed=(True, False, True))  # isolate leg2 (leg3 closed)
        job = ExecutionJob.objects.create(job_type="MODIFY_POSITION", account=self.acct,
                                          status=ExecutionJob.Status.PENDING, payload={})
        legs[1].breakeven_job = job
        legs[1].breakeven_attempts = 1
        legs[1].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["inflight"], 1)
        self.assertEqual(res["enqueued"], 0)

    def test_success_job_marks_applied(self):
        plan, legs = self._plan(closed=(True, False, True))
        job = ExecutionJob.objects.create(job_type="MODIFY_POSITION", account=self.acct,
                                          status=ExecutionJob.Status.SUCCESS, payload={})
        legs[1].breakeven_job = job
        legs[1].breakeven_attempts = 1
        legs[1].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        legs[1].refresh_from_db()
        self.assertIsNotNone(legs[1].breakeven_applied_at)  # terminal, broker-verified
        self.assertEqual(res["applied"], 1)
        self.assertEqual(res["enqueued"], 0)

    # -- retry + alert --------------------------------------------------------
    def test_failed_job_retries(self):
        plan, legs = self._plan(closed=(True, False, True))
        job = ExecutionJob.objects.create(job_type="MODIFY_POSITION", account=self.acct,
                                          status=ExecutionJob.Status.FAILED, payload={})
        legs[1].breakeven_job = job
        legs[1].breakeven_attempts = 1
        legs[1].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 1)
        legs[1].refresh_from_db()
        self.assertEqual(legs[1].breakeven_attempts, 2)

    def test_exhausted_retries_alerts_and_stops(self):
        from reliability.models import AlertEvent
        plan, legs = self._plan(closed=(True, False, True))
        job = ExecutionJob.objects.create(job_type="MODIFY_POSITION", account=self.acct,
                                          status=ExecutionJob.Status.FAILED, payload={})
        legs[1].breakeven_job = job
        legs[1].breakeven_attempts = breakeven.MAX_BREAKEVEN_ATTEMPTS
        legs[1].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["alerted"], 1)
        self.assertEqual(res["enqueued"], 0)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"breakeven_failed:plan:{plan.id}:leg:2",
            status=AlertEvent.Status.OPEN).exists())
        # Re-running does not create a duplicate alert (deduped) and still enqueues nothing.
        with _enable():
            breakeven.sweep_breakeven()
        self.assertEqual(AlertEvent.objects.filter(
            dedup_key=f"breakeven_failed:plan:{plan.id}:leg:2").count(), 1)

    # -- fail-safe (never increase risk) --------------------------------------
    def test_failsafe_skips_when_not_risk_reducing_buy(self):
        # BUY with entry BELOW the SL: moving SL to entry would LOWER the stop → widen risk. Skip.
        self._plan(direction="BUY", entry="3990", sl="4000", closed=(True, False, False))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)
        self.assertEqual(res["skipped"], 2)

    def test_sell_moves_to_breakeven(self):
        # SELL: SL sits above entry; moving SL down to entry reduces risk → allowed.
        self._plan(direction="SELL", entry="3990", sl="4000", closed=(True, False, False))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 2)

    def test_failsafe_skips_when_plan_sl_blank(self):
        self._plan(sl="", closed=(True, False, False))  # no original SL → cannot prove safe
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)
        self.assertEqual(res["skipped"], 2)

    # -- periodic position sync ----------------------------------------------
    def test_position_sync_enqueued_then_deduped(self):
        self._plan(closed=(True, False, False))
        with _enable():
            r1 = breakeven.sweep_breakeven()
            r2 = breakeven.sweep_breakeven()
        self.assertEqual(r1["synced"], 1)
        self.assertEqual(r2["synced"], 0)  # a SYNC is already pending → not duplicated
        self.assertEqual(ExecutionJob.objects.filter(job_type="SYNC_POSITIONS").count(), 1)
