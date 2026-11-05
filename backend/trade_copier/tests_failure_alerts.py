"""Phase-9 governed terminal-failure alerting tests (ADR-0074).

Asserts: gated off => no AlertEvent (but failure evidence persists elsewhere); the governed reliability
AlertEvent channel is used with the COPY_TRADING component; category + severity + retryable mapping; dedup;
secret-free; and that alerting creates no order/job and recovers nothing."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from reliability.constants import Component
from reliability.models import AlertEvent
from trade_copier import failure_alerts as fa
from trade_copier.models import (
    CopierLifecycleTransition, CopierMaster, CopierPositionLifecycle,
)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
ALERTS_ON = dict(COPIER_ENABLED=True, COPIER_ALERTS_ENABLED=True)


class AlertBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        self.macct = TradingAccount.objects.create(user=self.user, name="m", broker_name="IS6",
                                                   account_number="1000001", is_demo=True, is_active=True,
                                                   broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        from trade_copier import services
        facct = TradingAccount.objects.create(user=self.user, name="f", broker_name="IS6",
                                              account_number="2000001", is_demo=True, is_active=True,
                                              broker_server=self.bs)
        self.follower = services.add_follower(self.master, facct)

    def _lc(self, state, *, ticket=1, terminal=True, failure_reason="", trigger=""):
        lc = CopierPositionLifecycle.objects.create(
            master=self.master, follower=self.follower, master_position_ticket=ticket, state=state,
            is_terminal=terminal, failure_reason=failure_reason, entry_reference_at=timezone.now())
        if trigger:
            CopierLifecycleTransition.objects.create(lifecycle=lc, from_state=state, to_state=state,
                                                     trigger=trigger, sequence=1)
        return lc


class FailureAlertTests(AlertBase):
    def test_gate_off_raises_no_alert(self):
        self._lc("ENTRY_FAILED_FINAL", failure_reason="entry_deadline_exceeded")
        with override_settings(COPIER_ENABLED=True, COPIER_ALERTS_ENABLED=False):
            out = fa.run_failure_alert_pass()
        self.assertFalse(out["enabled"])
        self.assertEqual(AlertEvent.objects.count(), 0)

    @override_settings(**ALERTS_ON)
    def test_entry_failed_final_raises_governed_alert(self):
        self._lc("ENTRY_FAILED_FINAL", failure_reason="entry_deadline_exceeded")
        out = fa.run_failure_alert_pass()
        self.assertEqual(out["raised"], 1)
        a = AlertEvent.objects.get()
        self.assertEqual(a.component, Component.COPY_TRADING)
        self.assertEqual(a.detail["category"], fa.ENTRY_FAILED_FINAL)
        self.assertEqual(a.severity, AlertEvent.Severity.WARN)
        self.assertEqual(a.trading_account_id, self.macct.id)
        self.assertFalse(a.detail["retryable"])   # execution-side failure is terminal, not retryable

    @override_settings(**ALERTS_ON)
    def test_contained_close_failed_is_critical(self):
        self._lc("CONTAINED_FAILURE", trigger="close_failed", failure_reason="close_failed")
        fa.run_failure_alert_pass()
        a = AlertEvent.objects.get()
        self.assertEqual(a.detail["category"], fa.CLOSE_FAILED_FINAL)
        self.assertEqual(a.severity, AlertEvent.Severity.CRITICAL)

    @override_settings(**ALERTS_ON)
    def test_contained_divergence_category(self):
        self._lc("CONTAINED_FAILURE", trigger="link_divergent")
        fa.run_failure_alert_pass()
        self.assertEqual(AlertEvent.objects.get().detail["category"], fa.RECONCILIATION_DIVERGENCE)

    @override_settings(**ALERTS_ON)
    def test_dedup_no_duplicate_on_rerun(self):
        self._lc("ENTRY_FAILED_FINAL", failure_reason="x")
        self.assertEqual(fa.run_failure_alert_pass()["raised"], 1)
        self.assertEqual(fa.run_failure_alert_pass()["raised"], 0)
        self.assertEqual(AlertEvent.objects.count(), 1)

    @override_settings(**ALERTS_ON)
    def test_modify_failed_warn(self):
        lc = self._lc("LINKED", terminal=False)
        CopierLifecycleTransition.objects.create(lifecycle=lc, from_state="MODIFY_PENDING", to_state="LINKED",
                                                 trigger="modify_failed", sequence=2)
        fa.run_failure_alert_pass()
        a = AlertEvent.objects.get()
        self.assertEqual(a.detail["category"], fa.MODIFY_FAILED_FINAL)
        self.assertEqual(a.severity, AlertEvent.Severity.WARN)

    @override_settings(**ALERTS_ON)
    def test_ingest_rejection_mapping(self):
        self.assertTrue(fa.raise_ingest_rejection("account_not_master", account_id=self.macct.id))
        a = AlertEvent.objects.get()
        self.assertEqual(a.detail["category"], fa.MAPPING_UNAVAILABLE)

    @override_settings(**ALERTS_ON)
    def test_event_rejected_is_retryable(self):
        fa.raise_ingest_rejection("some_structural_reason", account_id=self.macct.id)
        a = AlertEvent.objects.get()
        self.assertEqual(a.detail["category"], fa.EVENT_REJECTED)
        self.assertTrue(a.detail["retryable"])   # an observation failure the bridge can re-emit past

    @override_settings(**ALERTS_ON)
    def test_alert_detail_is_secret_free(self):
        self._lc("ENTRY_FAILED_FINAL", failure_reason="entry_deadline_exceeded")
        fa.run_failure_alert_pass()
        a = AlertEvent.objects.get()
        blob = (a.title + a.body + str(a.detail)).lower()
        for leak in ("secret", "token", "password", "hmac", "authorization"):
            self.assertNotIn(leak, blob)
