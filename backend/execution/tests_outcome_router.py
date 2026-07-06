"""PROFIT-NOTIFICATION-FOUNDATION — outcome-router routing, candidates, idempotency, boundary.

Proves: WIN → a PENDING NotificationCandidate (correlation preserved); LOSS/BREAKEVEN → no
candidate; idempotent (no duplicate candidate, no re-route); only unrouted processed; the
router references no Telegram/WIMS/order path.
"""
import ast
import pathlib
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution import outcome_router
from execution.models import NotificationCandidate, TradeOutcomeRecord
from trading.models import Trade, TradingAccount

User = get_user_model()
Outcome = TradeOutcomeRecord.Outcome


class OutcomeRouterBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.account = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
        )
        self._n = 0

    def _record(self, *, outcome, net_pnl, cid="", source="", is_candidate=None, routed=False):
        self._n += 1
        trade = Trade.objects.create(
            account=self.account, ticket=f"t{self._n}", symbol="EURUSD", side="BUY",
            volume=Decimal("0.01"), open_time=timezone.now(), open_price=Decimal("1.0850"),
            close_time=timezone.now(), close_price=Decimal("1.0900"),
            profit=Decimal(str(net_pnl)),
        )
        if is_candidate is None:
            is_candidate = (outcome == Outcome.WIN)
        return TradeOutcomeRecord.objects.create(
            trade=trade, outcome=outcome, net_pnl=Decimal(str(net_pnl)),
            is_delivery_candidate=is_candidate, correlation_id=cid, signal_source=source,
            routed=routed,
        )


class RoutingTests(OutcomeRouterBase):
    def test_win_creates_pending_candidate(self):
        rec = self._record(outcome=Outcome.WIN, net_pnl="12")
        counts = outcome_router.route_outcomes()
        cand = NotificationCandidate.objects.get()
        self.assertEqual(cand.outcome_record_id, rec.id)
        self.assertEqual(cand.status, NotificationCandidate.Status.PENDING)
        self.assertEqual(cand.net_pnl, Decimal("12"))
        rec.refresh_from_db()
        self.assertTrue(rec.routed)
        self.assertEqual(counts, {"routed": 1, "candidates": 1, "internal_only": 0})

    def test_loss_creates_no_candidate(self):
        rec = self._record(outcome=Outcome.LOSS, net_pnl="-4")
        outcome_router.route_outcomes()
        self.assertEqual(NotificationCandidate.objects.count(), 0)
        rec.refresh_from_db()
        self.assertTrue(rec.routed)

    def test_breakeven_creates_no_candidate(self):
        rec = self._record(outcome=Outcome.BREAKEVEN, net_pnl="0")
        outcome_router.route_outcomes()
        self.assertEqual(NotificationCandidate.objects.count(), 0)
        rec.refresh_from_db()
        self.assertTrue(rec.routed)

    def test_win_without_delivery_flag_no_candidate(self):
        # Defensive edge: a WIN whose is_delivery_candidate is False → no candidate.
        self._record(outcome=Outcome.WIN, net_pnl="5", is_candidate=False)
        counts = outcome_router.route_outcomes()
        self.assertEqual(NotificationCandidate.objects.count(), 0)
        self.assertEqual(counts["internal_only"], 1)

    def test_correlation_preserved(self):
        self._record(outcome=Outcome.WIN, net_pnl="8", cid="corr-9", source="wayond")
        outcome_router.route_outcomes()
        cand = NotificationCandidate.objects.get()
        self.assertEqual(cand.correlation_id, "corr-9")
        self.assertEqual(cand.signal_source, "wayond")

    def test_mixed_batch_counts(self):
        self._record(outcome=Outcome.WIN, net_pnl="3")
        self._record(outcome=Outcome.WIN, net_pnl="7")
        self._record(outcome=Outcome.LOSS, net_pnl="-2")
        self._record(outcome=Outcome.BREAKEVEN, net_pnl="0")
        counts = outcome_router.route_outcomes()
        self.assertEqual(counts, {"routed": 4, "candidates": 2, "internal_only": 2})
        self.assertEqual(NotificationCandidate.objects.count(), 2)


class IdempotencyTests(OutcomeRouterBase):
    def test_no_duplicate_candidate_on_rerun(self):
        self._record(outcome=Outcome.WIN, net_pnl="6")
        outcome_router.route_outcomes()
        n = NotificationCandidate.objects.count()
        second = outcome_router.route_outcomes()  # re-run
        outcome_router.route_outcomes()           # and again
        self.assertEqual(NotificationCandidate.objects.count(), n)
        self.assertEqual(n, 1)
        self.assertEqual(second["routed"], 0)  # nothing left to route

    def test_already_routed_not_reprocessed(self):
        # A WIN already marked routed must not get a candidate.
        self._record(outcome=Outcome.WIN, net_pnl="9", routed=True)
        counts = outcome_router.route_outcomes()
        self.assertEqual(NotificationCandidate.objects.count(), 0)
        self.assertEqual(counts["routed"], 0)


class BoundaryAndCommandTests(OutcomeRouterBase):
    def test_router_has_no_forbidden_references(self):
        src = pathlib.Path(outcome_router.__file__).read_text()
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for token in ("order_send", "order_check", "agent_order", "deliver_trade_result",
                      "ingest_winning_trade", "ConsumptionContract", "send_message",
                      "sendMessage", "ExecutionJob"):
            self.assertNotIn(token, names, f"outcome_router must not reference {token!r}")
        # No transport/publish module is imported (check imports, not the docstring).
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
        for mod in ("wims", "telegram", "intelligence", "requests", "urllib"):
            self.assertNotIn(mod, imported, f"outcome_router must not import {mod!r}")

    def test_command_runs_and_creates_candidate(self):
        self._record(outcome=Outcome.WIN, net_pnl="4")
        call_command("run_outcome_router")
        self.assertEqual(NotificationCandidate.objects.count(), 1)
