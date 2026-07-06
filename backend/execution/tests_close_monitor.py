"""AUTO-SHADOW-CLOSE-MONITOR — close detection, classification, idempotency, boundary.

Proves: open trades ignored; WIN/LOSS/BREAKEVEN classified from net pnl; WIN → internal
delivery candidate only; LOSS/BREAKEVEN → internal only; no duplicate delivery; corrupt/
incomplete trades fail closed; signal linkage preserved where available; no order/Telegram/
WIMS side effect.
"""
import ast
import pathlib
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution import close_monitor
from execution.models import (
    ExecutionJob,
    ProposedOrderLeg,
    SignalExecutionPlan,
    TradeOutcomeRecord,
)
from signal_intake.models import PendingSignalApproval
from trading.models import Trade, TradingAccount
from wims.models import ConsumptionContract

User = get_user_model()


def _trade(account, *, ticket, profit="0", commission="0", swap="0",
           closed=True, close_price="1.0900", cid=""):
    return Trade.objects.create(
        account=account, ticket=ticket, symbol="EURUSD", side="BUY",
        volume=Decimal("0.01"), open_time=timezone.now(), open_price=Decimal("1.0850"),
        close_time=(timezone.now() if closed else None),
        close_price=(Decimal(close_price) if close_price is not None else None),
        profit=Decimal(profit), commission=Decimal(commission), swap=Decimal(swap),
        correlation_id=cid,
    )


class CloseMonitorBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.account = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
        )


class ClassificationTests(CloseMonitorBase):
    def test_open_trade_ignored(self):
        _trade(self.account, ticket="t1", profit="10", closed=False, close_price=None)
        counts = close_monitor.process_closed_trades()
        self.assertEqual(TradeOutcomeRecord.objects.count(), 0)
        self.assertEqual(counts["processed"], 0)

    def test_win_classified(self):
        _trade(self.account, ticket="t2", profit="10")
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.outcome, TradeOutcomeRecord.Outcome.WIN)
        self.assertEqual(rec.net_pnl, Decimal("10"))
        self.assertTrue(rec.is_delivery_candidate)
        self.assertFalse(rec.delivered)

    def test_loss_classified(self):
        _trade(self.account, ticket="t3", profit="-5")
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.outcome, TradeOutcomeRecord.Outcome.LOSS)
        self.assertFalse(rec.is_delivery_candidate)

    def test_breakeven_classified(self):
        _trade(self.account, ticket="t4", profit="0")
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.outcome, TradeOutcomeRecord.Outcome.BREAKEVEN)
        self.assertFalse(rec.is_delivery_candidate)

    def test_net_pnl_includes_commission_and_swap(self):
        # profit +10 but commission -12 → net -2 → LOSS (proves net = profit+comm+swap).
        _trade(self.account, ticket="t5", profit="10", commission="-12")
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.net_pnl, Decimal("-2"))
        self.assertEqual(rec.outcome, TradeOutcomeRecord.Outcome.LOSS)


class RoutingTests(CloseMonitorBase):
    def test_win_is_internal_delivery_candidate_only(self):
        _trade(self.account, ticket="w1", profit="25")
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get()
        self.assertTrue(rec.is_delivery_candidate)
        self.assertFalse(rec.delivered)          # nothing delivered here
        self.assertEqual(ConsumptionContract.objects.count(), 0)  # no WIMS publish

    def test_loss_breakeven_internal_only(self):
        _trade(self.account, ticket="l1", profit="-3")
        _trade(self.account, ticket="b1", profit="0")
        close_monitor.process_closed_trades()
        self.assertEqual(TradeOutcomeRecord.objects.filter(is_delivery_candidate=True).count(), 0)
        self.assertEqual(ConsumptionContract.objects.count(), 0)


class IdempotencyTests(CloseMonitorBase):
    def test_no_duplicate_delivery(self):
        _trade(self.account, ticket="i1", profit="7")
        close_monitor.process_closed_trades()
        n = TradeOutcomeRecord.objects.count()
        close_monitor.process_closed_trades()  # re-run
        close_monitor.process_closed_trades()  # re-run again
        self.assertEqual(TradeOutcomeRecord.objects.count(), n)
        self.assertEqual(n, 1)


