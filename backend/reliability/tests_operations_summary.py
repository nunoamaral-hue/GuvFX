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


class SignalDispositionBlockTests(TestCase):
    """GFX-PKT-TI-SIGNALS-NON-EXECUTION-INCIDENT — the disposition block must isolate GENUINE silent
    losses: planned / durably-deferred / in-flight (within the settle window) never inflate it."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(
            username="d", email="d@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D9", is_demo=True)
        SignalSourceConfig.objects.create(source="ti_signals", auto_demo_execution_enabled=True)

    def _appr(self, mid, *, age_s):
        from signal_intake.models import PendingSignalApproval
        from datetime import timedelta
        a = PendingSignalApproval.objects.create(
            source="ti_signals", message_id=mid, symbol="XAUUSD", direction="BUY",
            stop_loss="4050", take_profits=["4060"], status=PendingSignalApproval.Status.APPROVED)
        PendingSignalApproval.objects.filter(id=a.id).update(
            created_at=timezone.now() - timedelta(seconds=age_s))
        return PendingSignalApproval.objects.get(id=a.id)

    def test_in_flight_signal_not_counted_as_silent_loss(self):
        from execution.execution_health import UNPLANNED_SIGNAL_ALERT_SECONDS
        # Just-approved (within the planning-settle window), no plan yet → in-flight, NOT a loss.
        self._appr("f1", age_s=5)
        block = ops._signal_disposition_block(timezone.now())
        self.assertEqual(block["silent_loss_total"], 0)
        self.assertEqual(block["by_source"]["ti_signals"]["in_flight"], 1)
        self.assertEqual(block["by_source"]["ti_signals"]["unplanned_no_reason"], 0)

    def test_settled_unplanned_no_reason_is_silent_loss(self):
        from execution.execution_health import UNPLANNED_SIGNAL_ALERT_SECONDS
        self._appr("f2", age_s=UNPLANNED_SIGNAL_ALERT_SECONDS + 60)  # settled, no plan, no reason
        block = ops._signal_disposition_block(timezone.now())
        self.assertEqual(block["silent_loss_total"], 1)
        self.assertEqual(block["by_source"]["ti_signals"]["unplanned_no_reason"], 1)

    def test_durably_deferred_is_not_silent_loss(self):
        from execution.execution_health import UNPLANNED_SIGNAL_ALERT_SECONDS
        from signal_intake.models import SignalAuditEvent
        a = self._appr("f3", age_s=UNPLANNED_SIGNAL_ALERT_SECONDS + 60)
        SignalAuditEvent.objects.create(
            event=SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED, approval=a,
            detail={"reason": "auto-demo_rejected:plan_integrity_error"})
        block = ops._signal_disposition_block(timezone.now())
        self.assertEqual(block["silent_loss_total"], 0)
        self.assertEqual(block["by_source"]["ti_signals"]["deferred"], 1)
        self.assertEqual(block["by_source"]["ti_signals"]["deferral_reasons"]
                         ["auto-demo_rejected:plan_integrity_error"], 1)


class ExecutionJobsBlockTests(TestCase):
    """GFX-PKT-POST-INCIDENT — /operations surfaces orphaned/stuck place-order jobs."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(username="ej", email="ej@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="EJ1", is_demo=True)

    def test_orphaned_running_place_order_surfaced(self):
        from execution.models import ExecutionJob
        from datetime import timedelta
        j = ExecutionJob.objects.create(job_type="PLACE_ORDER", account=self.acct, status="RUNNING",
                                        payload={"plan_id": 1, "leg_index": 1})
        ExecutionJob.objects.filter(id=j.id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=120))
        block = ops._execution_jobs_block(timezone.now())["place_order"]
        self.assertEqual(block["running"], 1)
        self.assertEqual(block["orphaned_running"], 1)


