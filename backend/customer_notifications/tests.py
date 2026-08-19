from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
import pathlib
from unittest.mock import MagicMock, patch
import urllib.error
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.contrib import admin
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from trading.models import Trade, TradingAccount

from .delivery import (
    CustomerTelegramBotClient,
    TelegramAck,
    TelegramDeliveryError,
    dispatch_customer_notifications,
    queue_health,
)
from .event_sources import (
    collect_customer_notification_events,
    enqueue_execution_problem,
    enqueue_strategy_change,
    enqueue_trade_outcome,
    enqueue_trade_opened,
    enqueue_workspace_ready,
)
from .messages import render_customer_message
from .models import (
    CustomerNotification,
    CustomerNotificationAttempt,
    CustomerNotificationPreference,
    CustomerNotificationProjectionCursor,
    CustomerTelegramBinding,
    TelegramConnectionToken,
)
from .services import (
    create_connection_token,
    disconnect_telegram,
    enqueue_customer_notification,
    redeem_connection_token,
)


SETTINGS = dict(
    CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED=True,
    CUSTOMER_TELEGRAM_BOT_USERNAME="GuvFXCustomerBot",
    CUSTOMER_TELEGRAM_BOT_TOKEN="test-only-token",
    CUSTOMER_TELEGRAM_WEBHOOK_SECRET="test-webhook-secret",
    CUSTOMER_TELEGRAM_WEBHOOK_URL="https://api.example.test/api/customer-notifications/telegram/webhook/",
    CUSTOMER_TELEGRAM_WORKER_ENABLED=True,
    CUSTOMER_TELEGRAM_TOKEN_TTL_SECONDS=600,
    CUSTOMER_NOTIFICATION_MAX_ATTEMPTS=3,
    SECURE_SSL_REDIRECT=False,
)


class FakeClient:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [TelegramAck("101")])
        self.calls = []

    def send_message(self, chat_id, text):
        self.calls.append((chat_id, text))
        outcome = self.outcomes.pop(0) if self.outcomes else TelegramAck("102")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@override_settings(**SETTINGS)
class CustomerTelegramTestBase(TestCase):
    def setUp(self):
        U = get_user_model()
        self.user_a = U.objects.create_user(username="alice", email="alice@example.test", password="pass1234")
        self.user_b = U.objects.create_user(username="bob", email="bob@example.test", password="pass1234")
        self.account_a = TradingAccount.objects.create(
            user=self.user_a, name="Alice demo", broker_name="Test Broker A",
            account_number="10001", is_demo=True,
        )
        self.account_b = TradingAccount.objects.create(
            user=self.user_b, name="Bob demo", broker_name="Test Broker B",
            account_number="20002", is_demo=True,
        )

    def token_for(self, user, language="en"):
        result = create_connection_token(user, language=language)
        return parse_qs(urlparse(result["url"]).query)["start"][0]

    def bind(self, user, chat_id, language="en"):
        raw = self.token_for(user, language)
        return redeem_connection_token(
            raw, chat_id=chat_id, telegram_user_id=chat_id,
            username="display_user", first_name="Customer",
        )

    def webhook(self, raw, chat_id, *, secret="test-webhook-secret", chat_type="private"):
        client = APIClient()
        return client.post(
            "/api/customer-notifications/telegram/webhook/",
            {"update_id": 1, "message": {
                "text": f"/start {raw}",
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": chat_id, "username": "telegram_user", "first_name": "Alice"},
            }},
            format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret,
        )

    def notification(self, user=None, account=None, event=CustomerNotification.EventType.TRADE_OPENED,
                     dedupe="n:1", payload=None):
        user = user or self.user_a
        account = account or self.account_a
        CustomerNotificationPreference.objects.get_or_create(user=user)
        return enqueue_customer_notification(
            user=user, account=account, event_type=event,
            source_object_type="tests.Source", source_object_id="1", dedupe_key=dedupe,
            payload=payload or {
                "strategy": "Wayond WIM", "symbol": "XAUUSD", "side": "SELL",
                "volume": "0.01", "entry": "4343.44", "stop_loss": "4349.69",
                "take_profit": "4341.07", "account_kind": "demo", "account_number": "10001",
            },
        )


