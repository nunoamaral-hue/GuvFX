"""B1 — operations summary: read-only aggregation for the /operations status page.

Proves the summary is source-aware, fail-safe on the bridge, leaks no secrets, mutates nothing,
and rolls the overall state up from the worst signal. The endpoint is staff-only.
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.permissions import IsAdminUser

from reliability.services import operations_summary as ops
from reliability.views import OperationsSummaryView
from execution.models import SignalSourceConfig
from trading.models import TradingAccount, Trade

User = get_user_model()


class OperationsSummaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="IS6 Demo (1302561)", account_number="1302561",
            is_demo=True, public_display_name="IS6FX")
        SignalSourceConfig.objects.create(
            source="ti_signals", auto_demo_execution_enabled=True,
            total_lot_target=Decimal("1.20"), max_lot_per_leg=Decimal("0.40"))
        SignalSourceConfig.objects.create(source="wayond", auto_demo_execution_enabled=True)

    def _summary(self):
        # no bridge env → broker metrics must fail SAFE (never raise, never network)
        with mock.patch.dict("os.environ", {"GUVFX_WINDOWS_AGENT_BASE_URL": "", "GUVFX_AGENT_URL": "",
                                            "WINDOWS_AGENT_BASE": ""}, clear=False):
            return ops.build_operations_summary()

    def test_shape_and_read_only(self):
        before = Trade.objects.count()
        s = self._summary()
        for k in ("generated_at", "overall", "control", "components", "heartbeats", "strategies",
                  "positions", "dispatch", "broker", "alerts"):
            self.assertIn(k, s)
        self.assertEqual(Trade.objects.count(), before)  # builds/mutates nothing

    def test_source_aware_metrics_not_combined(self):
        rows = self._summary()["strategies"]
        keys = {r["key"] for r in rows}
        self.assertIn("ti_signals", keys)
        self.assertIn("wayond", keys)
        ti = next(r for r in rows if r["key"] == "ti_signals")
        self.assertEqual(ti["source_label"], "TI Signals")
        self.assertEqual(ti["per_leg_lot"], "0.40")
        for r in rows:  # each row carries its own metric set (never one combined total)
            for f in ("signals_today", "wins", "losses", "realised_pnl", "cards_sent"):
                self.assertIn(f, r)

    def test_broker_metrics_fail_safe_without_bridge(self):
        broker = self._summary()["broker"]
        self.assertFalse(broker["reachable"])       # no bridge → False, not an exception
        self.assertEqual(broker["account"], "IS6FX")  # public label, not the raw number

    def test_no_secrets_leaked(self):
        blob = json.dumps(self._summary()).lower()
        for banned in ("token", "password", "secret", "fernet", "bot_token"):
            self.assertNotIn(banned, blob)

    def test_overall_rolls_up_worst_signal(self):
        from reliability.models import Heartbeat
        # a critical heartbeat stale well past its interval → overall not HEALTHY
        Heartbeat.objects.create(
            source="monitor_chain", last_beat_at=timezone.now() - timezone.timedelta(hours=1),
            expected_interval_s=90)
        self.assertIn(self._summary()["overall"], ("WARNING", "CRITICAL"))

    def test_endpoint_is_staff_only(self):
        self.assertEqual(OperationsSummaryView.permission_classes, [IsAdminUser])


class OperationsD1toD4Tests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="d", email="d@x.invalid", password="x", is_staff=True)
        self.acct = TradingAccount.objects.create(
            user=self.user, name="IS6 Demo (1302561)", account_number="1302561",
            is_demo=True, is_active=True, public_display_name="IS6FX")
        SignalSourceConfig.objects.create(
            source="ti_signals", auto_demo_execution_enabled=True,
            total_lot_target=Decimal("1.20"), max_lot_per_leg=Decimal("0.40"),
            max_total_lot=Decimal("1.20"), daily_group_cap=0)

    def _summary(self):
        with mock.patch.dict("os.environ", {"GUVFX_WINDOWS_AGENT_BASE_URL": ""}, clear=False):
            return ops.build_operations_summary()

    def test_infra_block_and_core_flag(self):
        infra = self._summary()["infra"]
        self.assertIn("reliability_core_enabled", infra)
        self.assertEqual(infra["listener"]["status"], "UNKNOWN")
        self.assertEqual(infra["shadow_worker"]["status"], "UNKNOWN")
        self.assertEqual(infra["postgres"]["status"], "HEALTHY")

    def test_d1_strategy_fields(self):
        ti = next(r for r in self._summary()["strategies"] if r["key"] == "ti_signals")
        for f in ("assignment_id", "assignment_active", "provider_enabled", "mode", "total_lot",
                  "daily_cap", "plans_promoted", "trades_closed", "cards_delivered",
                  "rejection_reasons", "last_execution_at", "last_notification_at"):
            self.assertIn(f, ti)
        self.assertEqual(ti["daily_cap"], "unlimited")   # 0 → unlimited
        self.assertTrue(ti["provider_enabled"])

    def test_alerts_carry_id(self):
        from reliability.models import AlertEvent
        AlertEvent.objects.create(severity="WARN", component="EXECUTION_PIPELINE",
            title="x", dedup_key="k1", status="OPEN")
        alerts = self._summary()["alerts"]
        self.assertTrue(alerts and "id" in alerts[0])

    def test_staff_actions_admin_only(self):
        from reliability.views import AlertAcknowledgeView, AssignmentSetActiveView
        self.assertEqual(AlertAcknowledgeView.permission_classes, [IsAdminUser])
        self.assertEqual(AssignmentSetActiveView.permission_classes, [IsAdminUser])

    def test_assignment_toggle_flips_only_is_active(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from reliability.views import AssignmentSetActiveView
        from strategies.models import Strategy, StrategyAssignment
        strat = Strategy.objects.create(owner=self.user, name="TI")
        asn = StrategyAssignment.objects.create(
            strategy=strat, account=self.acct, signal_source="ti_signals", is_active=True)
        mode_before = asn.execution_mode
        factory = APIRequestFactory()
        req = factory.post("/", {"active": False}, format="json")
        force_authenticate(req, user=self.user)
        resp = AssignmentSetActiveView.as_view()(req, pk=asn.id)
        self.assertEqual(resp.status_code, 200)
        asn.refresh_from_db()
        self.assertFalse(asn.is_active)
        self.assertEqual(asn.execution_mode, mode_before)  # nothing else changed

    def test_assignment_toggle_refuses_non_source_bound(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from reliability.views import AssignmentSetActiveView
        from strategies.models import Strategy, StrategyAssignment
        strat = Strategy.objects.create(owner=self.user, name="Manual")
        asn = StrategyAssignment.objects.create(
            strategy=strat, account=self.acct, signal_source="", is_active=True)
        factory = APIRequestFactory()
        req = factory.post("/", {"active": False}, format="json")
        force_authenticate(req, user=self.user)
        resp = AssignmentSetActiveView.as_view()(req, pk=asn.id)
        self.assertEqual(resp.status_code, 400)
