"""WS-INCREMENTAL-TP-PROTECTION — the protection-ladder state machine.

Covers the packet's required cases: BUY/SELL TP1→breakeven and TP2→TP2-lock; simultaneous TP1+TP2
→ TP3 direct to TP2; profit-gating (SL/loss closes never advance); monotonicity (no downgrade,
idempotent no-op); retry+alert; worker-crash recovery; isolation; Wayond state-2 disabled.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import breakeven
from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan, SignalSourceConfig
from signal_intake.models import PendingSignalApproval
from trading.models import Trade, TradingAccount

User = get_user_model()
TI = "ti_signals"
WAY = "wayond"


def _enable():
    return mock.patch.object(breakeven, "breakeven_enabled", return_value=True)


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="be", email="be@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="BE1", is_demo=True)
        SignalSourceConfig.objects.create(source=TI, incremental_protection_enabled=True)
        SignalSourceConfig.objects.create(source=WAY, incremental_protection_enabled=False)
        p = mock.patch.object(breakeven, "_windows_username", return_value="mt5user")
        p.start(); self.addCleanup(p.stop)
        self._n = 0

    def _plan(self, *, source=TI, direction="SELL", entry="4038", sl="4043",
              tps=("4036", "4034", "4032"), states=("open", "open", "open")):
        """states[i] ∈ {'open','tp'(closed at its TP=profit),'loss'(closed at SL),<price str>}."""
        self._n += 1
        mid = f"m{self._n}"
        appr = PendingSignalApproval.objects.create(
            source=source, message_id=mid, symbol="XAUUSD", direction=direction, stop_loss=sl,
            take_profits=list(tps), status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=self.acct, source=source, message_id=mid, symbol="XAUUSD",
            direction=direction, stop_loss=sl, is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PROMOTED)
        now = timezone.now()
        legs = []
        for i, (tp, st) in enumerate(zip(tps, states), start=1):
            leg = ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit=tp, stop_loss=sl, lot_size=Decimal("0.40"),
                status=ProposedOrderLeg.Status.PROMOTED)
            ct, cp = (None, None)
            if st == "tp":
                ct, cp = now, Decimal(tp)
            elif st == "loss":
                ct, cp = now, Decimal(sl)
            elif st != "open":
                ct, cp = now, Decimal(st)
            Trade.objects.create(
                account=self.acct, symbol="XAUUSD", side=direction, volume=Decimal("0.40"),
                ticket=f"pos{plan.id}{i}", open_time=now, open_price=Decimal(entry),
                close_time=ct, close_price=cp, comment=f"WAY{plan.id}L{i}")
            legs.append(leg)
        return plan, legs

    def _modify_jobs(self, stage=None):
        qs = ExecutionJob.objects.filter(job_type="MODIFY_POSITION")
        return [j for j in qs if stage is None or (j.payload or {}).get("protection_stage") == stage]

    def _mkjob(self, status, stage, payload_extra=None, result=None, lease_expires_at=None):
        return ExecutionJob.objects.create(
            job_type="MODIFY_POSITION", account=self.acct, status=status,
            payload={"protection_stage": stage, **(payload_extra or {})},
            result=result or {}, lease_expires_at=lease_expires_at)

    def _age_close(self, plan, leg_index, seconds):
        """Backdate a leg's Trade close_time so its trigger looks stale (overdue tests)."""
        t = Trade.objects.get(account=self.acct, comment=f"WAY{plan.id}L{leg_index}")
        t.close_time = timezone.now() - timedelta(seconds=seconds)
        t.save(update_fields=["close_time"])


class Stage1BreakevenTests(Base):
    def test_sell_tp1_moves_remaining_to_breakeven(self):
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "open"))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 2)
        jobs = self._modify_jobs("BREAKEVEN")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].payload["sl"], 4038.0)          # each leg's own entry
        self.assertIn("tp", jobs[0].payload)                     # leg TP preserved

    def test_buy_tp1_moves_remaining_to_breakeven(self):
        plan, legs = self._plan(direction="BUY", entry="4005", sl="4000",
                                tps=("4010", "4020", "4030"), states=("tp", "open", "open"))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 2)
        self.assertEqual(self._modify_jobs("BREAKEVEN")[0].payload["sl"], 4005.0)