class FailClosedTests(CloseMonitorBase):
    def test_incomplete_close_fails_closed(self):
        # close_time set but close_price missing → producer raises → skipped, no record.
        _trade(self.account, ticket="c1", profit="10", close_price=None)
        counts = close_monitor.process_closed_trades()
        self.assertEqual(TradeOutcomeRecord.objects.count(), 0)
        self.assertEqual(counts["skipped"], 1)

    def test_linkage_error_skips_one_trade_not_batch(self):
        # A flaky linkage lookup must skip that one trade, not abort the whole run.
        _trade(self.account, ticket="ok1", profit="5")
        _trade(self.account, ticket="bad", profit="5", cid="boom")
        _trade(self.account, ticket="ok2", profit="5")
        orig = close_monitor._resolve_linkage

        def flaky(trade):
            if getattr(trade, "correlation_id", "") == "boom":
                raise RuntimeError("simulated db blip")
            return orig(trade)

        with mock.patch.object(close_monitor, "_resolve_linkage", side_effect=flaky):
            counts = close_monitor.process_closed_trades()
        self.assertEqual(TradeOutcomeRecord.objects.count(), 2)  # both good trades recorded
        self.assertEqual(counts["processed"], 2)
        self.assertEqual(counts["skipped"], 1)


class LinkageTests(CloseMonitorBase):
    def _plan(self, cid, *, with_job=False):
        approval = PendingSignalApproval.objects.create(
            source="wayond", message_id=f"m-{cid}", symbol="EURUSD", direction="BUY",
            entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900"], status=PendingSignalApproval.Status.APPROVED,
        )
        plan = SignalExecutionPlan.objects.create(
            approval=approval, account=self.account, source="wayond",
            symbol="EURUSD", direction="BUY", entry="1.0850", stop_loss="1.0800",
            total_lot=Decimal("0.01"), is_demo=True, correlation_id=cid,
        )
        if with_job:
            job = ExecutionJob.objects.create(
                job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW, account=self.account,
                status=ExecutionJob.Status.PENDING, payload={"execution_mode": "SHADOW"},
            )
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=1, take_profit="1.0900", stop_loss="1.0800",
                lot_size=Decimal("0.01"), execution_job=job,
            )
        return plan

    def test_linkage_preserved_from_matching_plan(self):
        self._plan("corr-123", with_job=True)
        _trade(self.account, ticket="lk1", profit="9", cid="corr-123")
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.correlation_id, "corr-123")
        self.assertEqual(rec.signal_source, "wayond")
        self.assertIsNotNone(rec.execution_job)
        self.assertEqual(rec.execution_job.job_type, ExecutionJob.JobType.PLACE_ORDER_SHADOW)

    def test_correlation_id_preserved_without_plan(self):
        _trade(self.account, ticket="lk2", profit="9", cid="orphan")
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.correlation_id, "orphan")
        self.assertEqual(rec.signal_source, "")
        self.assertIsNone(rec.execution_job)

    def test_unlinked_trade_still_recorded(self):
        _trade(self.account, ticket="lk3", profit="9")  # no correlation_id
        close_monitor.process_closed_trades()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.correlation_id, "")
        self.assertTrue(rec.is_delivery_candidate)  # it is a WIN, still recorded


class BoundaryAndCommandTests(CloseMonitorBase):
    def test_close_monitor_has_no_forbidden_references(self):
        src = pathlib.Path(close_monitor.__file__).read_text()
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for token in ("order_send", "order_check", "agent_order", "deliver_trade_result",
                      "ingest_winning_trade", "ConsumptionContract", "send_message",
                      "sendMessage"):
            self.assertNotIn(token, names, f"close_monitor must not reference {token!r}")
        # No wims import.
        self.assertNotIn("wims", src.split("logger =")[0])

    def test_command_runs_and_records(self):
        _trade(self.account, ticket="cmd1", profit="4")
        call_command("run_close_monitor")
        self.assertEqual(TradeOutcomeRecord.objects.filter(
            outcome=TradeOutcomeRecord.Outcome.WIN).count(), 1)
