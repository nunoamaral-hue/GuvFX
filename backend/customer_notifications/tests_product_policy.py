from __future__ import annotations

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from hosted_workspace.models import HostedMt5Workspace
from strategies.models import Strategy, StrategyAssignment

from .cards import customer_result_card_model, render_customer_result_card
from .delivery import dispatch_customer_notifications
from .messages import render_customer_message
from .models import (
    CustomerNotification,
    CustomerNotificationPreference,
    CustomerStrategyNotificationPreference,
    WorkspaceReadinessNotificationIntent,
)
from .services import (
    enqueue_customer_notification,
    fulfill_pending_workspace_readiness,
    request_workspace_readiness_notification,
    set_strategy_notification_preference,
)
from .tests import CustomerTelegramTestBase, FakeClient, SETTINGS


@override_settings(**SETTINGS, FRONTEND_BASE_URL="https://app.guvfx.test")
class CustomerTelegramProductPolicyTests(CustomerTelegramTestBase):
    def setUp(self):
        super().setUp()
        self.bind(self.user_a, 111)
        self.bind(self.user_b, 222)
        self.strategy_a = Strategy.objects.create(owner=self.user_a, name="Wayond WIM Strategy")
        self.assignment_a = StrategyAssignment.objects.create(
            strategy=self.strategy_a,
            account=self.account_a,
            is_active=True,
            execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
            signal_source="ti_signals",
            stage=StrategyAssignment.STAGE_LIVE,
        )
        self.strategy_b = Strategy.objects.create(owner=self.user_a, name="London Breakout")
        self.assignment_b = StrategyAssignment.objects.create(
            strategy=self.strategy_b,
            account=self.account_a,
            is_active=True,
            execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
            signal_source="london_breakout",
            stage=StrategyAssignment.STAGE_LIVE,
        )
        CustomerStrategyNotificationPreference.objects.create(
            user=self.user_a, assignment=self.assignment_a, enabled=True,
        )

    def final(self, *, result="12.34", assignment=None, dedupe="final", user=None, account=None):
        assignment = assignment or self.assignment_a
        user = user or self.user_a
        account = account or self.account_a
        return enqueue_customer_notification(
            user=user,
            account=account,
            strategy_assignment=assignment,
            event_type=CustomerNotification.EventType.TRADE_CLOSED,
            source_object_type="tests.DurableOutcome",
            source_object_id=dedupe,
            dedupe_key=dedupe,
            payload={
                "strategy": assignment.strategy.name,
                "symbol": "XAUUSD",
                "result": result,
                "currency": "USD",
                "progress_closed": 3,
                "progress_total": 3,
                "volume": "0.03",
                "account_kind": "demo",
                "account_number": str(account.account_number),
            },
        )

    def tp(self, *, dedupe="tp", payload=None):
        safe = {
            "strategy": self.strategy_a.name,
            "symbol": "XAUUSD",
            "result": "2.66",
            "currency": "USD",
            "progress_label": "TP1",
            "progress_closed": 1,
            "progress_total": 3,
            "account_kind": "demo",
            "account_number": "10001",
        }
        if payload:
            safe.update(payload)
        return enqueue_customer_notification(
            user=self.user_a,
            account=self.account_a,
            strategy_assignment=self.assignment_a,
            event_type=CustomerNotification.EventType.TRADE_UPDATED,
            source_object_type="tests.DurableLegEvidence",
            source_object_id=dedupe,
            dedupe_key=dedupe,
            payload=safe,
        )

    def system(self, *, dedupe="system"):
        return enqueue_customer_notification(
            user=self.user_a,
            account=self.account_a,
            event_type=CustomerNotification.EventType.EXECUTION_PROBLEM,
            source_object_type="tests.SafeAccountEvent",
            source_object_id=dedupe,
            dedupe_key=dedupe,
            payload={"message_code": "workspace_attention"},
        )

    def ready_workspace(self):
        workspace = HostedMt5Workspace.objects.create(trading_account=self.account_a)
        type(self.account_a).objects.filter(pk=self.account_a.pk).update(
            workspace_confirmed_at=timezone.now(),
        )
        HostedMt5Workspace.objects.filter(pk=workspace.pk).update(
            proj_account_match=True,
            canonical_state="EXECUTION_READY",
        )
        workspace.refresh_from_db()
        return workspace

    def test_01_winner_default_sends(self):
        row = self.final(dedupe="winner-default")
        self.assertEqual(row.status, CustomerNotification.Status.PENDING)
        self.assertEqual(dispatch_customer_notifications(client=FakeClient())["delivered"], 1)

    def test_02_winner_disabled_suppresses(self):
        CustomerNotificationPreference.objects.filter(user=self.user_a).update(winning_trades=False)
        self.assertEqual(self.final(dedupe="winner-off").status, CustomerNotification.Status.SUPPRESSED)

    def test_03_loser_default_suppresses(self):
        row = self.final(result="-4.25", dedupe="loser-default")
        self.assertEqual(row.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(row.last_error, "event_disabled")

    def test_04_loser_enabled_sends(self):
        CustomerNotificationPreference.objects.filter(user=self.user_a).update(losing_trades=True)
        self.assertEqual(self.final(result="-4.25", dedupe="loser-on").status, CustomerNotification.Status.PENDING)

    def test_05_breakeven_is_non_winning_and_silent_by_default(self):
        row = self.final(result="0", dedupe="breakeven-default")
        self.assertEqual(row.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(row.payload["outcome"], "BREAKEVEN")

    def test_06_safe_tp_progress_sends_without_live_fields(self):
        row = self.tp(payload={
            "side": "BUY", "entry": "4340", "stop_loss": "4300",
            "take_profit": "4350", "signal_id": "copy-this",
        })
        self.assertEqual(row.status, CustomerNotification.Status.PENDING)
        for forbidden in ("side", "entry", "stop_loss", "take_profit", "signal_id"):
            self.assertNotIn(forbidden, row.payload)
        text = render_customer_message(row)
        self.assertIn("TP1 reached", text)
        self.assertNotIn("4340", text)
        self.assertNotIn("4300", text)

    def test_07_incomplete_or_non_partial_tp_is_suppressed(self):
        incomplete = self.tp(dedupe="tp-incomplete", payload={"progress_label": "", "result": ""})
        finalish = self.tp(dedupe="tp-not-partial", payload={"progress_closed": 3})
        self.assertEqual(incomplete.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(finalish.status, CustomerNotification.Status.SUPPRESSED)

    def test_08_trade_open_enqueue_is_prohibited_and_never_delivered(self):
        row = enqueue_customer_notification(
            user=self.user_a,
            account=self.account_a,
            event_type=CustomerNotification.EventType.TRADE_OPENED,
            source_object_type="tests.Attack",
            source_object_id="trade-open",
            dedupe_key="attack-trade-open",
            payload={"side": "BUY", "entry": "4340", "stop_loss": "4300", "take_profit": "4370"},
        )
        self.assertEqual(row.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(row.last_error, "live_signal_prohibited")
        self.assertEqual(row.payload, {})
        self.assertEqual(dispatch_customer_notifications(client=FakeClient())["claimed"], 0)

    def test_09_raw_signal_enqueue_is_prohibited(self):
        row = enqueue_customer_notification(
            user=self.user_a, account=self.account_a, event_type="RAW_SIGNAL",
            source_object_type="tests.Attack", source_object_id="raw", dedupe_key="attack-raw",
            payload={"message": "BUY XAUUSD now"},
        )
        self.assertEqual((row.status, row.last_error, row.payload), (
            CustomerNotification.Status.SUPPRESSED, "live_signal_prohibited", {},
        ))

    def test_10_unknown_and_malformed_events_fail_closed(self):
        unknown = enqueue_customer_notification(
            user=self.user_a, account=self.account_a, event_type="FUTURE_EVENT",
            source_object_type="tests.Attack", source_object_id="future", dedupe_key="attack-future",
            payload={"arbitrary": "BUY now"},
        )
        malformed = enqueue_customer_notification(
            user=self.user_a, account=self.account_a,
            event_type=CustomerNotification.EventType.EXECUTION_PROBLEM,
            source_object_type="tests.Attack", source_object_id="malformed", dedupe_key="attack-malformed",
            payload=["not", "a", "mapping"],
        )
        self.assertEqual(unknown.last_error, "event_not_permitted")
        self.assertEqual(malformed.last_error, "payload_invalid")
        self.assertEqual(malformed.payload, {})
        typed_field_attack = self.tp(
            dedupe="attack-allowed-field",
            payload={"occurred_at": "BUY XAUUSD now at 4340 SL 4300"},
        )
        self.assertEqual(typed_field_attack.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(typed_field_attack.last_error, "durable_metadata_invalid")

        safe_system = enqueue_customer_notification(
            user=self.user_a,
            account=self.account_a,
            event_type=CustomerNotification.EventType.STRATEGY_ENABLED,
            source_object_type="tests.Attack",
            source_object_id="strategy-label",
            dedupe_key="attack-strategy-label",
            payload={"strategy": "BUY XAUUSD now at 4340"},
        )
        self.assertEqual(safe_system.payload["strategy"], "GuvFX")
        self.assertNotIn("4340", render_customer_message(safe_system))

        direct_db_bypass = CustomerNotification.objects.create(
            user=self.user_a,
            account=self.account_a,
            event_type=CustomerNotification.EventType.STRATEGY_ENABLED,
            source_object_type="tests.DirectDatabaseBypass",
            source_object_id="direct",
            dedupe_key="attack-direct-db",
            payload={"strategy": "BUY XAUUSD now at 4340 SL 4300"},
        )
        client = FakeClient()
        dispatch_customer_notifications(client=client)
        direct_db_bypass.refresh_from_db()
        self.assertEqual(direct_db_bypass.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(direct_db_bypass.last_error, "payload_not_canonical")
        self.assertNotIn("4340", "\n".join(text for _, text in client.calls))

    def test_11_system_enabled_sends(self):
        self.assertEqual(self.system().status, CustomerNotification.Status.PENDING)

    def test_12_system_disabled_suppresses_optional_message(self):
        CustomerNotificationPreference.objects.filter(user=self.user_a).update(system_messages=False)
        self.assertEqual(self.system(dedupe="system-off").status, CustomerNotification.Status.SUPPRESSED)

    def test_13_strategy_notifications_disabled_suppress_outcome(self):
        CustomerStrategyNotificationPreference.objects.filter(
            assignment=self.assignment_a,
        ).update(enabled=False)
        row = self.final(dedupe="strategy-off")
        self.assertEqual((row.status, row.last_error), (
            CustomerNotification.Status.SUPPRESSED, "strategy_notifications_disabled",
        ))

    def test_14_strategy_a_enabled_does_not_enable_strategy_b(self):
        row_a = self.final(dedupe="strategy-a")
        row_b = self.final(assignment=self.assignment_b, dedupe="strategy-b")
        self.assertEqual(row_a.status, CustomerNotification.Status.PENDING)
        self.assertEqual(row_b.status, CustomerNotification.Status.SUPPRESSED)

    def test_15_disconnect_suppresses_and_reconnect_only_restores_future_eligibility(self):
        self.account_a.user.customer_telegram_binding.delete()
        old = self.final(dedupe="while-disconnected")
        self.assertEqual(old.status, CustomerNotification.Status.SUPPRESSED)
        self.bind(self.user_a, 333)
        future = self.final(dedupe="after-reconnect")
        old.refresh_from_db()
        self.assertEqual(old.status, CustomerNotification.Status.SUPPRESSED)
        self.assertEqual(future.status, CustomerNotification.Status.PENDING)

    def test_16_language_snapshot_is_deterministic_for_future_only(self):
        first = self.system(dedupe="language-en")
        CustomerNotificationPreference.objects.filter(user=self.user_a).update(language="ja")
        second = self.system(dedupe="language-ja")
        self.assertEqual((first.language, second.language), ("en", "ja"))
        self.assertIn("needs your attention", render_customer_message(first))
        self.assertIn("GuvFXからのお知らせ", render_customer_message(second))

    def test_17_preference_api_has_only_product_categories(self):
        client = APIClient()
        client.force_authenticate(self.user_a)
        body = client.get("/api/customer-notifications/telegram/").json()["preferences"]
        self.assertEqual(set(body), {
            "winning_trades", "losing_trades", "tp_progress", "system_messages", "language",
        })
        rejected = client.patch(
            "/api/customer-notifications/telegram/preferences/", {"trade_opened": True}, format="json",
        )
        self.assertEqual(rejected.status_code, 400)

    def test_18_strategy_preference_is_owner_scoped_and_pending_when_disconnected(self):
        self.account_a.user.customer_telegram_binding.delete()
        state = set_strategy_notification_preference(self.user_a, self.assignment_b, enabled=True)
        self.assertFalse(state["enabled"])
        self.assertTrue(state["pending_enable"])
        with self.captureOnCommitCallbacks(execute=True):
            self.bind(self.user_a, 333)
        preference = CustomerStrategyNotificationPreference.objects.get(assignment=self.assignment_b)
        self.assertTrue(preference.enabled)
        self.assertFalse(preference.pending_enable)
        client = APIClient()
        client.force_authenticate(self.user_b)
        self.assertEqual(client.patch(
            f"/api/customer-notifications/telegram/strategy-preferences/{self.assignment_b.id}/",
            {"enabled": True}, format="json",
        ).status_code, 404)

    def test_19_onboarding_ready_connected_is_one_shot_and_deduped(self):
        workspace = self.ready_workspace()
        first = request_workspace_readiness_notification(self.user_a, language="en")
        second = request_workspace_readiness_notification(self.user_a, language="en")
        self.assertTrue(first["requested"])
        self.assertTrue(first["workspace_ready"])
        self.assertIsNone(first["connect_url"])
        self.assertEqual(WorkspaceReadinessNotificationIntent.objects.filter(workspace=workspace).count(), 1)
        self.assertEqual(CustomerNotification.objects.filter(
            source_object_type="customer_notifications.WorkspaceReadinessNotificationIntent",
        ).count(), 1)
        self.assertTrue(second["requested"])

    def test_20_ready_before_connect_preserves_intent_then_enqueues_after_binding(self):
        workspace = self.ready_workspace()
        self.account_a.user.customer_telegram_binding.delete()
        requested = request_workspace_readiness_notification(self.user_a, language="ja")
        self.assertTrue(requested["requested"])
        self.assertIsNotNone(requested["connect_url"])
        self.assertFalse(CustomerNotification.objects.filter(event_type="WORKSPACE_READY").exists())
        self.bind(self.user_a, 333, language="ja")
        self.assertEqual(fulfill_pending_workspace_readiness(user_id=self.user_a.id), 1)
        row = CustomerNotification.objects.get(event_type="WORKSPACE_READY")
        self.assertEqual(row.language, "ja")
        self.assertEqual(row.payload, {"continue_url": "https://app.guvfx.test/onboarding/hosted"})
        self.assertEqual(row.account_id, workspace.trading_account_id)

    def test_21_disconnect_before_readiness_then_reconnect_delivers_once(self):
        workspace = HostedMt5Workspace.objects.create(trading_account=self.account_a)
        request_workspace_readiness_notification(self.user_a, language="en")
        self.account_a.user.customer_telegram_binding.delete()
        type(self.account_a).objects.filter(pk=self.account_a.pk).update(workspace_confirmed_at=timezone.now())
        HostedMt5Workspace.objects.filter(pk=workspace.pk).update(
            proj_account_match=True, canonical_state="EXECUTION_READY",
        )
        self.assertEqual(fulfill_pending_workspace_readiness(workspace_id=workspace.id), 0)
        self.bind(self.user_a, 444)
        self.assertEqual(fulfill_pending_workspace_readiness(workspace_id=workspace.id), 1)
        self.assertEqual(fulfill_pending_workspace_readiness(workspace_id=workspace.id), 1)
        self.assertEqual(CustomerNotification.objects.filter(event_type="WORKSPACE_READY").count(), 1)

    def test_22_onboarding_intent_and_delivery_are_cross_user_isolated(self):
        workspace = self.ready_workspace()
        request_workspace_readiness_notification(self.user_a, language="en")
        row = CustomerNotification.objects.get(event_type="WORKSPACE_READY")
        self.assertEqual((row.user_id, row.account_id), (self.user_a.id, self.account_a.id))
        self.assertFalse(WorkspaceReadinessNotificationIntent.objects.filter(
            user=self.user_b, workspace=workspace,
        ).exists())

    def test_23_final_result_card_uses_customer_safe_values_only(self):
        row = enqueue_customer_notification(
            user=self.user_a,
            account=self.account_a,
            strategy_assignment=self.assignment_a,
            event_type=CustomerNotification.EventType.TRADE_CLOSED,
            source_object_type="tests.DurableOutcome",
            source_object_id="card",
            dedupe_key="card",
            payload={
                "strategy": "ATTACKER STRATEGY",
                "symbol": "XAUUSD",
                "result": "12.34",
                "currency": "BUYNOW",
                "progress_closed": 3,
                "progress_total": 3,
                "account_kind": "trading",
                "account_number": "99999999",
            },
        )
        model = customer_result_card_model(row)
        self.assertEqual(model["result"], "12.34 USD")
        self.assertEqual(model["account"], "10001")
        self.assertEqual(model["strategy"], "Wayond WIM Strategy")
        self.assertEqual(row.payload["account_kind"], "demo")
        self.assertEqual(model["heading"], "COMPLETED TRADE RESULT")
        png = render_customer_result_card(row)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        row.language = "ja"
        japanese = customer_result_card_model(row)
        self.assertEqual((japanese["heading"], japanese["result_label"]), ("確定した取引結果", "確定損益"))

    def test_24_customer_language_and_preference_cannot_affect_other_customer(self):
        CustomerNotificationPreference.objects.filter(user=self.user_a).update(
            language="ja", system_messages=False,
        )
        row = enqueue_customer_notification(
            user=self.user_b, account=self.account_b,
            event_type=CustomerNotification.EventType.EXECUTION_PROBLEM,
            source_object_type="tests.SafeAccountEvent", source_object_id="b", dedupe_key="customer-b",
            payload={"message_code": "workspace_attention"},
        )
        self.assertEqual((row.status, row.language), (CustomerNotification.Status.PENDING, "en"))

    def test_25_wims_stakeholder_transport_is_not_imported_or_used(self):
        from pathlib import Path
        root = Path(__file__).parent
        source = "\n".join((root / name).read_text() for name in (
            "cards.py", "delivery.py", "event_sources.py", "services.py",
        )).lower()
        for forbidden in (
            "from wims", "import wims", "consumptioncontract",
            'getattr(settings, "telegram_chat_id"', "validation_agent_telegram",
        ):
            self.assertNotIn(forbidden, source)

    def test_26_notification_preferences_never_mutate_strategy_or_execution_state(self):
        before = (
            self.assignment_a.is_active,
            self.assignment_a.execution_mode,
            self.account_a.is_active,
        )
        set_strategy_notification_preference(self.user_a, self.assignment_a, enabled=False)
        CustomerNotificationPreference.objects.filter(user=self.user_a).update(
            winning_trades=False, losing_trades=True, tp_progress=False, system_messages=False,
        )
        self.assignment_a.refresh_from_db()
        self.account_a.refresh_from_db()
        after = (
            self.assignment_a.is_active,
            self.assignment_a.execution_mode,
            self.account_a.is_active,
        )
        self.assertEqual(after, before)
