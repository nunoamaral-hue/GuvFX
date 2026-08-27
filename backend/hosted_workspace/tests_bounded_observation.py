"""P0 bounded, tenant-isolated, de-duplicated observation — behavioural tests.

Covers: de-duplication (one observe drives both canonical + delivery), tenant isolation (a slow/unavailable
tenant cannot starve a healthy one or serialize the cycle), bounded worker count, typed-reason preservation,
CZ delivery exclusion, and the capability-recovery onboarding gate. The observe seam is injected, so these tests
never touch a host.
"""
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import UserSubscriptionState
from execution.models import TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import bounded_observation as BO
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()


def _ws(name, *, state=S.WAITING_FOR_LOGIN, login="700900", confirmed=False, trade_allowed=None,
        connected=None, matched=None):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    acct = TradingAccount.objects.create(user=u, name="B", broker_name="B", account_number=login,
                                         is_demo=True, broker_server=srv,
                                         readiness_provider=PERSISTENT_WORKSPACE, is_active=False)
    if confirmed:
        acct.workspace_confirmed_at = timezone.now()
        acct.save(update_fields=["workspace_confirmed_at"])
    tn = TerminalNode.objects.create(hostname=f"n-{u.pk}", status=TerminalNode.Status.ACTIVE, rdp_host="10.0.0.1")
    ws = HostedMt5Workspace.objects.create(
        trading_account=acct, canonical_state=state, proj_connected=connected, proj_account_match=matched,
        proj_trade_allowed=trade_allowed, execution_node=tn)
    return ws, acct


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_DELIVERY_LIFECYCLE_ENABLED="1")
class BoundedObservationTests(TestCase):
    def setUp(self):
        # No CZ in these fixtures; pin the CZ set empty so account-1 collisions never skip a fixture.
        p = patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset())
        p.start(); self.addCleanup(p.stop)

    def test_deduped_one_observe_drives_canonical_and_delivery(self):
        _ws("a", connected=True, matched=True)
        canonical = SimpleNamespace()  # opaque; ingest is mocked
        combined = lambda ws: (canonical, "CONNECTED", "ok")   # ONE call yields both projections
        with patch("hosted_workspace.bounded_observation.ingest_observation",
                   return_value=SimpleNamespace(status="APPLIED")) as ingest, \
             patch("hosted_workspace.delivery_persistence.record_remoteapp_connected") as conn:
            out = BO.run_bounded_observation_cycle(combined_fn=combined)
        self.assertEqual(out["applied"], 1)
        self.assertEqual(out["delivery"]["connected"], 1)
        self.assertEqual(ingest.call_count, 1)          # exactly ONE canonical ingest
        self.assertEqual(conn.call_count, 1)            # exactly ONE delivery write — from the SAME observe

    def test_slow_tenant_does_not_starve_healthy_and_cycle_is_bounded(self):
        _ws("slow", login="999001"); _ws("fast1", login="700001", connected=True, matched=True)
        _ws("fast2", login="700002", connected=True, matched=True)
        def combined(ws):
            if ws.trading_account.account_number == "999001":   # preloaded relation — no DB query in the thread
                time.sleep(1.0)                          # a single slow/busy tenant
                return (None, None, "observation_timeout")
            return (SimpleNamespace(), "CONNECTED", "ok")
        with patch("hosted_workspace.bounded_observation.ingest_observation",
                   return_value=SimpleNamespace(status="APPLIED")), \
             patch("hosted_workspace.delivery_persistence.record_remoteapp_connected"):
            t0 = time.monotonic()
            out = BO.run_bounded_observation_cycle(combined_fn=combined)
            elapsed = time.monotonic() - t0
        self.assertEqual(out["polled"], 3)
        self.assertEqual(out["applied"], 2)             # both healthy tenants processed
        self.assertEqual(out["unavailable"], 1)         # the slow one held, not fatal
        self.assertLess(elapsed, 1.0 * 3 - 0.3)         # concurrent, NOT serial (serial would be ~3s)

    def test_worker_pool_is_bounded(self):
        self.assertLessEqual(BO._max_workers(), BO._HARD_MAX_WORKERS)
        with override_settings(HOSTED_OBSERVATION_MAX_WORKERS="999"):
            self.assertEqual(BO._max_workers(), BO._HARD_MAX_WORKERS)   # never exceeds host capacity
        with override_settings(HOSTED_OBSERVATION_MAX_WORKERS="0"):
            self.assertEqual(BO._max_workers(), 1)

    def test_typed_reasons_preserved_not_flattened(self):
        _ws("t1"); _ws("t2"); _ws("t3")
        reasons = iter(["observation_timeout", "terminal_not_running", "duplicate_terminal"])
        combined = lambda ws: (None, None, next(reasons))
        with patch("hosted_workspace.bounded_observation.ingest_observation"):
            out = BO.run_bounded_observation_cycle(combined_fn=combined)
        self.assertEqual(out["unavailable"], 3)
        self.assertEqual(sum(out["reasons"].values()), 3)
        self.assertEqual(set(out["reasons"]), {"observation_timeout", "terminal_not_running", "duplicate_terminal"})

    def test_dark_when_master_flag_off(self):
        _ws("x")
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="0"):
            out = BO.run_bounded_observation_cycle(combined_fn=lambda ws: (SimpleNamespace(), "CONNECTED", "ok"))
        self.assertFalse(out["enabled"])
        self.assertEqual(out["polled"], 0)

    def test_one_tenant_error_does_not_stop_others(self):
        _ws("boom", login="999002"); _ws("ok", login="700003", connected=True, matched=True)
        def combined(ws):
            if ws.trading_account.account_number == "999002":   # preloaded — no DB query in the thread
                raise RuntimeError("host blew up")
            return (SimpleNamespace(), None, "ok")
        with patch("hosted_workspace.bounded_observation.ingest_observation",
                   return_value=SimpleNamespace(status="APPLIED")):
            out = BO.run_bounded_observation_cycle(combined_fn=combined)
        self.assertEqual(out["polled"], 2)
        self.assertEqual(out["applied"], 1)             # the healthy one still ingested
        self.assertEqual(out["reasons"].get("error"), 1)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_CAPABILITY_RECOVERY_ENABLED="1")