class ConnectionIsolationTests(CustomerTelegramTestBase):
    def test_customer_notification_admins_are_observation_only(self):
        request = APIClient().get("/").wsgi_request
        for model in (
            CustomerTelegramBinding, TelegramConnectionToken,
            CustomerNotificationPreference, CustomerNotification,
            CustomerNotificationAttempt, CustomerNotificationProjectionCursor,
        ):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))
        self.assertIn("telegram_chat_id", admin.site._registry[CustomerTelegramBinding].exclude)
        self.assertIn("telegram_user_id", admin.site._registry[CustomerTelegramBinding].exclude)
        self.assertIn("recipient_chat_id", admin.site._registry[CustomerNotificationAttempt].exclude)
        self.assertIn("token_digest", admin.site._registry[TelegramConnectionToken].exclude)

    def test_raw_token_is_not_stored_and_fits_telegram_limit(self):
        raw = self.token_for(self.user_a)
        row = TelegramConnectionToken.objects.get(user=self.user_a)
        self.assertLessEqual(len(raw), 64)
        self.assertNotEqual(row.token_digest, raw)
        self.assertNotIn(raw, row.token_digest)

    def test_user_a_token_can_only_bind_user_a(self):
        raw = self.token_for(self.user_a)
        self.assertEqual(self.webhook(raw, 111).status_code, 200)
        binding = CustomerTelegramBinding.objects.get(telegram_chat_id=111)
        self.assertEqual(binding.user_id, self.user_a.id)
        self.assertFalse(CustomerTelegramBinding.objects.filter(user=self.user_b).exists())

    def test_one_private_chat_cannot_bind_two_users(self):
        self.bind(self.user_a, 111)
        raw_b = self.token_for(self.user_b)
        self.assertEqual(self.webhook(raw_b, 111).status_code, 400)
        self.assertIsNone(TelegramConnectionToken.objects.get(user=self.user_b).consumed_at)
        self.assertEqual(CustomerTelegramBinding.objects.get(telegram_chat_id=111).user_id, self.user_a.id)

    def test_expired_token_is_rejected(self):
        raw = self.token_for(self.user_a)
        TelegramConnectionToken.objects.filter(user=self.user_a).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.webhook(raw, 111).status_code, 400)
        self.assertFalse(CustomerTelegramBinding.objects.exists())

    def test_reused_token_is_rejected(self):
        raw = self.token_for(self.user_a)
        self.assertEqual(self.webhook(raw, 111).status_code, 200)
        self.assertEqual(self.webhook(raw, 111).status_code, 400)

    def test_duplicate_webhook_delivery_cannot_create_a_second_binding(self):
        raw = self.token_for(self.user_a)
        first = self.webhook(raw, 111)
        duplicate = self.webhook(raw, 111)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(CustomerTelegramBinding.objects.filter(user=self.user_a).count(), 1)

    def test_changed_username_is_display_only_and_does_not_change_authoritative_identity(self):
        binding = self.bind(self.user_a, 111)
        raw = self.token_for(self.user_a)
        updated = redeem_connection_token(
            raw, chat_id=111, telegram_user_id=111,
            username="renamed_display_user", first_name="Renamed",
        )
        self.assertEqual(updated.pk, binding.pk)
        self.assertEqual(updated.telegram_chat_id, 111)
        self.assertEqual(updated.telegram_username, "renamed_display_user")

    def test_out_of_range_private_chat_id_is_rejected_before_persistence(self):
        raw = self.token_for(self.user_a)
        self.assertEqual(self.webhook(raw, 2 ** 63).status_code, 400)
        self.assertFalse(CustomerTelegramBinding.objects.exists())

    def test_malformed_token_is_rejected(self):
        self.assertEqual(self.webhook("user=1@example.test", 111).status_code, 400)
        self.assertFalse(CustomerTelegramBinding.objects.exists())

    def test_arbitrary_chat_id_in_connect_request_cannot_bind(self):
        client = APIClient()
        client.force_authenticate(self.user_a)
        response = client.post(
            "/api/customer-notifications/telegram/connect/",
            {"language": "en", "chat_id": 999999}, format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(CustomerTelegramBinding.objects.exists())

    def test_group_start_is_rejected(self):
        raw = self.token_for(self.user_a)
        self.assertEqual(self.webhook(raw, -100123, chat_type="supergroup").status_code, 400)
        self.assertFalse(CustomerTelegramBinding.objects.exists())

    def test_webhook_authentication_failure_is_rejected(self):
        raw = self.token_for(self.user_a)
        self.assertEqual(self.webhook(raw, 111, secret="wrong").status_code, 403)
        self.assertFalse(CustomerTelegramBinding.objects.exists())

    @override_settings(CUSTOMER_TELEGRAM_WORKER_ENABLED=False)
    def test_worker_disabled_makes_connect_and_webhook_unavailable(self):
        client = APIClient()
        client.force_authenticate(self.user_a)
        self.assertEqual(client.post(
            "/api/customer-notifications/telegram/connect/", {"language": "en"}, format="json",
        ).status_code, 503)
        client.force_authenticate(user=None)
        self.assertEqual(client.post(
            "/api/customer-notifications/telegram/webhook/", {}, format="json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-webhook-secret",
        ).status_code, 404)

    @override_settings(CUSTOMER_TELEGRAM_WEBHOOK_URL="http://insecure.example.test/webhook")
    def test_non_https_webhook_configuration_is_unavailable(self):
        client = APIClient()
        client.force_authenticate(self.user_a)
        self.assertEqual(client.post(
            "/api/customer-notifications/telegram/connect/", {"language": "en"}, format="json",
        ).status_code, 503)

    def test_unexpected_update_type_is_ignored(self):
        client = APIClient()
        response = client.post(
            "/api/customer-notifications/telegram/webhook/", {"callback_query": {"id": "x"}},
            format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-webhook-secret",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomerTelegramBinding.objects.exists())

    def test_owner_settings_never_expose_numeric_chat_id(self):
        self.bind(self.user_a, 111)
        client = APIClient()
        client.force_authenticate(self.user_a)
        body = client.get("/api/customer-notifications/telegram/").json()
        self.assertTrue(body["connected"])
        self.assertNotIn("chat_id", str(body))
        self.assertNotIn("111", str(body))

    def test_user_a_cannot_retrieve_user_b_binding(self):
        self.bind(self.user_b, 222)
        client = APIClient()
        client.force_authenticate(self.user_a)
        body = client.get("/api/customer-notifications/telegram/").json()
        self.assertFalse(body["connected"])
        self.assertEqual(body["display"], {"username": "", "first_name": ""})
        self.assertNotIn("222", str(body))

    def test_staff_health_surface_never_exposes_numeric_chat_id(self):
        self.bind(self.user_a, 111)
        self.user_a.is_staff = True
        self.user_a.save(update_fields=["is_staff"])
        client = APIClient()
        client.force_authenticate(self.user_a)
        body = client.get("/api/customer-notifications/health/").json()
        self.assertNotIn("chat", str(body).lower())
        self.assertNotIn("111", str(body))


class PreferenceAndRoutingTests(CustomerTelegramTestBase):
    def setUp(self):
        super().setUp()
        self.bind(self.user_a, 111)
        self.bind(self.user_b, 222)

    def test_user_a_never_receives_user_b_trade(self):
        trade_b = Trade.objects.create(
            account=self.account_b, ticket="b1", symbol="XAUUSD", side="SELL", volume="0.01",
            open_time=timezone.now(), open_price="4343.44",
        )
        row = enqueue_trade_opened(trade_b.id)
        self.assertEqual(row.user_id, self.user_b.id)
        self.assertEqual(row.account_id, self.account_b.id)
        self.assertFalse(CustomerNotification.objects.filter(user=self.user_a, account=self.account_b).exists())

    def test_a_and_b_events_deliver_only_to_their_verified_private_chats(self):
        self.notification(user=self.user_a, account=self.account_a, dedupe="route-a")
        self.notification(
            user=self.user_b, account=self.account_b, dedupe="route-b",
            payload={
                "strategy": "Wayond WIM", "symbol": "XAUUSD", "side": "BUY",
                "account_kind": "demo", "account_number": "20002",
            },
        )
        client = FakeClient([TelegramAck("a"), TelegramAck("b")])
        self.assertEqual(dispatch_customer_notifications(client=client)["delivered"], 2)
        self.assertEqual([chat_id for chat_id, _ in client.calls], [111, 222])
        self.assertIn("10001", client.calls[0][1])
        self.assertNotIn("20002", client.calls[0][1])
        self.assertIn("20002", client.calls[1][1])
        self.assertNotIn("10001", client.calls[1][1])

    def test_explicit_cross_owner_enqueue_fails_closed(self):
        row = enqueue_customer_notification(
            user=self.user_a, account=self.account_b,
            event_type=CustomerNotification.EventType.TRADE_OPENED,
            source_object_type="tests", source_object_id="x", dedupe_key="cross-owner", payload={},
        )
        self.assertIsNone(row)
        self.assertFalse(CustomerNotification.objects.filter(dedupe_key="cross-owner").exists())

    def test_duplicate_source_event_creates_one_row(self):
        first = self.notification(dedupe="same-source")
        second = self.notification(dedupe="same-source")
        self.assertEqual(first.id, second.id)
        self.assertEqual(CustomerNotification.objects.filter(dedupe_key="same-source").count(), 1)

    def test_event_preference_off_suppresses(self):
        pref = CustomerNotificationPreference.objects.get(user=self.user_a)
        pref.trade_opened = False
        pref.save(update_fields=["trade_opened", "updated_at"])
        row = self.notification(dedupe="pref-off")
        self.assertEqual(row.status, CustomerNotification.Status.SUPPRESSED)

    def test_master_off_suppresses_all(self):
        pref = CustomerNotificationPreference.objects.get(user=self.user_a)
        pref.telegram_enabled = False
        pref.save(update_fields=["telegram_enabled", "updated_at"])
        row = self.notification(dedupe="master-off")
        self.assertEqual(row.status, CustomerNotification.Status.SUPPRESSED)

    @override_settings(TELEGRAM_CHAT_ID="stakeholder-global-chat")
    def test_missing_binding_never_falls_back_to_stakeholder_or_global_chat(self):
        disconnect_telegram(self.user_a)
        row = self.notification(dedupe="no-global-fallback")
        client = FakeClient()
        self.assertEqual(row.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(dispatch_customer_notifications(client=client)["claimed"], 0)
        self.assertEqual(client.calls, [])

    def test_disconnect_before_delivery_suppresses_without_send(self):
        row = self.notification(dedupe="disconnect-before-send")
        disconnect_telegram(self.user_a)
        client = FakeClient()
        result = dispatch_customer_notifications(client=client)
        row.refresh_from_db()
        self.assertEqual(result["suppressed"], 1)
        self.assertEqual(row.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(client.calls, [])

    def test_preference_changed_after_enqueue_suppresses_without_send(self):
        row = self.notification(dedupe="preference-changed-after-enqueue")
        pref = CustomerNotificationPreference.objects.get(user=self.user_a)
        pref.trade_opened = False
        pref.save(update_fields=["trade_opened", "updated_at"])
        client = FakeClient()
        self.assertEqual(dispatch_customer_notifications(client=client)["suppressed"], 1)
        row.refresh_from_db()
        self.assertEqual(row.last_error, "event_disabled")
        self.assertEqual(client.calls, [])

    def test_inactive_user_with_queued_notification_is_suppressed(self):
        row = self.notification(dedupe="inactive-after-enqueue")
        self.user_a.is_active = False
        self.user_a.save(update_fields=["is_active"])
        client = FakeClient()
        self.assertEqual(dispatch_customer_notifications(client=client)["suppressed"], 1)
        row.refresh_from_db()
        self.assertEqual(row.last_error, "user_inactive")
        self.assertEqual(client.calls, [])

    def test_deleted_user_with_queued_notification_is_preserved_and_suppressed(self):
        row = self.notification(dedupe="deleted-after-enqueue")
        row_id = row.pk
        self.user_a.delete()
        row = CustomerNotification.objects.get(pk=row_id)
        self.assertIsNone(row.user_id)
        client = FakeClient()
        self.assertEqual(dispatch_customer_notifications(client=client)["suppressed"], 1)
        row.refresh_from_db()
        self.assertEqual(row.last_error, "user_deleted")
        self.assertEqual(client.calls, [])

    def test_preference_change_api_is_owner_scoped(self):
        client = APIClient()
        client.force_authenticate(self.user_a)
        response = client.patch(
            "/api/customer-notifications/telegram/preferences/",
            {"trade_closed": False, "language": "ja"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomerNotificationPreference.objects.get(user=self.user_a).trade_closed)
        self.assertTrue(CustomerNotificationPreference.objects.get(user=self.user_b).trade_closed)


class DeliverySafetyTests(CustomerTelegramTestBase):
    def setUp(self):
        super().setUp()
        self.bind(self.user_a, 111)

    def test_successful_ack_is_sent_at_most_once(self):
        row = self.notification(dedupe="once")
        client = FakeClient([TelegramAck("501")])
        self.assertEqual(dispatch_customer_notifications(client=client)["delivered"], 1)
        self.assertEqual(dispatch_customer_notifications(client=client)["delivered"], 0)
        row.refresh_from_db()
        self.assertEqual(row.status, CustomerNotification.Status.DELIVERED)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(row.delivery_attempts.get().provider_message_id, "501")

    def test_delivery_attempt_evidence_is_append_only(self):
        row = self.notification(dedupe="immutable-attempt")
        dispatch_customer_notifications(client=FakeClient())
        attempt = CustomerNotificationAttempt.objects.get(notification=row)
        attempt.error_code = "tampered"
        with self.assertRaises(ValueError):
            attempt.save()
        with self.assertRaises(ValueError):
            CustomerNotificationAttempt.objects.filter(pk=attempt.pk).update(error_code="tampered")
        with self.assertRaises(ValueError):
            attempt.delete()

    def test_known_failure_retries_then_delivers_once(self):
        row = self.notification(dedupe="retry")
        client = FakeClient([
            TelegramDeliveryError("telegram_http_500", retryable=True), TelegramAck("502"),
        ])
        self.assertEqual(dispatch_customer_notifications(client=client)["retrying"], 1)
        CustomerNotification.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now())
        self.assertEqual(dispatch_customer_notifications(client=client)["delivered"], 1)
        self.assertEqual(dispatch_customer_notifications(client=client)["delivered"], 0)
        self.assertEqual(len(client.calls), 2)

    def test_ambiguous_network_failure_is_never_retried(self):
        row = self.notification(dedupe="ambiguous")
        client = FakeClient([TelegramDeliveryError("telegram_network_ambiguous", ambiguous=True)])
        self.assertEqual(dispatch_customer_notifications(client=client)["failed"], 1)
        self.assertEqual(dispatch_customer_notifications(client=client)["claimed"], 0)
        row.refresh_from_db()
        self.assertEqual(row.status, CustomerNotification.Status.FAILED)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(queue_health()["ambiguous_delivery_count"], 1)

    @override_settings(CUSTOMER_NOTIFICATION_MAX_ATTEMPTS=1)
    def test_retry_exhaustion_is_terminal_and_counted(self):
        row = self.notification(dedupe="retry-exhaustion")
        client = FakeClient([TelegramDeliveryError("telegram_http_429", retryable=True)])
        self.assertEqual(dispatch_customer_notifications(client=client)["failed"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, CustomerNotification.Status.FAILED)
        self.assertEqual(row.last_error, "retry_exhausted:telegram_http_429")
        self.assertEqual(queue_health()["retry_exhaustion_count"], 1)

    def test_worker_restart_never_reclaims_processing_row(self):
        row = self.notification(dedupe="processing-restart")
        CustomerNotification.objects.filter(pk=row.pk).update(
            status=CustomerNotification.Status.PROCESSING,
        )
        client = FakeClient()
        result = dispatch_customer_notifications(client=client)
        self.assertEqual(result["claimed"], 0)
        self.assertEqual(client.calls, [])

    @override_settings(CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED=False)
    def test_feature_flag_off_neither_enqueues_nor_delivers(self):
        pending = CustomerNotification.objects.create(
            user=self.user_a, account=self.account_a,
            event_type=CustomerNotification.EventType.TRADE_OPENED,
            source_object_type="tests.Source", source_object_id="dark-existing",
            dedupe_key="dark-existing", payload={},
        )
        client = FakeClient()
        result = dispatch_customer_notifications(client=client)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["claimed"], 0)
        self.assertEqual(client.calls, [])
        self.assertIsNone(self.notification(dedupe="dark-new"))
        pending.refresh_from_db()
        self.assertEqual(pending.status, CustomerNotification.Status.PENDING)

    @override_settings(CUSTOMER_TELEGRAM_WORKER_ENABLED=False)
    def test_worker_flag_off_leaves_queue_untouched(self):
        row = self.notification(dedupe="worker-disabled")
        client = FakeClient()
        result = dispatch_customer_notifications(client=client)
        self.assertFalse(result["worker_enabled"])
        self.assertEqual(result["claimed"], 0)
        self.assertEqual(client.calls, [])
        row.refresh_from_db()
        self.assertEqual(row.status, CustomerNotification.Status.PENDING)

    @override_settings(CUSTOMER_TELEGRAM_BOT_TOKEN="")
    def test_missing_bot_token_fails_notification_plane_only(self):
        trade = Trade.objects.create(
            account=self.account_a, ticket="a1", symbol="XAUUSD", side="BUY", volume="0.01",
            open_time=timezone.now(), open_price="1.23456",
        )
        row = self.notification(dedupe="missing-token")
        result = dispatch_customer_notifications()
        self.assertEqual(result["retrying"], 1)
        self.assertTrue(Trade.objects.filter(pk=trade.pk).exists())
        row.refresh_from_db()
        self.assertEqual(row.last_error, "bot_token_missing")

    def test_execution_event_remains_committed_when_delivery_is_rejected(self):
        trade = Trade.objects.create(
            account=self.account_a, ticket="delivery-failure", symbol="XAUUSD", side="BUY",
            volume="0.01", open_time=timezone.now(), open_price="1.23456",
        )
        self.notification(dedupe="delivery-rejected")
        result = dispatch_customer_notifications(client=FakeClient([
            TelegramDeliveryError("telegram_http_400"),
        ]))
        self.assertEqual(result["failed"], 1)
        self.assertTrue(Trade.objects.filter(pk=trade.pk).exists())

    @override_settings(
        CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED=False,
        CUSTOMER_TELEGRAM_WORKER_ENABLED=False,
    )
    def test_worker_output_never_logs_credentials_or_raw_chat_ids(self):
        output = StringIO()
        call_command("run_customer_notification_worker", "--once", stdout=output)
        rendered = output.getvalue()
        self.assertNotIn("111", rendered)
        self.assertNotIn("test-only-token", rendered)
        self.assertNotIn("test-webhook-secret", rendered)

    def test_observer_failure_cannot_roll_back_trade_transaction(self):
        with patch("customer_notifications.signals.enqueue_trade_opened", side_effect=RuntimeError("boom")):
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    trade = Trade.objects.create(
                        account=self.account_a, ticket="safe1", symbol="XAUUSD", side="BUY",
                        volume="0.01", open_time=timezone.now(), open_price="1.23456",
                    )
        self.assertTrue(Trade.objects.filter(pk=trade.pk).exists())

    def test_unsupported_customer_command_cannot_mutate_execution(self):
        from execution.models import ExecutionJob

        raw = self.token_for(self.user_b)
        before = list(ExecutionJob.objects.values_list("id", "status"))
        client = APIClient()
        response = client.post(
            "/api/customer-notifications/telegram/webhook/",
            {"message": {
                "text": "/place_order XAUUSD BUY",
                "chat": {"id": 222, "type": "private"},
                "from": {"id": 222, "username": "attacker"},
                "connection_token": raw,
            }},
            format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-webhook-secret",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ignored"])
        self.assertEqual(list(ExecutionJob.objects.values_list("id", "status")), before)
        self.assertFalse(CustomerTelegramBinding.objects.filter(user=self.user_b).exists())

    def test_customer_delivery_plane_has_no_wims_or_global_fallback(self):
        root = pathlib.Path(__file__).parent
        source = "\n".join(
            (root / name).read_text()
            for name in ("delivery.py", "services.py", "views.py")
        )
        for forbidden in (
            'getattr(settings, "TELEGRAM_CHAT_ID"', "VALIDATION_AGENT_TELEGRAM",
            "ConsumptionContract", "from wims", "import wims",
        ):
            self.assertNotIn(forbidden, source)

    def test_delivery_worker_has_no_execution_authority_imports_or_mutators(self):
        root = pathlib.Path(__file__).parent
        delivery = (root / "delivery.py").read_text().lower()
        command = (
            root / "management" / "commands" / "run_customer_notification_worker.py"
        ).read_text().lower()
        for forbidden in (
            "executionjob", "workeridentity", "place_order", "close_trade",
            "modify_position", "order_transport", "next_job",
        ):
            self.assertNotIn(forbidden, delivery)
            self.assertNotIn(forbidden, command)

    def test_payload_allowlist_removes_architecture_and_security_fields(self):
        row = self.notification(
            dedupe="sanitised",
            payload={
                "strategy": "Wayond WIM", "symbol": "XAUUSD", "side": "SELL",
                "account_kind": "demo", "account_number": "10001",
                "worker_id": "node-2-secret", "windows_username": "guvfx_u_25",
                "bridge_url": "http://internal:8788", "stack_trace": "Traceback secret",
                "bot_token": "secret",
            },
        )
        text = render_customer_message(row)
        combined = f"{row.payload} {text}".lower()
        for forbidden in ("worker_id", "node-2", "windows", "guvfx_u_25", "8788", "traceback", "bot_token", "secret"):
            self.assertNotIn(forbidden, combined)


class LanguageCatalogueTests(CustomerTelegramTestBase):
    def test_japanese_user_receives_japanese_message(self):
        self.bind(self.user_a, 111, language="ja")
        row = self.notification(dedupe="ja")
        self.assertEqual(row.language, "ja")
        self.assertIn("取引が開始", render_customer_message(row))
        self.assertIn("デモ口座", render_customer_message(row))

    def test_english_user_receives_english_message(self):
        self.bind(self.user_a, 111, language="en")
        row = self.notification(dedupe="en")
        self.assertEqual(row.language, "en")
        self.assertIn("trade opened", render_customer_message(row).lower())
        self.assertIn("Demo account", render_customer_message(row))


class TelegramBotClientFailureTests(TestCase):
    def bot_client(self):
        return CustomerTelegramBotClient(token="test-client-token", timeout=1)

    def response(self, payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    def assert_http_error(self, status_code, *, retryable):
        error = urllib.error.HTTPError(
            "https://api.telegram.org", status_code, "provider error", {}, None,
        )
        with patch("customer_notifications.delivery.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramDeliveryError) as raised:
                self.bot_client().send_message(111, "safe message")
        self.assertEqual(raised.exception.code, f"telegram_http_{status_code}")
        self.assertEqual(raised.exception.retryable, retryable)

    def test_telegram_4xx_is_terminal(self):
        self.assert_http_error(400, retryable=False)

    def test_telegram_429_is_retryable(self):
        self.assert_http_error(429, retryable=True)

    def test_telegram_5xx_is_retryable(self):
        self.assert_http_error(503, retryable=True)

    def test_timeout_before_acknowledgement_is_ambiguous_and_terminal(self):
        with patch(
            "customer_notifications.delivery.urllib.request.urlopen",
            side_effect=TimeoutError("timed out before response"),
        ):
            with self.assertRaises(TelegramDeliveryError) as raised:
                self.bot_client().send_message(111, "safe message")
        self.assertTrue(raised.exception.ambiguous)
        self.assertFalse(raised.exception.retryable)

    def test_timeout_after_potential_acknowledgement_is_ambiguous_and_terminal(self):
        with patch(
            "customer_notifications.delivery.urllib.request.urlopen",
            side_effect=urllib.error.URLError(TimeoutError("timed out reading response")),
        ):
            with self.assertRaises(TelegramDeliveryError) as raised:
                self.bot_client().send_message(111, "safe message")
        self.assertTrue(raised.exception.ambiguous)
        self.assertFalse(raised.exception.retryable)

    def test_malformed_response_is_ambiguous_and_terminal(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"not-json"
        with patch("customer_notifications.delivery.urllib.request.urlopen", return_value=response):
            with self.assertRaises(TelegramDeliveryError) as raised:
                self.bot_client().send_message(111, "safe message")
        self.assertEqual(raised.exception.code, "telegram_response_ambiguous")
        self.assertTrue(raised.exception.ambiguous)

    def test_valid_ack_requires_message_id(self):
        with patch(
            "customer_notifications.delivery.urllib.request.urlopen",
            return_value=self.response({"ok": True, "result": {}}),
        ):
            with self.assertRaises(TelegramDeliveryError) as raised:
                self.bot_client().send_message(111, "safe message")
        self.assertEqual(raised.exception.code, "telegram_response_ambiguous")


class EventSourceMappingTests(CustomerTelegramTestBase):
    def setUp(self):
        super().setUp()
        self.bind(self.user_a, 111)

    def test_strategy_audit_transition_maps_to_owner_only(self):
        from core.models import AuditEvent
        from strategies.models import Strategy, StrategyAssignment
        strategy = Strategy.objects.create(owner=self.user_a, name="Wayond WIM")
        assignment = StrategyAssignment.objects.create(
            strategy=strategy, account=self.account_a, is_active=False,
            execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
            signal_source="ti_signals", stage=StrategyAssignment.STAGE_LIVE,
        )
        audit = AuditEvent.objects.create(
            user=self.user_a, event_type="SIGNAL_COPY_DISABLED", entity_type="account",
            entity_id=str(self.account_a.id), metadata={"assignment_id": assignment.id},
        )
        row = enqueue_strategy_change(audit.pk)
        self.assertEqual(row.event_type, CustomerNotification.EventType.STRATEGY_DISABLED)
        self.assertEqual(row.user_id, self.user_a.id)
        self.assertEqual(row.payload["strategy"], "Wayond WIM")

    def test_workspace_ready_uses_authoritative_transition(self):
        from hosted_workspace.models import HostedMt5Workspace, WorkspaceTransition
        workspace = HostedMt5Workspace.objects.create(trading_account=self.account_a)
        transition = WorkspaceTransition.objects.create(
            workspace=workspace, from_state="CONNECTED", to_state="EXECUTION_READY",
            observation_version=1, decision_version=1, state_changed=True,
            execution_ready_changed=True, dedupe_key="workspace-ready-test",
        )
        row = enqueue_workspace_ready(transition.pk)
        self.assertEqual(row.event_type, CustomerNotification.EventType.WORKSPACE_READY)
        self.assertEqual(row.account_id, self.account_a.id)

    def test_execution_problem_is_customer_safe_and_hourly_deduped(self):
        from operational_events.models import OperationalEvent
        first = OperationalEvent.objects.create(
            account=self.account_a, category="EXECUTION", event_type="place_order_failed",
            severity="ERROR", reason_code="bridge_internal_detail", customer_visible=True,
            summary="worker node-2 at internal:8788 failed",
        )
        second = OperationalEvent.objects.create(
            account=self.account_a, category="EXECUTION", event_type="place_order_failed",
            severity="ERROR", reason_code="bridge_internal_detail", customer_visible=True,
            summary="another internal failure",
        )
        row = enqueue_execution_problem(first.pk)
        enqueue_execution_problem(second.pk)
        self.assertEqual(CustomerNotification.objects.filter(
            event_type=CustomerNotification.EventType.EXECUTION_PROBLEM).count(), 1)
        text = render_customer_message(row).lower()
        self.assertIn("couldn’t place", text)
        self.assertNotIn("node-2", text)
        self.assertNotIn("8788", text)

    def test_trade_open_uses_authoritative_per_trade_volume(self):
        trade = Trade.objects.create(
            account=self.account_a, ticket="actual-volume", symbol="XAUUSD", side="SELL",
            volume="0.07", open_time=timezone.now(), open_price="4343.44",
        )
        row = enqueue_trade_opened(trade.pk)
        self.assertEqual(row.payload["volume"], "0.07")

    def test_durable_leg_outcome_emits_customer_safe_tp_progress_then_final_close(self):
        from execution.models import ProposedOrderLeg, SignalExecutionPlan, TradeOutcomeRecord
        from signal_intake.models import PendingSignalApproval

        now = timezone.now()
        approval = PendingSignalApproval.objects.create(
            source="ti_signals", message_id="customer-progress", symbol="XAUUSD",
            direction="BUY", stop_loss="4300", take_profits=["4350", "4360", "4370"],
            status=PendingSignalApproval.Status.APPROVED, correlation_id="customer-progress",
        )
        plan = SignalExecutionPlan.objects.create(
            approval=approval, account=self.account_a, source="ti_signals",
            message_id="customer-progress", symbol="XAUUSD", direction="BUY",
            entry="4340", stop_loss="4300", is_demo=True,
            signal_timestamp=now, correlation_id="customer-progress",
            status=SignalExecutionPlan.Status.PROMOTED,
        )
        trades = []
        for index, target in enumerate(("4350", "4360", "4370"), start=1):
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=index, take_profit=target, stop_loss="4300",
                lot_size=Decimal("0.40"), status=ProposedOrderLeg.Status.PROMOTED,
            )
            trades.append(Trade.objects.create(
                account=self.account_a, ticket=f"progress-{index}", symbol="XAUUSD",
                side="BUY", volume=Decimal("0.40"), open_time=now,
                open_price=Decimal("4340"), comment=f"WAY{plan.id}L{index}",
            ))

        first = trades[0]
        first.close_time = now
        first.close_ingested_at = now
        first.close_price = Decimal("4350")
        first.profit = Decimal("40")
        first.save(update_fields=["close_time", "close_ingested_at", "close_price", "profit"])
        first_outcome = TradeOutcomeRecord.objects.create(
            trade=first, outcome=TradeOutcomeRecord.Outcome.WIN, net_pnl=Decimal("40"),
            correlation_id="customer-progress", signal_source="ti_signals",
        )
        update = enqueue_trade_outcome(first_outcome.pk)
        self.assertEqual(update.event_type, CustomerNotification.EventType.TRADE_UPDATED)
        self.assertEqual(update.payload["progress_label"], "TP1")
        self.assertEqual(update.payload["progress_closed"], 1)
        self.assertEqual(update.payload["progress_total"], 3)
        update_text = render_customer_message(update)
        self.assertIn("TP1 reached", update_text)
        self.assertIn("Demo account · 10001", update_text)
        self.assertIn("UTC", update_text)

        for index, trade in enumerate(trades[1:], start=2):
            trade.close_time = now + timedelta(seconds=index)
            trade.close_ingested_at = trade.close_time
            trade.close_price = Decimal(str(4340 + index * 10))
            trade.profit = Decimal("40")
            trade.save(update_fields=["close_time", "close_ingested_at", "close_price", "profit"])
        final_outcome = TradeOutcomeRecord.objects.create(
            trade=trades[2], outcome=TradeOutcomeRecord.Outcome.WIN, net_pnl=Decimal("40"),
            correlation_id="customer-progress", signal_source="ti_signals",
        )
        closed = enqueue_trade_outcome(final_outcome.pk)
        self.assertEqual(closed.event_type, CustomerNotification.EventType.TRADE_CLOSED)
        self.assertEqual(closed.payload["progress_closed"], 3)
        self.assertEqual(closed.payload["result"], "120.00")
        self.assertIn("Final result: +120.00", render_customer_message(closed))

    def test_reconciler_cursor_advances_across_bounded_batches_without_starvation(self):
        trades = [Trade.objects.create(
            account=self.account_a, ticket=f"cursor-{index}", symbol="XAUUSD", side="BUY",
            volume="0.01", open_time=timezone.now(), open_price="4343.44",
        ) for index in range(3)]
        first = collect_customer_notification_events(limit=2)
        second = collect_customer_notification_events(limit=2)
        self.assertEqual(first["trade_opened"], 2)
        self.assertEqual(second["trade_opened"], 1)
        self.assertEqual(CustomerNotification.objects.filter(
            event_type=CustomerNotification.EventType.TRADE_OPENED,
            source_object_id__in=[str(trade.pk) for trade in trades],
        ).count(), 3)
        self.assertEqual(
            CustomerNotificationProjectionCursor.objects.get(source="trade_opened").last_object_id,
            str(trades[-1].pk),
        )

    def test_reconciler_projection_error_rolls_back_cursor_for_retry(self):
        trade = Trade.objects.create(
            account=self.account_a, ticket="cursor-retry", symbol="XAUUSD", side="BUY",
            volume="0.01", open_time=timezone.now(), open_price="4343.44",
        )
        with patch(
            "customer_notifications.event_sources.enqueue_trade_opened",
            side_effect=RuntimeError("temporary projection failure"),
        ):
            failed = collect_customer_notification_events(limit=1)
        self.assertEqual(failed["errors"], 1)
        self.assertFalse(CustomerNotificationProjectionCursor.objects.filter(
            source="trade_opened", last_object_id=str(trade.pk),
        ).exists())
        recovered = collect_customer_notification_events(limit=1)
        self.assertEqual(recovered["trade_opened"], 1)
        self.assertTrue(CustomerNotification.objects.filter(
            event_type=CustomerNotification.EventType.TRADE_OPENED,
            source_object_id=str(trade.pk),
        ).exists())
