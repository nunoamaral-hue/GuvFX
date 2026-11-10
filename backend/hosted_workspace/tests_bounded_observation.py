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

    # ── ADR-0090 observation-concurrency budget (reserve control-plane headroom on the host executor) ──────────
    def test_default_budget_reserves_control_plane_headroom(self):
        b = BO.observation_budget()
        self.assertEqual(b["effective"], 4)                              # conservative default, below the cap of 8
        self.assertEqual((b["capacity"], b["reserved"]), (8, 2))
        self.assertLessEqual(b["effective"] + b["reserved"], b["capacity"])   # >= `reserved` permits stay free
        self.assertFalse(b["notes"])
        self.assertLessEqual(BO._max_workers(), BO._HARD_MAX_WORKERS)

    def test_budget_clamps_to_capacity_minus_reserved(self):
        with override_settings(HOSTED_OBSERVATION_MAX_WORKERS="7"):      # 7 < capacity(8) but > max_safe(6)
            b = BO.observation_budget()
            self.assertEqual(b["effective"], 6)
            self.assertIn("observation_concurrency_exceeds_headroom", b["notes"])

    def test_budget_fails_closed_on_invalid_concurrency(self):
        # Zero / negative / >= capacity must never saturate the executor: effective stays in [1, capacity-reserved].
        for bad in ("0", "-3", "8", "999"):
            with override_settings(HOSTED_OBSERVATION_MAX_WORKERS=bad):
                b = BO.observation_budget()
                self.assertGreaterEqual(b["effective"], 1)
                self.assertLessEqual(b["effective"], b["capacity"] - b["reserved"])
                self.assertIn("observation_concurrency_invalid", b["notes"])

    def test_budget_fails_closed_when_reserved_leaves_no_headroom(self):
        with override_settings(HOSTED_EXECUTOR_CONNECTION_CAPACITY="3", HOSTED_CONTROL_PLANE_RESERVED_SLOTS="5"):
            b = BO.observation_budget()
            self.assertIn("reserved_ge_capacity", b["notes"])
            self.assertGreaterEqual(b["effective"], 1)
            self.assertLessEqual(b["effective"] + b["reserved"], b["capacity"])   # headroom still preserved

    def test_budget_tracks_a_raised_host_capacity_up_to_node_ceiling(self):
        # If an operator raises the daemon cap, the backend mirror must be raised to match for observation to scale.
        with override_settings(HOSTED_EXECUTOR_CONNECTION_CAPACITY="24", HOSTED_OBSERVATION_MAX_WORKERS="10"):
            self.assertEqual(BO.observation_budget()["effective"], 10)   # scales with a raised, matched capacity
        with override_settings(HOSTED_EXECUTOR_CONNECTION_CAPACITY="24", HOSTED_OBSERVATION_MAX_WORKERS="16"):
            # …but the Node-2 host-capacity ceiling (_HARD_MAX_WORKERS) still caps the fan-out regardless.
            self.assertEqual(BO.observation_budget()["effective"], BO._HARD_MAX_WORKERS)

    def test_pool_concurrency_never_exceeds_effective_and_defers_truthfully(self):
        import threading
        for i in range(28):
            _ws(f"w{i}", login=str(700100 + i))
        lock = threading.Lock(); state = {"cur": 0, "peak": 0}
        def combined(ws):
            with lock:
                state["cur"] += 1; state["peak"] = max(state["peak"], state["cur"])
            time.sleep(0.02)
            with lock:
                state["cur"] -= 1
            return (SimpleNamespace(), None, "ok")
        with patch("hosted_workspace.bounded_observation.ingest_observation",
                   return_value=SimpleNamespace(status="APPLIED")):
            out = BO.run_bounded_observation_cycle(combined_fn=combined)
        self.assertEqual(out["polled"], 28)                              # every workspace observed — queued, not lost
        self.assertEqual(out["workers"], 4)
        self.assertLessEqual(state["peak"], 4)                           # peak in-flight never exceeded the budget
        self.assertEqual(out["deferred"], 24)                            # 28 - 4 queued behind the budget at peak
        self.assertEqual(out["deferred_reason"], "host_observation_capacity_deferred")
        self.assertEqual(out["budget"]["reserved"], 2)                   # control-plane headroom reserved throughout

    def test_typed_reasons_preserved_not_flattened(self):
        _ws("t1"); _ws("t2"); _ws("t3")
        reasons = iter(["observation_timeout", "terminal_not_running", "duplicate_terminal"])
        combined = lambda ws: (None, None, next(reasons))
        with patch("hosted_workspace.bounded_observation.ingest_observation"):
            out = BO.run_bounded_observation_cycle(combined_fn=combined)
        self.assertEqual(out["unavailable"], 3)
        self.assertEqual(sum(out["reasons"].values()), 3)
        self.assertEqual(set(out["reasons"]), {"observation_timeout", "terminal_not_running", "duplicate_terminal"})

    def test_frozen_onboarding_observation_is_surfaced_distinctly(self):
        # A present-but-unattachable terminal on an onboarding tenant (duplicate_terminal) is a STUCK state, not
        # routine unavailability - it must be counted as `frozen` and logged, so operators/alerting see it instead of
        # it hiding in the generic `unavailable` bucket (the account-37 forensic). Only reachable reasons are used
        # here: duplicate_terminal is what the live pipeline actually surfaces; observation_timeout is routine.
        _ws("dup", login="700010"); _ws("gone", login="700011"); _ws("routine", login="700012")
        reasons = iter(["duplicate_terminal", "duplicate_terminal", "observation_timeout"])
        combined = lambda ws: (None, None, next(reasons))
        with patch("hosted_workspace.bounded_observation.ingest_observation"):
            with self.assertLogs("guvfx.hosted_workspace", level="WARNING") as cm:
                out = BO.run_bounded_observation_cycle(combined_fn=combined)
        self.assertEqual(out["unavailable"], 3)
        self.assertEqual(out["frozen"], 2)              # the two duplicate_terminal onboarding tenants; timeout is not
        self.assertTrue(any("hosted_observation_frozen" in line for line in cm.output))

    def test_frozen_not_counted_for_non_onboarding_state(self):
        # The same blocking reason on a CONNECTED (post-onboarding) tenant is NOT a frozen-onboarding event.
        _ws("connected_dup", login="700013", state=S.CONNECTED, connected=True, matched=True)
        combined = lambda ws: (None, None, "duplicate_terminal")
        with patch("hosted_workspace.bounded_observation.ingest_observation"):
            out = BO.run_bounded_observation_cycle(combined_fn=combined)
        self.assertEqual(out["unavailable"], 1)
        self.assertEqual(out["frozen"], 0)

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
