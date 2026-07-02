"""
GFX-PKT-E3-RUNTIME-RISK-CONTROLS tests.

Each promotion-time control blocks (with a persisted PROMOTION_REJECTED audit)
when its limit is exceeded, a clean within-spec plan still promotes, the evaluator
is FAIL-CLOSED on indeterminate state, and the worker refuses a stale shadow job
before validation (no order_check, no order). All shadow — no order is ever placed.
"""

from decimal import Decimal
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from execution import risk_controls
from execution import signal_promotion as promo
from execution.signal_promotion import PromotionRejected, promote_plan_to_shadow_jobs
from execution.models import (
    ExecutionJob,
    ProposedOrderLeg,
    PromotionAuditEvent,
    SignalExecutionPlan,
    SignalSourceConfig,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount, Trade

User = get_user_model()
SRC = PendingSignalApproval.Source.WAYOND_TELEGRAM


class PromotionRiskControlTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True
        )
        SignalSourceConfig.objects.create(source=SRC, auto_demo_execution_enabled=True)

    def _plan(self, *, status=SignalExecutionPlan.Status.PLANNED, mid="p1",
              symbol="EURUSD", lots=("0.01", "0.01"), sl="1.0800"):
        approval = PendingSignalApproval.objects.create(
            source=SRC, message_id=mid, symbol=symbol, direction="BUY",
            stop_loss=sl, take_profits=["1.0900", "1.0950"],
            status=PendingSignalApproval.Status.APPROVED,
        )
        plan = SignalExecutionPlan.objects.create(
            approval=approval, account=self.demo, source=SRC, message_id=mid,
            symbol=symbol, direction="BUY", stop_loss=sl, is_demo=True,
            signal_timestamp=timezone.now(), status=status,
        )
        for i, lot in enumerate(lots, start=1):
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit="1.0900", stop_loss=sl,
                lot_size=Decimal(lot), status=ProposedOrderLeg.Status.PLANNED,
            )
        return plan

    _tk = 0

    def _open_trade(self, *, symbol="EURUSD", volume="0.01", closed=False, profit="0"):
        now = timezone.now()
        type(self)._tk += 1
        return Trade.objects.create(
            account=self.demo, symbol=symbol, side="BUY", volume=Decimal(volume),
            ticket=f"t{type(self)._tk}",
            open_time=now, close_time=(now if closed else None),
            open_price=Decimal("1.10"), profit=Decimal(profit),
        )

    def _reject_code(self, plan):
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_shadow_jobs(plan, actor=self.user)
        # every block writes a persisted PROMOTION_REJECTED audit (control 7)
        self.assertTrue(
            PromotionAuditEvent.objects.filter(
                plan=plan, event=PromotionAuditEvent.Event.PROMOTION_REJECTED
            ).exists()
        )
        return cm.exception.code

    def test_clean_account_promotes(self):
        plan = self._plan()
        jobs = promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j.job_type == ExecutionJob.JobType.PLACE_ORDER_SHADOW for j in jobs))

    def test_account_exposure_blocks(self):
        self._open_trade(symbol="GBPUSD", volume="0.10")  # account exposure at cap
        self.assertEqual(self._reject_code(self._plan()), "account_exposure_exceeded")

    def test_symbol_exposure_blocks(self):
        self._open_trade(symbol="EURUSD", volume="0.05")  # acct 0.05 ok, symbol 0.05+0.02>0.06
        self.assertEqual(self._reject_code(self._plan()), "symbol_exposure_exceeded")

    def test_max_open_positions_blocks(self):
        for _ in range(3):
            self._open_trade(symbol="GBPUSD", volume="0.01")  # 3 open, low exposure
        self.assertEqual(self._reject_code(self._plan()), "max_open_positions_reached")

    def test_daily_drawdown_blocks(self):
        self._open_trade(symbol="GBPUSD", volume="0.01", closed=True, profit="-150")
        self.assertEqual(self._reject_code(self._plan()), "daily_drawdown_hit")

    def test_concurrent_position_blocks(self):
        self._plan(status=SignalExecutionPlan.Status.PROMOTED, mid="other", lots=("0.01",))
        self.assertEqual(self._reject_code(self._plan(mid="p2")), "concurrent_position_limit")

    def test_fail_closed_on_indeterminate_state(self):
        plan = self._plan()
        with mock.patch.object(risk_controls, "_open_position_lots", side_effect=RuntimeError("db down")):
            self.assertEqual(
                risk_controls.evaluate_promotion_risk(plan, list(plan.legs.all())),
                "risk_state_indeterminate",
            )


class WorkerStalenessTests(SimpleTestCase):
    def setUp(self):
        import mt5_trade_ingest_worker as worker
        self.worker = worker

    def _job(self, **payload):
        base = {"symbol": "EURUSD", "side": "BUY", "lots": "0.01", "comment": "WAY1L1",
                "execution_mode": "SHADOW", "correlation_id": "c-stale"}
        base.update(payload)
        return {"id": 9, "job_type": "PLACE_ORDER_SHADOW", "account": 1,
                "created_at": "2026-07-01T00:00:00+00:00", "payload": base}

    def _run(self, job):
        with mock.patch.object(self.worker, "agent_order_check",
                               return_value={"ok": True, "retcode": 0}) as chk, \
             mock.patch.object(self.worker, "complete_job", return_value=(200, {})) as comp:
            res = self.worker.handle_shadow_job(job)
        return res, chk, comp

    def test_stale_signal_refused_before_order_check(self):
        old = "2020-01-01T00:00:00+00:00"  # far past → stale
        res, chk, comp = self._run(self._job(signal_timestamp=old))
        chk.assert_not_called()  # no order_check
        self.assertFalse(res["order_send_called"])
        self.assertEqual(res["error"], "stale_at_execution")
        self.assertEqual(comp.call_args.args[1], "FAILED")

    def test_fresh_signal_proceeds_to_order_check(self):
        from datetime import datetime, timezone as tz
        fresh = datetime.now(tz.utc).isoformat()
        res, chk, comp = self._run(self._job(signal_timestamp=fresh))
        chk.assert_called_once()
        self.assertTrue(res["ok"])

    def test_missing_timestamp_unaffected(self):
        res, chk, comp = self._run(self._job())  # no signal_timestamp
        chk.assert_called_once()