class Stage2Tp2LockTests(Base):
    def test_sell_tp2_moves_tp3_to_tp2_price(self):
        plan, legs = self._plan(direction="SELL", states=("tp", "tp", "open"))
        legs[2].protection_stage = "BREAKEVEN"          # TP3 already at breakeven (state-1 done)
        legs[2].save(update_fields=["protection_stage"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 1)
        j = self._modify_jobs("TP2_LOCKED")[0]
        self.assertEqual(j.payload["sl"], 4034.0)       # the planned TP2 price
        self.assertEqual(j.payload["leg_index"], 3)

    def test_buy_tp2_moves_tp3_to_tp2_price(self):
        plan, legs = self._plan(direction="BUY", entry="4005", sl="4000",
                                tps=("4010", "4020", "4030"), states=("tp", "tp", "open"))
        legs[2].protection_stage = "BREAKEVEN"
        legs[2].save(update_fields=["protection_stage"])
        with _enable():
            breakeven.sweep_breakeven()
        self.assertEqual(self._modify_jobs("TP2_LOCKED")[0].payload["sl"], 4020.0)

    def test_tp1_and_tp2_same_sweep_tp3_direct_to_tp2(self):
        # TP1 and TP2 both closed before the first sweep → TP3 goes DIRECTLY to TP2 (skips breakeven).
        plan, legs = self._plan(direction="SELL", states=("tp", "tp", "open"))  # leg3 stage INITIAL
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 1)
        self.assertFalse(self._modify_jobs("BREAKEVEN"))         # never wastes a breakeven cycle
        self.assertEqual(self._modify_jobs("TP2_LOCKED")[0].payload["sl"], 4034.0)

    def test_wayond_state2_disabled_stays_breakeven(self):
        # Wayond is NOT opted into TP2-lock → TP2 close leaves TP3 at breakeven (unchanged behaviour).
        plan, legs = self._plan(source=WAY, direction="SELL", states=("tp", "tp", "open"))
        legs[2].protection_stage = "BREAKEVEN"
        legs[2].save(update_fields=["protection_stage"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)                     # no TP2-lock for Wayond
        self.assertFalse(self._modify_jobs("TP2_LOCKED"))


class ProfitGatingTests(Base):
    def test_tp1_closed_at_loss_no_breakeven(self):
        self._plan(direction="SELL", states=("loss", "open", "open"))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)

    def test_tp2_closed_at_loss_no_tp2_lock(self):
        plan, legs = self._plan(direction="SELL", states=("tp", "loss", "open"))
        legs[2].protection_stage = "BREAKEVEN"
        legs[2].save(update_fields=["protection_stage"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertFalse(self._modify_jobs("TP2_LOCKED"))        # TP2 not profitable → no advance

    def test_tp3_already_closed_no_modification(self):
        self._plan(direction="SELL", states=("tp", "tp", "tp"))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)


class MonotonicityTests(Base):
    def test_no_downgrade_when_already_more_protective(self):
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "open"))
        legs[2].protection_stage = "TP2_LOCKED"          # already beyond breakeven
        legs[2].save(update_fields=["protection_stage"])
        with _enable():
            breakeven.sweep_breakeven()
        # leg3 not re-touched; only leg2 (still INITIAL) gets breakeven
        self.assertEqual(len([j for j in self._modify_jobs() if j.payload["leg_index"] == 3]), 0)

    def test_exact_target_already_applied_is_noop(self):
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "open"))
        for lg in legs[1:]:
            lg.protection_stage = "BREAKEVEN"
            lg.save(update_fields=["protection_stage"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)

    def test_duplicate_invocation_no_duplicate_job(self):
        self._plan(direction="SELL", states=("tp", "open", "open"))
        with _enable():
            breakeven.sweep_breakeven()
            n1 = ExecutionJob.objects.filter(job_type="MODIFY_POSITION").count()
            breakeven.sweep_breakeven()
            n2 = ExecutionJob.objects.filter(job_type="MODIFY_POSITION").count()
        self.assertEqual(n1, n2)


class RetryRecoveryTests(Base):
    def test_success_job_marks_stage_without_new_job(self):
        # Worker succeeded (job SUCCESS) but the DB stage wasn't set (crash before) → reconcile sets it.
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "open"))
        j = self._mkjob("SUCCESS", "BREAKEVEN")
        legs[1].breakeven_job = j; legs[1].breakeven_attempts = 1
        legs[1].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        legs[1].refresh_from_db()
        self.assertEqual(legs[1].protection_stage, "BREAKEVEN")
        self.assertGreaterEqual(res["applied"], 1)

    def test_exhausted_retries_alert(self):
        from reliability.models import AlertEvent
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "tp"))  # isolate leg2
        j = self._mkjob("FAILED", "BREAKEVEN")
        legs[1].breakeven_job = j
        legs[1].breakeven_attempts = breakeven.MAX_BREAKEVEN_ATTEMPTS
        legs[1].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["alerted"], 1)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"tp_protection_failed:plan:{plan.id}:leg:2:BREAKEVEN", status="OPEN").exists())


