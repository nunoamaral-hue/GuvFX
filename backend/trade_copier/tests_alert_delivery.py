"""ADR-0074 Phase-9 — governed alert-DELIVERY outbox + dispatcher.

A copier alert raised while COPIER_ALERTS_ENABLED is on also enqueues ONE durable, idempotent outbox row. A
SEPARATE default-OFF dispatcher sends it through an injected transport that DEFAULTS to an inert dry-run with no
real destination. Mutation-killers: enqueue is idempotent (dedup_key); dispatch is inert while its flag is off
(rows stay PENDING — operator-visible backlog); a transmitting transport → SENT; a failing/dry-run transport →
FAILED then DEAD after max_attempts; every attempt writes an immutable audit row; the payload is sanitised.
Nothing is ever delivered to a real destination in this packet, and nothing recovers a trade.
"""
from django.test import TestCase, override_settings

from reliability.models import AlertEvent
from trade_copier import failure_alerts
from trade_copier.alert_delivery import (DeliveryOutcome, delivery_backlog, dispatch_pending, enqueue_delivery,
                                         select_alert_transport)
from trade_copier.models import CopierAlertDelivery, CopierAlertDeliveryAttempt

ALERTS = dict(COPIER_ENABLED=True, COPIER_ALERTS_ENABLED=True)
DISPATCH = dict(COPIER_ENABLED=True, COPIER_ALERTS_ENABLED=True, COPIER_ALERT_DISPATCH_ENABLED=True)


class _FakeTransport:
    name = "fake"

    def __init__(self, transmitted=True, retryable=True):
        self._transmitted = transmitted
        self._retryable = retryable
        self.calls = 0

    def deliver(self, delivery):
        self.calls += 1
        return DeliveryOutcome(transmitted=self._transmitted, retryable=self._retryable,
                               detail=("ok" if self._transmitted else "boom"), transport=self.name)


@override_settings(**ALERTS)
class EnqueueTests(TestCase):
    def test_raise_alert_enqueues_one_delivery(self):
        self.assertTrue(failure_alerts.raise_copier_alert(
            failure_alerts.CONTAINED_FAILURE, scope_key="lifecycle:1", reason="link_divergent"))
        self.assertEqual(CopierAlertDelivery.objects.count(), 1)
        row = CopierAlertDelivery.objects.get()
        self.assertEqual(row.status, CopierAlertDelivery.Status.PENDING)
        self.assertTrue(row.dedup_key.startswith("copier:CONTAINED_FAILURE:"))

    def test_enqueue_is_idempotent_on_dedup_key(self):
        alert = AlertEvent.objects.create(component="COPY_TRADING", severity=AlertEvent.Severity.WARN,
                                          title="t", dedup_key="copier:X:1", detail={})
        self.assertTrue(enqueue_delivery(alert, category="X", severity="WARN", payload={"reason": "r"}))
        self.assertFalse(enqueue_delivery(alert, category="X", severity="WARN", payload={"reason": "r"}))
        self.assertEqual(CopierAlertDelivery.objects.filter(dedup_key="copier:X:1").count(), 1)

    def test_payload_is_sanitised(self):
        alert = AlertEvent.objects.create(component="COPY_TRADING", severity=AlertEvent.Severity.WARN,
                                          title="t", dedup_key="copier:Y:1", detail={})
        enqueue_delivery(alert, category="Y", severity="WARN",
                         payload={"reason": "r", "ticket": 7, "secret": "SHOULD_NOT_APPEAR", "login": "1000001"})
        row = CopierAlertDelivery.objects.get(dedup_key="copier:Y:1")
        self.assertIn("reason", row.payload)
        self.assertEqual(row.payload.get("ticket"), 7)
        self.assertNotIn("secret", row.payload)
        self.assertNotIn("login", row.payload)


class DispatchGateTests(TestCase):
    def _queue(self):
        alert = AlertEvent.objects.create(component="COPY_TRADING", severity=AlertEvent.Severity.WARN,
                                          title="t", dedup_key="copier:Z:1", detail={})
        enqueue_delivery(alert, category="Z", severity="WARN", payload={"reason": "r"})

    @override_settings(COPIER_ENABLED=True)   # dispatch flag OFF
    def test_dispatch_off_leaves_pending(self):
        self._queue()
        out = dispatch_pending()
        self.assertFalse(out["enabled"])
        self.assertEqual(CopierAlertDelivery.objects.get().status, CopierAlertDelivery.Status.PENDING)
        self.assertEqual(delivery_backlog()["pending"], 1)

    @override_settings(**DISPATCH)
    def test_transmitting_transport_marks_sent(self):
        self._queue()
        t = _FakeTransport(transmitted=True)
        out = dispatch_pending(transport=t)
        self.assertEqual(out["sent"], 1)
        row = CopierAlertDelivery.objects.get()
        self.assertEqual(row.status, CopierAlertDelivery.Status.SENT)
        self.assertIsNotNone(row.delivered_at)
        self.assertEqual(CopierAlertDeliveryAttempt.objects.filter(delivery=row, transmitted=True).count(), 1)

    @override_settings(**DISPATCH)
    def test_failing_transport_retries_then_dead(self):
        self._queue()
        row = CopierAlertDelivery.objects.get()
        row.max_attempts = 2
        row.save(update_fields=["max_attempts"])
        t = _FakeTransport(transmitted=False, retryable=True)
        dispatch_pending(transport=t)   # attempt 1 → FAILED
        self.assertEqual(CopierAlertDelivery.objects.get().status, CopierAlertDelivery.Status.FAILED)
        dispatch_pending(transport=t)   # attempt 2 → DEAD (exhausted)
        row = CopierAlertDelivery.objects.get()
        self.assertEqual(row.status, CopierAlertDelivery.Status.DEAD)
        self.assertEqual(CopierAlertDeliveryAttempt.objects.filter(delivery=row).count(), 2)

    @override_settings(**DISPATCH)
    def test_stuck_processing_is_reaped_and_retried(self):
        # L1 regression: a row orphaned in PROCESSING (process died mid-send) is reaped back to FAILED on the
        # next pass and then retried — never a silent delivery loss.
        from datetime import timedelta
        from django.utils import timezone
        self._queue()
        row = CopierAlertDelivery.objects.get()
        row.status = CopierAlertDelivery.Status.PROCESSING
        row.claimed_at = timezone.now() - timedelta(seconds=10_000)
        row.save(update_fields=["status", "claimed_at"])
        out = dispatch_pending(transport=_FakeTransport(transmitted=True))
        self.assertEqual(out["sent"], 1)
        self.assertEqual(CopierAlertDelivery.objects.get().status, CopierAlertDelivery.Status.SENT)

    @override_settings(**DISPATCH)
    def test_default_transport_has_no_real_destination(self):
        # The default (dry-run) transport never transmits — an armed dispatcher with no sink is visibly
        # unsuccessful, never a silent 'sent'.
        self.assertIsInstance(select_alert_transport(), type(select_alert_transport()))
        self._queue()
        out = dispatch_pending()   # default transport
        self.assertEqual(out["sent"], 0)
        self.assertIn(CopierAlertDelivery.objects.get().status,
                      (CopierAlertDelivery.Status.FAILED, CopierAlertDelivery.Status.DEAD))
