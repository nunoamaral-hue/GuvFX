"""Operator telemetry for the customer Telegram *connection* lifecycle (Objective E).

Proves: DARK by default (byte-for-byte no-op when OPERATIONS_EVENTS_ENABLED is off); when armed, the
token/binding/rejection lifecycle lands on the CONNECTIVITY operator timeline; every emit is
operator-only (customer_visible=False) so it never fires a spurious customer message; distinct reasons
(expired vs replayed vs already-bound); and no secret (raw token, digest, chat id) is ever recorded.
"""
import os
from unittest import mock

from django.test import override_settings

from operational_events.models import OperationalEvent

from .services import InvalidConnectionToken, create_connection_token, redeem_connection_token
from .tests import CustomerTelegramTestBase

_EVENTS_ON = mock.patch.dict(os.environ, {"OPERATIONS_EVENTS_ENABLED": "1"})


class TelegramConnectionTelemetryTests(CustomerTelegramTestBase):
    def _events(self, **flt):
        return OperationalEvent.objects.filter(source="customer_telegram", **flt)

    def test_dark_by_default_emits_nothing(self):
        # OPERATIONS_EVENTS_ENABLED unset -> the connection flow works but records no operator event.
        with self.captureOnCommitCallbacks(execute=True):
            self.bind(self.user_a, 111222333)
        self.assertEqual(self._events().count(), 0)

    def test_token_created_is_operator_only_and_secret_free(self):
        with _EVENTS_ON:
            raw = self.token_for(self.user_a)
        ev = self._events(event_type="telegram_token_created").get()
        self.assertEqual(ev.category, "CONNECTIVITY")
        self.assertFalse(ev.customer_visible)                 # operator-only — never a customer message
        self.assertEqual(ev.metadata.get("user_id"), self.user_a.pk)
        self.assertIn("ttl_seconds", ev.metadata)
        # The raw deep-link token is never recorded anywhere on the event.
        blob = f"{ev.metadata}{ev.summary}{ev.reason_code}{ev.dedup_key}"
        self.assertNotIn(raw, blob)

    def test_binding_created_then_reconnected(self):
        with _EVENTS_ON:
            with self.captureOnCommitCallbacks(execute=True):
                self.bind(self.user_a, 111222333)
            self.assertEqual(self._events(event_type="telegram_binding_created").count(), 1)
            # Same user reconnecting the same identity → reconnected, not created (isolation preserved).
            with self.captureOnCommitCallbacks(execute=True):
                self.bind(self.user_a, 111222333)
            self.assertEqual(self._events(event_type="telegram_binding_reconnected").count(), 1)
            self.assertFalse(self._events(event_type="telegram_binding_created").get().customer_visible)

    def test_cross_user_conflict_is_recorded_with_reason(self):
        # user_a owns chat 111; user_b tries the same chat via the real webhook → chat_already_connected.
        with self.captureOnCommitCallbacks(execute=True):
            self.bind(self.user_a, 111000111)
        raw_b = self.token_for(self.user_b)
        with _EVENTS_ON:
            resp = self.webhook(raw_b, 111000111)
        self.assertEqual(resp.status_code, 200)               # acked (deadlock fix) — but now observable
        ev = self._events(event_type="telegram_connect_rejected").get()
        self.assertEqual(ev.reason_code, "chat_already_connected")
        self.assertEqual(ev.severity, "WARNING")
        self.assertFalse(ev.customer_visible)
        # No cross-user binding was created (isolation intact).
        from .models import CustomerTelegramBinding
        self.assertFalse(CustomerTelegramBinding.objects.filter(user=self.user_b).exists())

    def test_expired_and_replayed_tokens_have_distinct_telemetry_reasons(self):
        # Expired: a token whose window has closed raises token_expired (same code/HTTP as before).
        raw = self.token_for(self.user_a)
        from django.utils import timezone
        from datetime import timedelta
        from .models import TelegramConnectionToken
        TelegramConnectionToken.objects.filter(user=self.user_a).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(InvalidConnectionToken) as ctx:
            redeem_connection_token(raw, chat_id=222, telegram_user_id=222)
        self.assertEqual(ctx.exception.telemetry_reason, "token_expired")
        self.assertEqual(ctx.exception.code, "invalid_or_expired_token")   # customer-facing unchanged

        # Replayed: a consumed token raises token_replayed.
        raw2 = self.token_for(self.user_b)
        with self.captureOnCommitCallbacks(execute=True):
            redeem_connection_token(raw2, chat_id=333, telegram_user_id=333)
        with self.assertRaises(InvalidConnectionToken) as ctx2:
            redeem_connection_token(raw2, chat_id=333, telegram_user_id=333)
        self.assertEqual(ctx2.exception.telemetry_reason, "token_replayed")

    def test_reconnects_are_not_over_deduped(self):
        # Distinct reconnect instants must be distinct events (observability); a retried on_commit for
        # the SAME connect instant collapses (idempotent).
        from datetime import timedelta
        from django.utils import timezone
        from . import telemetry
        t0 = timezone.now()
        with _EVENTS_ON:
            telemetry.binding_established(user_id=1, binding_id=99, created=False, connected_at=t0)
            telemetry.binding_established(user_id=1, binding_id=99, created=False,
                                         connected_at=t0 + timedelta(seconds=5))
            telemetry.binding_established(user_id=1, binding_id=99, created=False, connected_at=t0)  # dup
        self.assertEqual(self._events(event_type="telegram_binding_reconnected").count(), 2)

    def test_no_event_records_a_chat_id(self):
        with _EVENTS_ON:
            with self.captureOnCommitCallbacks(execute=True):
                self.bind(self.user_a, 987654321)
        for ev in self._events():
            blob = f"{ev.metadata}{ev.summary}{ev.reason_code}{ev.dedup_key}{ev.actor}"
            self.assertNotIn("987654321", blob)