class IsolationValidationTests(Base):
    def test_leg_without_trade_skipped(self):
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "open"))
        Trade.objects.filter(account=self.acct, comment=f"WAY{plan.id}L2").delete()
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 1)   # only leg3 (leg2 has no trade)

    def test_inflight_guard_account_scoped(self):
        # A MODIFY for the same ticket on another account must not suppress this plan's modify.
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "open"))
        other = TradingAccount.objects.create(
            user=self.user, name="Other", account_number="BE2", is_demo=True)
        t2 = Trade.objects.get(account=self.acct, comment=f"WAY{plan.id}L2")
        ExecutionJob.objects.create(job_type="MODIFY_POSITION", account=other, status="PENDING",
            payload={"ticket": t2.ticket, "protection_stage": "BREAKEVEN"})
        with _enable():
            breakeven.sweep_breakeven()
        self.assertTrue(ExecutionJob.objects.filter(
            job_type="MODIFY_POSITION", account=self.acct, payload__ticket=t2.ticket).exists())

    def test_disabled_is_noop(self):
        self._plan(direction="SELL", states=("tp", "open", "open"))
        res = breakeven.sweep_breakeven()
        self.assertEqual(res, {"enabled": False})


class DetectionTests(Base):
    def test_periodic_sync_enqueued_and_deduped(self):
        self._plan(direction="SELL", states=("open", "open", "open"))
        with _enable():
            r1 = breakeven.sweep_breakeven()
            r2 = breakeven.sweep_breakeven()
        self.assertEqual(r1["synced"], 1)
        self.assertEqual(r2["synced"], 0)
        self.assertEqual(ExecutionJob.objects.filter(job_type="SYNC_POSITIONS").count(), 1)


class AlreadyClosedNoopTests(Base):
    """Review F2 — a position_not_found no-op must NOT mark a still-open leg protected."""

    def test_already_closed_success_does_not_advance_stage(self):
        # leg3 closed (isolate leg2); leg2 still OPEN with a SUCCESS-but-already_closed modify job.
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "tp"))
        j = self._mkjob("SUCCESS", "BREAKEVEN", result={"ok": True, "already_closed": True})
        legs[1].breakeven_job = j
        legs[1].save(update_fields=["breakeven_job"])
        with _enable():
            res = breakeven.sweep_breakeven()
        legs[1].refresh_from_db()
        self.assertEqual(res["noop_closed"], 1)
        self.assertEqual(res["applied"], 0)
        self.assertEqual(legs[1].protection_stage, "INITIAL")   # never falsely marked protected
        self.assertEqual(res["enqueued"], 0)                    # and does not re-enqueue