class NotificationReconciliationBlockTests(TestCase):
    """GFX-PKT-POST-DEPLOY WS-E — WIN==candidate==SENT==transmitted per source over the SETTLED window,
    cohorts CO-ANCHORED to the WIN outcome (no boundary flap), POSTURE-aware (real vs dry-run vs
    disabled); a missing/duplicate card surfaces as a mismatch that rolls the summary up to WARNING."""

    REAL = {"enabled": True, "transport": "telegram-real"}
    DRYRUN = {"enabled": True, "transport": "dry-run"}
    OFF = {"enabled": False, "transport": "dry-run"}

    def setUp(self):
        self.user = User.objects.create_user(username="nr", email="nr@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="NR1", is_demo=True)

    def _win(self, i, *, deliver=True, sent=True, dup=False, age_seconds=3600, src="ti_signals"):
        """One WIN chain. Only the OUTCOME's created_at anchors the window (candidate/delivery join
        back through it), so those may keep 'now' timestamps — exactly the boundary the fix must
        tolerate. ``age_seconds`` < 180 makes an UNSETTLED (in-flight) win."""
        import datetime
        from execution.models import (TradeOutcomeRecord, NotificationCandidate, NotificationDelivery)
        anchor = timezone.now() - datetime.timedelta(seconds=age_seconds)
        tr = Trade.objects.create(
            account=self.acct, symbol="XAUUSD", side="BUY", volume=Decimal("0.40"), ticket=f"nr{i}",
            open_time=anchor, open_price=Decimal("4000"), close_time=anchor,
            close_price=Decimal("4010"), profit=Decimal("100"), comment=f"WAYnr{i}")
        rec = TradeOutcomeRecord.objects.create(
            trade=tr, outcome="WIN", net_pnl=Decimal("100"), signal_source=src, correlation_id=f"cid{i}")
        cand = NotificationCandidate.objects.create(
            outcome_record=rec, signal_source=src, status=("SENT" if sent else "PENDING"),
            net_pnl=Decimal("100"), correlation_id=f"cid{i}")
        if deliver:
            for _ in range(2 if dup else 1):
                NotificationDelivery.objects.create(
                    candidate=cand, transmitted=True, transport="telegram-real", correlation_id=f"cid{i}")
        TradeOutcomeRecord.objects.filter(id=rec.id).update(created_at=anchor)  # the only anchor
        return rec, cand

    def test_healthy_chain_reconciles_exactly_once(self):
        for i in range(3):
            self._win(i)
        block = ops._notification_reconciliation_block(timezone.now(), self.REAL)
        self.assertFalse(block["any_mismatch"])
        ti = block["by_source"]["ti_signals"]
        self.assertEqual((ti["win_outcomes"], ti["candidates"], ti["sent"], ti["transmitted"]), (3, 3, 3, 3))
        self.assertTrue(ti["exactly_once"])

    def test_fresh_win_within_settle_window_is_excluded_no_flap(self):
        # Finding-3 regression: a just-won trade (<180s) with no card yet must NOT flap the WARNING.
        self._win(0)                                     # settled healthy chain
        self._win(9, deliver=False, sent=False, age_seconds=20)  # fresh WIN-only, unsettled
        block = ops._notification_reconciliation_block(timezone.now(), self.REAL)
        self.assertFalse(block["any_mismatch"])          # fresh win excluded → no false mismatch
        self.assertEqual(block["by_source"]["ti_signals"]["win_outcomes"], 1)

    def test_candidate_committing_after_settle_does_not_flap(self):
        # The exact straddle: outcome just inside the window, its candidate/delivery timestamps 'now'
        # (after settle). Co-anchoring on the outcome must still reconcile them as one cohort.
        self._win(0, age_seconds=181)                    # outcome just past settle; card ts = now
        block = ops._notification_reconciliation_block(timezone.now(), self.REAL)
        self.assertFalse(block["any_mismatch"])

    def test_missing_delivery_flags_mismatch(self):
        self._win(0, deliver=True)
        self._win(1, deliver=False)   # WIN + SENT candidate but no transmitted delivery
        block = ops._notification_reconciliation_block(timezone.now(), self.REAL)
        self.assertTrue(block["any_mismatch"])
        self.assertFalse(block["by_source"]["ti_signals"]["exactly_once"])

    def test_duplicate_delivery_flags_mismatch(self):
        self._win(0, deliver=True, dup=True)   # two transmitted deliveries for one candidate
        block = ops._notification_reconciliation_block(timezone.now(), self.REAL)
        self.assertEqual(block["by_source"]["ti_signals"]["duplicates"], 1)
        self.assertTrue(block["any_mismatch"])

    def test_dryrun_sent_without_transmit_is_healthy(self):
        self._win(0, deliver=False, sent=True)   # dry-run: SENT, nothing transmitted (by design)
        block = ops._notification_reconciliation_block(timezone.now(), self.DRYRUN)
        self.assertEqual(block["transport_mode"], "dry-run")
        self.assertFalse(block["any_mismatch"])

    def test_dispatch_disabled_pending_is_healthy(self):
        self._win(0, deliver=False, sent=False)  # dispatch off: candidate PENDING, no delivery
        block = ops._notification_reconciliation_block(timezone.now(), self.OFF)
        self.assertEqual(block["transport_mode"], "disabled")
        self.assertFalse(block["any_mismatch"])

    def test_stuck_candidate_flags_mismatch_real_transport(self):
        self._win(0, deliver=False, sent=False)  # candidate stuck PENDING under real transport → loss
        block = ops._notification_reconciliation_block(timezone.now(), self.REAL)
        ti = block["by_source"]["ti_signals"]
        self.assertEqual(ti["stuck"], 1)
        self.assertTrue(block["any_mismatch"])

    def test_real_transport_mismatch_escalates_summary(self):
        # Finding-5: a real transmit loss must roll the summary up to WARNING. The broker is mocked
        # REACHABLE so the ONLY warning source is the notification mismatch — isolating the escalation.
        import os
        from execution.models import SignalSourceConfig
        SignalSourceConfig.objects.get_or_create(
            source="ti_signals", defaults={"auto_demo_execution_enabled": True})
        self._win(0, deliver=False)   # SENT but never transmitted under real transport → loss
        with mock.patch.object(ops, "_broker_metrics", return_value={"reachable": True}), \
                mock.patch.dict(os.environ, {
                    "NOTIFICATION_DISPATCH_ENABLED": "1",
                    "NOTIFICATION_DISPATCH_TRANSPORT": "telegram-real"}, clear=False):
            s = ops.build_operations_summary()
        self.assertTrue(s["notification_reconciliation"]["any_mismatch"])
        self.assertEqual(s["overall"], "WARNING")   # broker healthy → mismatch is the sole warning


class RiskStateBlockTests(TestCase):
    """GFX-PKT-POST-DEPLOY WS-F — the dashboard explains a daily_drawdown_hit: today's realised PnL
    vs the configured limit, and whether the circuit-breaker is tripped."""

    def setUp(self):
        self.user = User.objects.create_user(username="rs", email="rs@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="IS6 Demo (1302561)", account_number="1302561",
            is_demo=True, public_display_name="IS6FX")
        from strategies.models import Strategy, StrategyAssignment
        strat = Strategy.objects.create(owner=self.user, name="TI")
        StrategyAssignment.objects.create(
            strategy=strat, account=self.acct, signal_source="ti_signals", is_active=True)

    def test_drawdown_not_tripped_under_limit(self):
        Trade.objects.create(account=self.acct, symbol="XAUUSD", side="BUY", volume=Decimal("1.20"),
                             ticket="rs1", open_time=timezone.now(), open_price=Decimal("4000"),
                             close_time=timezone.now(), close_price=Decimal("3990"), profit=Decimal("-502.80"))
        with mock.patch("execution.risk_controls.MAX_DAILY_DRAWDOWN_ABS", Decimal("2000")):
            block = ops._risk_state_block(timezone.now())
        acct = block["accounts"][0]
        self.assertEqual(acct["account"], "IS6FX")
        self.assertFalse(acct["drawdown_tripped"])

    def test_drawdown_tripped_over_limit(self):
        Trade.objects.create(account=self.acct, symbol="XAUUSD", side="BUY", volume=Decimal("1.20"),
                             ticket="rs2", open_time=timezone.now(), open_price=Decimal("4000"),
                             close_time=timezone.now(), close_price=Decimal("3900"), profit=Decimal("-2100"))
        with mock.patch("execution.risk_controls.MAX_DAILY_DRAWDOWN_ABS", Decimal("2000")):
            block = ops._risk_state_block(timezone.now())
        self.assertTrue(block["accounts"][0]["drawdown_tripped"])


class ProtectionWatcherBlockTests(TestCase):
    """GFX-PKT-TP-PROTECTION-LATENCY WS-L — the /operations protection_watcher block: state, cadence,
    heartbeat staleness (only a fault when armed), and protection-sync ingestion health."""

    def _beat(self, age_s, state="active", interval=5):
        from reliability.models import Heartbeat
        Heartbeat.objects.update_or_create(
            source="tp_protection_watcher",
            defaults={"last_beat_at": timezone.now() - __import__("datetime").timedelta(seconds=age_s),
                      "expected_interval_s": interval, "detail": {"state": state, "cadence_s": 1, "open_legs": 2}})

    def test_fresh_armed_watcher_not_stale(self):
        self._beat(age_s=2, state="active", interval=5)
        with mock.patch.dict("os.environ", {"TP_WATCHER_ENABLED": "1"}, clear=False):
            block = ops._protection_watcher_block(timezone.now())
        self.assertTrue(block["armed"])
        self.assertEqual(block["state"], "active")
        self.assertFalse(block["stale"])

    def test_stale_armed_watcher_flagged(self):
        self._beat(age_s=400, state="active", interval=5)
        with mock.patch.dict("os.environ", {"TP_WATCHER_ENABLED": "1"}, clear=False):
            block = ops._protection_watcher_block(timezone.now())
        self.assertTrue(block["stale"])

    def test_disabled_watcher_absent_hb_not_stale(self):
        with mock.patch.dict("os.environ", {"TP_WATCHER_ENABLED": "0"}, clear=False):
            block = ops._protection_watcher_block(timezone.now())
        self.assertFalse(block["armed"])
        self.assertFalse(block["stale"])
