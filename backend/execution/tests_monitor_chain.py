"""E3-MONITOR-SCHEDULING — the post-trade monitor chain command.

Proves the safety contract the scheduling relies on: the chain (close-monitor -> outcome-router
-> dispatch) is safe to run every minute at current defaults — it processes only pre-existing
closed trades / outcome records / candidates, creates internal records ONLY, is idempotent,
places no order, sends no Telegram (dispatch dry-run, flag-gated), and publishes nothing to WIMS.
"""
import ast
import importlib
import os
import pathlib
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from execution.models import (
    ExecutionJob,
    NotificationCandidate,
    NotificationDelivery,
    TradeOutcomeRecord,
)
from trading.models import Trade, TradingAccount
from wims.models import ConsumptionContract

User = get_user_model()


def _run(**opts):
    out, err = StringIO(), StringIO()
    call_command("run_monitor_chain", stdout=out, stderr=err, **opts)
    return out.getvalue(), err.getvalue()


class MonitorChainBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
        )

    def _closed_trade(self, *, ticket, profit, comment=""):
        return Trade.objects.create(
            account=self.demo, ticket=ticket, symbol="EURUSD", side="BUY",
            volume=Decimal("0.01"), open_time=timezone.now(), open_price=Decimal("1.0850"),
            close_time=timezone.now(), close_price=Decimal("1.0900"),
            profit=Decimal(str(profit)), comment=comment, correlation_id="",
        )


class MonitorChainSafetyTests(MonitorChainBase):
    def test_empty_db_is_safe_noop(self):
        # No closed trades at all → the chain runs cleanly, all zeros, no error.
        out, err = _run()
        self.assertIn("monitor-chain:", out)
        self.assertIn("processed=0", out)
        self.assertIn("failures=none", err + out)
        self.assertEqual(TradeOutcomeRecord.objects.count(), 0)
        self.assertEqual(NotificationCandidate.objects.count(), 0)

    def test_chain_creates_no_order_or_wims_contract(self):
        # A full pass over a closed WIN trade must create ZERO orders and ZERO WIMS contracts.
        jobs_before = ExecutionJob.objects.count()
        contracts_before = ConsumptionContract.objects.count()
        self._closed_trade(ticket="w1", profit=15)
        _run()
        self.assertEqual(ExecutionJob.objects.count(), jobs_before)       # no order job
        self.assertEqual(ConsumptionContract.objects.count(), contracts_before)  # no WIMS
        self.assertEqual(NotificationDelivery.objects.count(), 0)         # nothing dispatched

    def test_dispatch_is_noop_when_flag_off(self):
        # Default (flag off): a WIN produces a PENDING candidate that is NOT dispatched.
        self._closed_trade(ticket="w2", profit=20)
        out, _ = _run()
        self.assertIn("dispatch[enabled=False", out)
        cand = NotificationCandidate.objects.get()
        self.assertEqual(cand.status, NotificationCandidate.Status.PENDING)
        self.assertEqual(NotificationDelivery.objects.count(), 0)

    @mock.patch.dict(os.environ, {"NOTIFICATION_DISPATCH_ENABLED": "true"})
    def test_dispatch_transmits_nothing_even_when_enabled(self):
        # Even with the flag ON the transport is dry-run: the candidate is marked SENT but the
        # delivery audit records transmitted=False — no Telegram message ever leaves the process.
        self._closed_trade(ticket="w3", profit=25)
        out, _ = _run()
        self.assertIn("dispatch[enabled=True", out)
        cand = NotificationCandidate.objects.get()
        self.assertEqual(cand.status, NotificationCandidate.Status.SENT)
        delivery = NotificationDelivery.objects.get()
        self.assertFalse(delivery.transmitted)  # dry-run — nothing transmitted


class MonitorChainPipelineTests(MonitorChainBase):
    def test_single_pass_flows_close_to_outcome_in_order(self):
        # A candidate can only exist if the outcome record existed when route_outcomes ran, so
        # its presence after ONE invocation proves close-monitor ran before outcome-router.
        self._closed_trade(ticket="w4", profit=30)
        _run()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.outcome, TradeOutcomeRecord.Outcome.WIN)
        cand = NotificationCandidate.objects.get()
        self.assertEqual(cand.outcome_record_id, rec.id)

    def test_loss_makes_outcome_but_no_candidate(self):
        self._closed_trade(ticket="l1", profit=-8)
        _run()
        rec = TradeOutcomeRecord.objects.get()
        self.assertEqual(rec.outcome, TradeOutcomeRecord.Outcome.LOSS)
        self.assertEqual(NotificationCandidate.objects.count(), 0)  # losers never notify

    def test_chain_is_idempotent(self):
        self._closed_trade(ticket="w5", profit=12)
        _run()
        _run()  # second pass must not duplicate anything
        self.assertEqual(TradeOutcomeRecord.objects.count(), 1)
        self.assertEqual(NotificationCandidate.objects.count(), 1)


class MonitorChainResilienceTests(MonitorChainBase):
    _MOD = "execution.management.commands.run_monitor_chain"

    def test_one_step_failure_does_not_block_the_others(self):
        # If close-monitor raises, the outcome-router + dispatcher still run and the chain reports
        # the failure without aborting (each step is independent + idempotent).
        boom = mock.Mock(side_effect=RuntimeError("db blip"))
        with mock.patch(f"{self._MOD}.process_closed_trades", boom):
            out, err = _run()
        self.assertIn("STEP FAILED name=close_monitor", err)
        self.assertIn("failures=close_monitor", out)
        self.assertIn("outcome[routed=", out)  # later steps still ran

    def test_fail_fast_reraises(self):
        boom = mock.Mock(side_effect=RuntimeError("db blip"))
        with mock.patch(f"{self._MOD}.process_closed_trades", boom):
            with self.assertRaises(CommandError):
                _run(fail_fast=True)


class MonitorChainStaticBoundaryTests(TestCase):
    def test_command_makes_no_order_telegram_or_wims_reference(self):
        # Static: the orchestration command references no order/transmit/WIMS surface itself —
        # it only wires the three vetted functions together.
        src = pathlib.Path(
            importlib.import_module(
                "execution.management.commands.run_monitor_chain"
            ).__file__
        ).read_text()
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for n in node.names:
                    names.add(n.asname or n.name)
                if node.module:
                    names.add(node.module.split(".")[0])
        for forbidden in ("order_send", "order_check", "MetaTrader5", "mt5", "requests",
                          "httpx", "urllib", "create_place_order_job", "create_open_trade_job",
                          "agent_order", "ConsumptionContract", "wims", "deliver_trade_result"):
            self.assertNotIn(forbidden, names, f"monitor_chain references {forbidden}")