class SoftDeferTests(Base):
    """Review F1 — a broker stops/freeze-band rejection is a retryable deferral, never a CRITICAL."""

    def test_retryable_failure_defers_without_alert_or_attempt_bump(self):
        from reliability.models import AlertEvent
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "tp"))  # isolate leg2
        j = self._mkjob("FAILED", "BREAKEVEN",
                        result={"ok": False, "error": "sl_within_stops_level", "retryable": True})
        legs[1].breakeven_job = j
        legs[1].breakeven_attempts = breakeven.MAX_BREAKEVEN_ATTEMPTS   # already at the cap
        legs[1].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        legs[1].refresh_from_db()
        self.assertEqual(res["deferred"], 1)
        self.assertEqual(res["alerted"], 0)                     # NOT paged despite being at the cap
        self.assertEqual(res["enqueued"], 1)                    # re-enqueued to try again
        self.assertEqual(legs[1].breakeven_attempts,
                         breakeven.MAX_BREAKEVEN_ATTEMPTS)       # attempts NOT marched forward
        self.assertFalse(AlertEvent.objects.filter(
            dedup_key=f"tp_protection_failed:plan:{plan.id}:leg:2:BREAKEVEN").exists())


class OverdueGatingTests(Base):
    """Review F3 — overdue WARN fires only for an ACTUAL stuck attempt, not a late-ingested close."""

    def test_no_overdue_on_first_sight_of_late_close(self):
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "tp"))  # isolate leg2
        self._age_close(plan, 1, breakeven.PROTECTION_OVERDUE_SECONDS + 60)     # TP1 closed long ago
        with _enable():
            res = breakeven.sweep_breakeven()                   # first ever sweep for this leg
        self.assertEqual(res["overdue"], 0)                     # not "stuck" — just detected now
        self.assertEqual(res["enqueued"], 1)                    # first modify enqueued healthily

    def test_overdue_when_same_stage_attempt_is_stuck(self):
        from reliability.models import AlertEvent
        plan, legs = self._plan(direction="SELL", states=("tp", "open", "tp"))  # isolate leg2
        self._age_close(plan, 1, breakeven.PROTECTION_OVERDUE_SECONDS + 60)
        j = self._mkjob("RUNNING", "BREAKEVEN")                 # a real in-flight attempt, stuck
        legs[1].breakeven_job = j
        legs[1].breakeven_attempts = 1
        legs[1].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["overdue"], 1)
        self.assertEqual(res["inflight"], 1)                    # still counted in-flight (no re-enqueue)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"tp_protection_overdue:plan:{plan.id}:leg:2:BREAKEVEN", status="OPEN").exists())


class OrphanModifyReclaimTests(Base):
    """Review F6 — a worker recycle mid-modify must not wedge the ladder; execution_health reclaims."""

    def test_lease_expired_running_modify_is_reclaimed(self):
        from execution import execution_health
        stale = self._mkjob("RUNNING", "BREAKEVEN",
                             lease_expires_at=timezone.now() - timedelta(seconds=120))
        fresh = self._mkjob("RUNNING", "BREAKEVEN",
                            lease_expires_at=timezone.now() + timedelta(seconds=120))
        res = execution_health.sweep_execution_health()
        stale.refresh_from_db(); fresh.refresh_from_db()
        self.assertEqual(res["reclaimed_modify"], 1)
        self.assertEqual(stale.status, "FAILED")               # dead orphan failed → sweep re-enqueues
        self.assertEqual(fresh.status, "RUNNING")              # live lease untouched


