"""Notification provenance (ADR-0067).

A GuvFX strategy-result notification (stakeholder card OR customer message) requires a VERIFIED same-account GuvFX
execution-job link. Externally-executed / historically-ingested / monitoring-only trades — the exact #46/#49 case
that produced false "Automated strategy / completed automatically" cards — must never notify. A Trade row is NOT
evidence of GuvFX execution. These tests prove the packet's seven required behaviours end-to-end.
"""
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model

from execution import close_monitor, outcome_router
from execution.models import NotificationCandidate, TradeOutcomeRecord
from execution.notifications.real_transport import RealTelegramTransport
from execution.tests_close_monitor import _trade, _linked_trade
from customer_notifications.event_sources import enqueue_trade_outcome
from customer_notifications.models import CustomerNotification
from trading.models import Trade, TradingAccount

User = get_user_model()


class NotificationProvenanceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="prov", email="prov@x.invalid", password="x")
        self.account = TradingAccount.objects.create(
            user=self.user, name="Acct", account_number="P1", is_demo=True, broker_name="DemoBroker")

    def _outcome_for(self, ticket):
        return TradeOutcomeRecord.objects.get(trade__ticket=ticket)

    # 1 + 3: historical/external backfill inserts rows but emits ZERO strategy notifications (either channel).
    def test_external_backfill_inserts_rows_but_notifies_zero(self):
        _trade(self.account, ticket="hb1", profit="40")      # external WIN, no GuvFX link
        _trade(self.account, ticket="hb2", profit="70")
        self.assertEqual(Trade.objects.count(), 2)           # rows inserted
        close_monitor.process_closed_trades()
        outcome_router.route_outcomes()
        self.assertEqual(NotificationCandidate.objects.count(), 0)          # no stakeholder card
        for t in ("hb1", "hb2"):
            self.assertIsNone(enqueue_trade_outcome(self._outcome_for(t).pk))  # no customer message
        self.assertEqual(CustomerNotification.objects.filter(
            event_type__in=[CustomerNotification.EventType.TRADE_CLOSED,
                            CustomerNotification.EventType.TRADE_UPDATED]).count(), 0)

    # 2: idempotent rerun inserts zero and sends zero.
    def test_idempotent_rerun_notifies_zero(self):
        _trade(self.account, ticket="hb3", profit="40")
        for _ in range(3):
            close_monitor.process_closed_trades()
            outcome_router.route_outcomes()
        self.assertEqual(TradeOutcomeRecord.objects.count(), 1)   # no duplicate outcome
        self.assertEqual(NotificationCandidate.objects.count(), 0)
        self.assertIsNone(enqueue_trade_outcome(self._outcome_for("hb3").pk))

    # 4: a genuinely linked GuvFX execution produces exactly one correctly-attributed, dispatch-eligible result.
    #    (The customer-channel linked path is covered end-to-end by customer_notifications.tests.)
    def test_linked_execution_is_one_candidate_correctly_attributed_and_dispatch_eligible(self):
        t, plan, job = _linked_trade(self.account, ticket="gx1", profit="55")
        close_monitor.process_closed_trades()
        rec = self._outcome_for("gx1")
        self.assertIsNotNone(rec.execution_job)
        self.assertTrue(rec.is_delivery_candidate)
        self.assertEqual(outcome_router.route_outcomes()["candidates"], 1)
        # Idempotent routing: re-running creates no duplicate candidate.
        outcome_router.route_outcomes()
        cand = NotificationCandidate.objects.get()                       # exactly one
        self.assertEqual(cand.outcome_record.trade.account_id, self.account.id)   # correct account attribution
        # Dispatch boundary ACCEPTS a linked candidate (gate passes the verified-link check; it only then reaches
        # the credential check — no real send here). It is NOT refused for provenance.
        result = RealTelegramTransport(token="", chat_id="").deliver(cand)
        self.assertNotIn("no verified GuvFX execution link", result.detail)

    # 5: cross-account linkage cannot notify (the legit other-account trade still notifies; the cross one does not).
    def test_cross_account_linkage_cannot_notify(self):
        other = TradingAccount.objects.create(
            user=self.user, name="Other", account_number="P2", is_demo=True, broker_name="DemoBroker")
        _t, plan, _job = _linked_trade(other, ticket="oa1", profit="40")   # legit WIN on the OTHER account
        t = _trade(self.account, ticket="xa1", profit="40")                # a trade on THIS account...
        t.comment = f"WAY{plan.id}L1"; t.save(update_fields=["comment"])   # ...pointing at the other account's plan
        close_monitor.process_closed_trades()
        outcome_router.route_outcomes()
        # The cross-account trade is NOT a candidate and cannot notify; only the legit other-account one may.
        self.assertFalse(NotificationCandidate.objects.filter(outcome_record__trade__ticket="xa1").exists())
        self.assertIsNone(enqueue_trade_outcome(self._outcome_for("xa1").pk))
        self.assertIsNone(self._outcome_for("xa1").execution_job)

    # 6: deposits / credits / balance rows never become trades, so they never notify.
    def test_balance_ops_never_become_trades_or_notify(self):
        from trading.position_ingest import build_positions_from_deals
        balance_deals = [
            {"ticket": "b1", "type": 2, "entry": 0, "position_id": 0, "profit": 500.0, "symbol": "", "time": 1},
            {"ticket": "c1", "type": 3, "entry": 0, "position_id": 0, "profit": 200.0, "symbol": "", "time": 2},
        ]
        self.assertEqual(build_positions_from_deals(balance_deals), [])   # no positions -> no Trade rows
        close_monitor.process_closed_trades()
        self.assertEqual(TradeOutcomeRecord.objects.count(), 0)

    # 7: the dispatch boundary refuses to transmit any card without a verified execution link (defence in depth),
    #    so the "Automated strategy / completed automatically" wording can never ride on an external trade.
    def test_dispatch_refuses_card_without_execution_link(self):
        t = _trade(self.account, ticket="w7", profit="40")
        rec = TradeOutcomeRecord.objects.create(
            trade=t, outcome=TradeOutcomeRecord.Outcome.WIN, net_pnl=Decimal("40"),
            is_delivery_candidate=True, correlation_id="", signal_source="", execution_job=None)
        outcome_router.route_outcomes()
        cand = NotificationCandidate.objects.get()
        result = RealTelegramTransport(token="x", chat_id="1").deliver(cand)
        self.assertFalse(result.transmitted)
        self.assertIn("no verified GuvFX execution link", result.detail)
