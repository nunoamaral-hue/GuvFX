"""ADR-0034 Execution Engine (G9) — account-switch pause / safe resume / drop-not-queue (readiness-driven)."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from trading.models import BrokerServer, TradingAccount

from execution import hosted_switch_policy as SW
from execution.readiness import PERSISTENT_WORKSPACE
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()


def _acct(login="700900"):
    user = U.objects.create_user(username=f"s{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    return TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                         is_demo=True, broker_server=srv,
                                         readiness_provider=PERSISTENT_WORKSPACE)


def _ws(acct, **kw):
    base = dict(canonical_state=S.EXECUTION_READY, proj_connected=True, proj_trade_allowed=True,
                proj_account_match=True, proj_execution_ready=True, last_decision_at=timezone.now(),
                execution_enabled=True)
    base.update(kw)
    return HostedMt5Workspace.objects.create(trading_account=acct, **base)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class SwitchPolicyTests(TestCase):
    def test_matched_ready_armed_is_not_paused(self):
        acct = _acct(); _ws(acct); acct.refresh_from_db()
        self.assertFalse(SW.hosted_execution_effectively_paused(acct))
        self.assertFalse(SW.should_drop_stale_hosted_signal(acct))

    def test_account_switch_mismatch_pauses_and_drops(self):
        acct = _acct(); _ws(acct, proj_account_match=False); acct.refresh_from_db()
        self.assertTrue(SW.hosted_execution_effectively_paused(acct))     # switched away → paused
        self.assertTrue(SW.should_drop_stale_hosted_signal(acct))         # stale signal dropped, not queued

    def test_disconnect_pauses(self):
        acct = _acct(); _ws(acct, proj_connected=False); acct.refresh_from_db()
        self.assertTrue(SW.hosted_execution_effectively_paused(acct))

    def test_stale_observation_pauses(self):
        acct = _acct()
        _ws(acct, last_decision_at=timezone.now() - timezone.timedelta(seconds=10_000))
        acct.refresh_from_db()
        self.assertTrue(SW.hosted_execution_effectively_paused(acct))

    def test_return_to_expected_account_resumes_only_when_all_conditions(self):
        acct = _acct(); ws = _ws(acct, proj_account_match=False); acct.refresh_from_db()
        self.assertTrue(SW.hosted_execution_effectively_paused(acct))     # switched away
        # expected account returns + fresh:
        ws.proj_account_match = True
        ws.last_decision_at = timezone.now()
        ws.save(update_fields=["proj_account_match", "last_decision_at", "updated_at"])
        acct.refresh_from_db()
        self.assertFalse(SW.hosted_execution_effectively_paused(acct))    # safe resume (re-eligibility)

    def test_return_but_still_disconnected_does_not_resume(self):
        acct = _acct(); ws = _ws(acct, proj_account_match=False, proj_connected=False); acct.refresh_from_db()
        ws.proj_account_match = True  # account matches again but still not connected
        ws.save(update_fields=["proj_account_match", "updated_at"])
        acct.refresh_from_db()
        self.assertTrue(SW.hosted_execution_effectively_paused(acct))     # no blind resume while disconnected

    def test_unarmed_return_does_not_resume(self):
        acct = _acct(); _ws(acct, execution_enabled=False); acct.refresh_from_db()
        self.assertTrue(SW.hosted_execution_effectively_paused(acct))     # disarmed ⇒ stays paused

    def test_status_is_secret_free(self):
        acct = _acct(); _ws(acct); acct.refresh_from_db()
        st = SW.hosted_switch_status(acct)
        self.assertEqual(st["stale_signal_policy"], "drop")
        for forbidden in ("password", "token", "secret", "700900"):
            self.assertNotIn(forbidden, str(st))
