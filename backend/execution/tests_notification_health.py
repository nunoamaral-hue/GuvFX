"""WS-B4/B2 — notification-health rollup alert + Telegram message-id capture."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.notifications import reconcile
from execution.notifications.dispatcher import _persist_transmission
from execution.notifications.transport import DeliveryResult
from execution.models import (
    NotificationCandidate, NotificationDelivery, TradeOutcomeRecord,
)
from trading.models import Trade, TradingAccount

User = get_user_model()


def _outcome(acct, n, *, delivered=False):
    trade = Trade.objects.create(
        account=acct, symbol="XAUUSD", side="BUY", volume=Decimal("0.40"),
        ticket=f"t{n}", open_time=timezone.now(), open_price=Decimal("4005"),
        close_time=timezone.now(), close_price=Decimal("4010"), comment=f"WAY{n}L1")
    return TradeOutcomeRecord.objects.create(
        trade=trade, outcome=TradeOutcomeRecord.Outcome.WIN, net_pnl=Decimal("12"),
        is_delivery_candidate=True, delivered=delivered, correlation_id=f"c{n}",
        signal_source="ti_signals", routed=True)


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="nh", email="nh@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="NH1", is_demo=True)
        self._n = 0

    def _cand(self, status, *, deliveries=0, transmitted=False, delivery_result="FAILED"):
        self._n += 1
        rec = _outcome(self.acct, self._n)
        cand = NotificationCandidate.objects.create(
            outcome_record=rec, net_pnl=rec.net_pnl, correlation_id=rec.correlation_id,
            signal_source=rec.signal_source, status=status)
        for i in range(deliveries):
            NotificationDelivery.objects.create(
                candidate=cand, transport="telegram-real",
                result=getattr(NotificationDelivery.Result, delivery_result),
                transmitted=transmitted, attempt=i + 1)
        return cand


class NotificationHealthTests(Base):
    def test_opens_and_resolves_on_persistent_transport_failure(self):
        from reliability.models import AlertEvent
        cand = self._cand(NotificationCandidate.Status.FAILED, deliveries=3)  # 3 failed attempts
        r1 = reconcile.check_notification_health()
        self.assertGreaterEqual(r1["issues"], 1)
        self.assertEqual(r1["alerted"], 1)
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key="notify_pipeline_health:global", status="OPEN").exists())
        # second pass while still unhealthy → no duplicate alert
        r2 = reconcile.check_notification_health()
        self.assertEqual(r2["alerted"], 0)
        self.assertEqual(AlertEvent.objects.filter(dedup_key="notify_pipeline_health:global").count(), 1)
        # heal it → the alert auto-resolves
        cand.status = NotificationCandidate.Status.SENT
        cand.save(update_fields=["status"])
        NotificationDelivery.objects.create(candidate=cand, transport="telegram-real",
            result=NotificationDelivery.Result.SENT, transmitted=True, attempt=4)
        r3 = reconcile.check_notification_health()
        self.assertEqual(r3["resolved"], 1)
        self.assertFalse(AlertEvent.objects.filter(
            dedup_key="notify_pipeline_health:global", status="OPEN").exists())

    def test_healthy_pipeline_no_alert(self):
        from reliability.models import AlertEvent
        self._cand(NotificationCandidate.Status.SENT, deliveries=1, transmitted=True,
                   delivery_result="SENT")
        r = reconcile.check_notification_health()
        self.assertEqual(r["issues"], 0)
        self.assertFalse(AlertEvent.objects.filter(dedup_key="notify_pipeline_health:global").exists())


class UndeliveredAutoResolveTests(Base):
    def test_undelivered_alert_resolves_when_delivered(self):
        from reliability.models import AlertEvent
        rec = _outcome(self.acct, 99)
        cand = NotificationCandidate.objects.create(
            outcome_record=rec, net_pnl=rec.net_pnl, correlation_id=rec.correlation_id,
            signal_source=rec.signal_source, status=NotificationCandidate.Status.SENT)
        NotificationDelivery.objects.create(candidate=cand, transport="telegram-real",
            result=NotificationDelivery.Result.SENT, transmitted=True, attempt=1)
        # a stale OPEN undelivered alert from before it was delivered
        AlertEvent.objects.create(severity="WARN", component="EXECUTION_PIPELINE",
            title="WIN not notified", dedup_key=f"notify_undelivered:outcome:{rec.id}", status="OPEN")
        reconcile.reconcile_notifications()   # mark-delivered step must resolve it
        rec.refresh_from_db()
        self.assertTrue(rec.delivered)
        self.assertFalse(AlertEvent.objects.filter(
            dedup_key=f"notify_undelivered:outcome:{rec.id}", status="OPEN").exists())


class MessageIdCaptureTests(Base):
    def test_delivery_result_carries_message_id(self):
        dr = DeliveryResult(ok=True, status="SENT", transmitted=True, rendered_message="x",
                            message_id="4242")
        self.assertEqual(dr.message_id, "4242")

    def test_persist_transmission_stores_provider_message_id(self):
        cand = self._cand(NotificationCandidate.Status.PROCESSING)
        result = DeliveryResult(ok=True, status="SENT", transmitted=True, rendered_message="card",
                                detail="sent", message_id="777")
        _persist_transmission(cand, "telegram-real", 1, result)
        d = NotificationDelivery.objects.filter(candidate=cand, transmitted=True).first()
        self.assertIsNotNone(d)
        self.assertEqual(d.provider_message_id, "777")