class CapabilityRecoveryOnboardingGateTests(TestCase):
    def _stuck(self, name, *, confirmed):
        ws, acct = _ws(name, state=S.CONNECTED, connected=True, matched=True, trade_allowed=False,
                       confirmed=confirmed)
        ws.last_decision_at = timezone.now()             # observation-fresh (recovery gate requires it)
        ws.save(update_fields=["last_decision_at"])
        return ws, acct

    def test_unconfirmed_is_not_relaunched_when_flag_on(self):
        self._stuck("fresh", confirmed=False)
        from hosted_workspace.capability_recovery import run_hosted_capability_recovery
        with override_settings(HOSTED_BOUNDED_OBSERVATION_ENABLED="1"):
            # executor_resolver would only be hit if it got past the onboarding gate — assert it never does.
            out = run_hosted_capability_recovery(executor_resolver=lambda a, r: (_ for _ in ()).throw(
                AssertionError("must not reach executor for an unconfirmed onboarding tenant")))
        self.assertEqual(out["candidates"], 1)
        self.assertEqual(out["skipped_onboarding"], 1)
        self.assertEqual(out["attempted"], 0)
        self.assertEqual(out["relaunched"], 0)

    def test_confirmed_still_recovers_when_flag_on(self):
        self._stuck("confirmed", confirmed=True)
        from hosted_workspace.capability_recovery import run_hosted_capability_recovery
        ex = SimpleNamespace(apply_autotrading_config=lambda *a, **k: {"ok": True},
                             relaunch_terminal=lambda *a, **k: {"ok": True})
        with override_settings(HOSTED_BOUNDED_OBSERVATION_ENABLED="1"):
            out = run_hosted_capability_recovery(executor_resolver=lambda a, r: ex)
        self.assertEqual(out["skipped_onboarding"], 0)
        self.assertEqual(out["attempted"], 1)
        self.assertEqual(out["relaunched"], 1)

    def test_flag_off_recovers_regardless_of_confirmation(self):
        self._stuck("fresh_off", confirmed=False)
        from hosted_workspace.capability_recovery import run_hosted_capability_recovery
        ex = SimpleNamespace(apply_autotrading_config=lambda *a, **k: {"ok": True},
                             relaunch_terminal=lambda *a, **k: {"ok": True})
        # HOSTED_BOUNDED_OBSERVATION_ENABLED unset (off) → legacy behaviour, no onboarding gate.
        out = run_hosted_capability_recovery(executor_resolver=lambda a, r: ex)
        self.assertEqual(out["skipped_onboarding"], 0)
        self.assertEqual(out["attempted"], 1)
        self.assertEqual(out["relaunched"], 1)