class TP2AlwaysWinsTests(Base):
    """GFX-PKT-POST-DEPLOY WS-A — TP2 must always win: a leg never REMAINS at breakeven once TP2 has
    closed, even across the monitor-cadence race where a breakeven was enqueued on an earlier tick."""

    def test_pending_breakeven_superseded_when_tp2_locks(self):
        # Earlier tick saw only TP1 → enqueued a breakeven for TP3 (still PENDING). Now TP2 has closed.
        plan, legs = self._plan(direction="SELL", states=("tp", "tp", "open"))
        be = self._mkjob("PENDING", "BREAKEVEN",
                         payload_extra={"ticket": f"pos{plan.id}3", "leg_index": 3})
        legs[2].breakeven_job = be
        legs[2].breakeven_attempts = 1
        legs[2].save(update_fields=["breakeven_job", "breakeven_attempts"])
        with _enable():
            res = breakeven.sweep_breakeven()
        be.refresh_from_db()
        self.assertEqual(be.status, "FAILED")                      # obsolete breakeven retired
        self.assertEqual((be.result or {}).get("superseded_by"), "TP2_LOCKED")
        self.assertGreaterEqual(res["superseded"], 1)
        tp2 = self._modify_jobs("TP2_LOCKED")
        self.assertEqual(len(tp2), 1)                              # TP3 locked to the TP2 price
        self.assertEqual(tp2[0].payload["sl"], 4034.0)
        self.assertEqual(tp2[0].payload["leg_index"], 3)

    def test_never_remains_at_breakeven_after_tp2_invariant(self):
        # Breakeven already applied to TP3, THEN TP2 closes → must advance to TP2_LOCKED (never left).
        plan, legs = self._plan(direction="SELL", states=("tp", "tp", "open"))
        legs[2].protection_stage = "BREAKEVEN"
        legs[2].save(update_fields=["protection_stage"])
        with _enable():
            breakeven.sweep_breakeven()
        j = self._modify_jobs("TP2_LOCKED")
        self.assertEqual(len(j), 1)
        self.assertEqual(j[0].payload["leg_index"], 3)

    def test_running_breakeven_not_force_cancelled(self):
        # A RUNNING breakeven (worker already claimed it) is NOT retired — the bridge refuse-widen
        # backstop + the monotonic TP2 lock handle it. Only PENDING breakevens are superseded.
        plan, legs = self._plan(direction="SELL", states=("tp", "tp", "open"))
        be = self._mkjob("RUNNING", "BREAKEVEN",
                         payload_extra={"ticket": f"pos{plan.id}3", "leg_index": 3})
        legs[2].breakeven_job = be
        legs[2].save(update_fields=["breakeven_job"])
        with _enable():
            res = breakeven.sweep_breakeven()
        be.refresh_from_db()
        self.assertEqual(be.status, "RUNNING")                     # untouched
        self.assertEqual(res["superseded"], 0)
        self.assertEqual(len(self._modify_jobs("TP2_LOCKED")), 1)  # TP2 lock still enqueued

    def test_rapid_all_three_tps_close_no_error_no_orphan(self):
        # Fast market: all three TPs close before any sweep. Nothing to protect → no jobs, no error.
        self._plan(direction="SELL", states=("tp", "tp", "tp"))
        with _enable():
            res = breakeven.sweep_breakeven()
        self.assertEqual(res["enqueued"], 0)
        self.assertEqual(ExecutionJob.objects.filter(job_type="MODIFY_POSITION").count(), 0)

    def test_superseded_pending_breakeven_only_for_same_ticket(self):
        # The supersede is account+ticket scoped — a breakeven for a DIFFERENT leg/ticket is untouched.
        plan, legs = self._plan(direction="SELL", states=("tp", "tp", "open"))
        other_be = self._mkjob("PENDING", "BREAKEVEN",
                               payload_extra={"ticket": "pos-unrelated", "leg_index": 2})
        with _enable():
            breakeven.sweep_breakeven()
        other_be.refresh_from_db()
        self.assertEqual(other_be.status, "PENDING")               # unrelated breakeven preserved
