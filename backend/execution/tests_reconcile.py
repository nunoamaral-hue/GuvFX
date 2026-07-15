"""WS-C NOTIFICATION RECONCILIATION — reconcile_notifications() tests.

Cover: mark-delivered (dead field revived), backfill a missing candidate, revive the dry-run
"SENT" trap ONLY under the real transport (and never a genuinely-transmitted one), and the
deduped stuck-winner alert (fires only past the age threshold, never for a delivered winner).
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.notifications import reconcile
from execution.models import (
    NotificationCandidate, NotificationDelivery, TradeOutcomeRecord,
)
from trading.models import Trade, TradingAccount

User = get_user_model()


class ReconcileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rc", email="rc@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="RC1", is_demo=True)
        self._n = 0

    def _win(self, *, candidate=True, transmitted=False, sent=False, age_seconds=0,
             is_delivery_candidate=True):
        self._n += 1
        trade = Trade.objects.create(
            account=self.acct, symbol="XAUUSD", side="BUY", volume=Decimal("0.40"),
            ticket=f"t{self._n}", open_time=timezone.now(), open_price=Decimal("4005"),
            close_time=timezone.now(), close_price=Decimal("4010"), comment=f"WAY{self._n}L1")
        rec = TradeOutcomeRecord.objects.create(
            trade=trade, outcome=TradeOutcomeRecord.Outcome.WIN, net_pnl=Decimal("12.00"),
            is_delivery_candidate=is_delivery_candidate, correlation_id=f"c{self._n}",
            signal_source="ti_signals", routed=True)
        if age_seconds:
            past = timezone.now() - timezone.timedelta(seconds=age_seconds)
            TradeOutcomeRecord.objects.filter(id=rec.id).update(created_at=past)
        cand = None
        if candidate:
            cand = NotificationCandidate.objects.create(
                outcome_record=rec, net_pnl=rec.net_pnl, correlation_id=rec.correlation_id,
                signal_source=rec.signal_source,
                status=(NotificationCandidate.Status.SENT if sent
                        else NotificationCandidate.Status.PENDING))
            if transmitted:
                NotificationDelivery.objects.create(
                    candidate=cand, transport="telegram-real",
                    result=NotificationDelivery.Result.SENT, transmitted=True, attempt=1)
        return rec, cand

    # (A) mark-delivered ------------------------------------------------------
    def test_marks_delivered_when_transmitted(self):
        rec, _ = self._win(candidate=True, transmitted=True, sent=True)
        self.assertFalse(rec.delivered)
        counts = reconcile.reconcile_notifications()
        rec.refresh_from_db()
        self.assertTrue(rec.delivered)
        self.assertEqual(counts["marked_delivered"], 1)

    # (B) backfill ------------------------------------------------------------
    def test_backfills_missing_candidate(self):
        rec, _ = self._win(candidate=False)
        counts = reconcile.reconcile_notifications()
        self.assertEqual(counts["backfilled"], 1)
        cand = NotificationCandidate.objects.get(outcome_record=rec)
        self.assertEqual(cand.status, NotificationCandidate.Status.PENDING)
        self.assertEqual(cand.net_pnl, rec.net_pnl)

    def test_backfill_idempotent(self):
        self._win(candidate=False)
        reconcile.reconcile_notifications()
        counts2 = reconcile.reconcile_notifications()
        self.assertEqual(counts2["backfilled"], 0)  # already has a candidate now

    # (C) revive dry-run "SENT" trap -----------------------------------------
    def test_revive_sent_untransmitted_only_with_real_transport(self):
        rec, cand = self._win(candidate=True, transmitted=False, sent=True)
        with mock.patch.object(reconcile, "_real_transport_active", return_value=False):
            c_off = reconcile.reconcile_notifications()
        cand.refresh_from_db()
        self.assertEqual(cand.status, NotificationCandidate.Status.SENT)  # not revived in dry-run
        self.assertEqual(c_off["revived"], 0)
        with mock.patch.object(reconcile, "_real_transport_active", return_value=True):
            c_on = reconcile.reconcile_notifications()
        cand.refresh_from_db()
        self.assertEqual(cand.status, NotificationCandidate.Status.PENDING)  # revived → redispatch
        self.assertEqual(c_on["revived"], 1)

    def test_never_revives_a_transmitted_candidate(self):
        rec, cand = self._win(candidate=True, transmitted=True, sent=True)
        with mock.patch.object(reconcile, "_real_transport_active", return_value=True):
            counts = reconcile.reconcile_notifications()
        cand.refresh_from_db()
        self.assertEqual(cand.status, NotificationCandidate.Status.SENT)  # already delivered → left
        self.assertEqual(counts["revived"], 0)

    # (D) stuck-winner alert --------------------------------------------------
    def test_alerts_on_stuck_undelivered_winner(self):
        from reliability.models import AlertEvent
        rec, _ = self._win(candidate=True, transmitted=False, sent=False,
                           age_seconds=reconcile.UNDELIVERED_ALERT_SECONDS + 60)
        counts = reconcile.reconcile_notifications()
        self.assertEqual(counts["alerted"], 1)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"notify_undelivered:outcome:{rec.id}", status=AlertEvent.Status.OPEN).exists())
        # deduped: a second pass does not create another alert
        reconcile.reconcile_notifications()
        self.assertEqual(AlertEvent.objects.filter(
            dedup_key=f"notify_undelivered:outcome:{rec.id}").count(), 1)

    def test_no_alert_for_fresh_undelivered(self):
        self._win(candidate=True, transmitted=False, sent=False, age_seconds=10)
        counts = reconcile.reconcile_notifications()
        self.assertEqual(counts["alerted"], 0)

    def test_no_alert_when_transmitted(self):
        self._win(candidate=True, transmitted=True, sent=True,
                  age_seconds=reconcile.UNDELIVERED_ALERT_SECONDS + 60)
        counts = reconcile.reconcile_notifications()
        self.assertEqual(counts["alerted"], 0)
