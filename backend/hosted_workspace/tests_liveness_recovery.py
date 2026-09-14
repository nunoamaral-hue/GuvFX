"""P0 (2026-09-14) — hosted_workspace.liveness_recovery tests.

Proves: DARK unless master + liveness flags on; an ARMED (execution_enabled + authorized), confirmed,
matched, demo workspace whose read-only observe reports NO running terminal is relaunched via the certified
RELAUNCH_TERMINAL primitive; a FRESH workspace is never observed/relaunched; an ambiguous/unavailable observe
never relaunches (no duplicate); bounded + loop-safe (cooldown + max attempts); Customer Zero + unconfirmed +
unarmed + non-demo are never candidates; a DARK executor burns no attempt; it NEVER arms or places an order;
and an OPEN operator alert is raised on a down terminal and resolved on recovery.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import readiness as R
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import liveness_recovery as LR
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_n = 0

FLAGS = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_LIVENESS_RECOVERY_ENABLED="1")
# Flags needed for readiness.evaluate().eligible to be True (the recovery-resolution path).
ELIGIBLE_FLAGS = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_LIVENESS_RECOVERY_ENABLED="1",
                      HOSTED_MT5_EXECUTION_ENABLED="1", HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="1")


def _uniq():
    global _n
    _n += 1
    return f"96{_n:04d}"


class FakeExecutor:
    """Records host calls; returns configurable ok/fail. No real host contact."""

    def __init__(self, *, relaunch_ok=True):
        self.calls = []
        self.relaunch_ok = relaunch_ok

    def relaunch_terminal(self, username, runtime_root, rdp_host=None):
        self.calls.append(("relaunch_terminal", username, runtime_root, rdp_host))
        return {"ok": self.relaunch_ok}


def _account(*, account_id=None, is_demo=True, confirmed=True, rdp_host="100.79.101.19"):
    login = _uniq()
    user = U.objects.create_user(username=f"lr{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    node = TerminalNode.objects.create(hostname=f"n-{login}", status=TerminalNode.Status.ACTIVE, rdp_host=rdp_host)
    kw = dict(user=user, name="a", broker_name="B", account_number=login, is_demo=is_demo, is_active=True,
              broker_server=srv, readiness_provider=R.PERSISTENT_WORKSPACE, terminal_node=node,
              workspace_confirmed_at=(timezone.now() if confirmed else None))
    if account_id is not None:
        kw["id"] = account_id
    return TradingAccount.objects.create(**kw), node


def _armed_ws(acct, node, *, armed=True, matched=True, stale=True, canonical=S.EXECUTION_READY, **kw):
    """An ARMED (execution_enabled + authorized), matched workspace. ``stale`` picks last_decision_at older than
    the freshness window (the disarmed state) vs now (healthy)."""
    ts = (timezone.now() - timezone.timedelta(seconds=R.WORKSPACE_OBSERVATION_FRESH_SECONDS + 60)
          if stale else timezone.now())
    base = dict(canonical_state=canonical, proj_connected=True, proj_account_match=matched,
                proj_trade_allowed=True, last_decision_at=ts, execution_node=node,
                execution_enabled=armed,
                execution_authorized_at=(timezone.now() if armed else None))
    base.update(kw)
    return HostedMt5Workspace.objects.create(trading_account=acct, **base)


def _resolver(ex):
    return lambda account_id, rdp_host: ex


def _down(_ws):
    return {"ok": False, "process_running": False, "reason": "terminal_not_running"}


def _up(_ws):
    return {"ok": True, "process_running": True, "terminal_connected": True, "trade_allowed": True}


def _ambiguous(_ws):
    return None


class DarkTests(TestCase):
    def test_dark_when_flags_off(self):
        acct, node = _account()
        _armed_ws(acct, node)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertFalse(out["enabled"])
        self.assertEqual(ex.calls, [])

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_LIVENESS_RECOVERY_ENABLED="0")
    def test_dark_when_only_master_on(self):
        acct, node = _account()
        _armed_ws(acct, node)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertFalse(out["enabled"])
        self.assertEqual(ex.calls, [])


@override_settings(**FLAGS)
class RecoveryTests(TestCase):
    def test_down_armed_workspace_is_relaunched(self):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["candidates"], 1, out)
        self.assertEqual(out["down"], 1, out)
        self.assertEqual(out["relaunched"], 1, out)
        self.assertEqual([c[0] for c in ex.calls], ["relaunch_terminal"])
        # relaunch is server-derived on the slot identity (guvfx_u_<id> + runtime root)
        self.assertEqual(ex.calls[0][1], f"guvfx_u_{acct.id}")
        ws.refresh_from_db()
        self.assertEqual(ws.liveness_recovery_count, 1)
        self.assertIsNotNone(ws.liveness_recovery_at)

    def test_never_arms_or_changes_authorization(self):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        before_auth = ws.execution_authorized_at
        LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_down)
        ws.refresh_from_db()
        # execution_enabled was already True; the runner must not toggle arming state or re-authorize.
        self.assertTrue(ws.execution_enabled)
        self.assertEqual(ws.execution_authorized_at, before_auth)

    def test_opens_operator_alert_on_down(self):
        from reliability.models import AlertEvent
        acct, node = _account()
        _armed_ws(acct, node)
        LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_down)
        alert = AlertEvent.objects.filter(dedup_key=LR._alert_dedup_key(acct.id),
                                          status=AlertEvent.Status.OPEN).first()
        self.assertIsNotNone(alert)
        self.assertEqual(alert.severity, AlertEvent.Severity.CRITICAL)

    def test_fresh_workspace_is_skipped_healthy(self):
        acct, node = _account()
        _armed_ws(acct, node, stale=False)
        ex = FakeExecutor()
        called = {"n": 0}

        def _obs(ws):
            called["n"] += 1
            return _down(ws)
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_obs)
        self.assertEqual(out["skipped_healthy"], 1, out)
        self.assertEqual(out["candidates"], 0, out)     # fresh ⇒ not even observed
        self.assertEqual(called["n"], 0)                 # a healthy workspace is never observed by this pass
        self.assertEqual(ex.calls, [])

    def test_process_up_is_not_relaunched(self):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_up)
        self.assertEqual(out["down"], 0, out)
        self.assertEqual(out["relaunched"], 0, out)
        self.assertEqual(ex.calls, [])                   # up-but-not-connected is capability recovery's job
        ws.refresh_from_db()
        self.assertEqual(ws.liveness_recovery_count, 0)

    def test_ambiguous_observe_is_not_relaunched(self):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_ambiguous)
        self.assertEqual(out["skipped_ambiguous"], 1, out)
        self.assertEqual(out["relaunched"], 0, out)
        self.assertEqual(ex.calls, [])                   # never relaunch a possibly-running terminal (no duplicate)
        ws.refresh_from_db()
        self.assertEqual(ws.liveness_recovery_count, 0)

    def test_second_immediate_pass_is_cooldown_skipped(self):
        acct, node = _account()
        _armed_ws(acct, node)
        LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_down)
        ex2 = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex2), observe_fn=_down)
        self.assertEqual(out["skipped_cooldown"], 1, out)
        self.assertEqual(out["relaunched"], 0, out)
        self.assertEqual(ex2.calls, [])                  # MT5 not restarted again within cooldown

    def test_bounded_by_max_attempts(self):
        acct, node = _account()
        old = timezone.now() - timezone.timedelta(seconds=LR.RECOVERY_COOLDOWN_S + 10)
        _armed_ws(acct, node, liveness_recovery_count=LR.MAX_RECOVERY_ATTEMPTS, liveness_recovery_at=old)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["relaunched"], 0, out)      # cap reached ⇒ never restart-loops
        self.assertEqual(ex.calls, [])

    def test_customer_zero_excluded(self):
        acct, node = _account(account_id=1)
        _armed_ws(acct, node)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["candidates"], 0, out)
        self.assertEqual(ex.calls, [])

    def test_unconfirmed_onboarding_not_a_candidate(self):
        acct, node = _account(confirmed=False)
        _armed_ws(acct, node)
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_down)
        self.assertEqual(out["candidates"], 0, out)      # fresh/unconfirmed tenant never relaunched

    def test_unarmed_workspace_not_a_candidate(self):
        acct, node = _account()
        _armed_ws(acct, node, armed=False)
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_down)
        self.assertEqual(out["candidates"], 0, out)      # not authorized ⇒ not a candidate

    def test_non_demo_not_a_candidate(self):
        acct, node = _account(is_demo=False)
        _armed_ws(acct, node)
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_down)
        self.assertEqual(out["candidates"], 0, out)

    def test_dark_executor_burns_no_attempt(self):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        out = LR.run_hosted_liveness_recovery(executor_resolver=lambda a, r: None, observe_fn=_down)
        self.assertEqual(out["skipped_no_executor"], 1, out)
        self.assertEqual(out["relaunched"], 0, out)
        ws.refresh_from_db()
        self.assertEqual(ws.liveness_recovery_count, 0)

    def test_relaunch_failure_still_claims_attempt(self):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        ex = FakeExecutor(relaunch_ok=False)
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["relaunched"], 0, out)
        self.assertEqual(out["errors"], 1, out)
        ws.refresh_from_db()
        self.assertEqual(ws.liveness_recovery_count, 1)  # attempt claimed ⇒ loop-safety backs off


@override_settings(**ELIGIBLE_FLAGS)
class RecoveredAlertTests(TestCase):
    def test_open_alert_resolved_when_workspace_healthy_again(self):
        from reliability.models import AlertEvent
        acct, node = _account()
        ws = _armed_ws(acct, node, stale=False)          # healthy + fresh + eligible
        # Sanity: the workspace really is eligible now.
        self.assertTrue(R.PersistentWorkspaceProvider().evaluate(acct).eligible)
        AlertEvent.objects.create(severity=AlertEvent.Severity.CRITICAL, component="MT5_TERMINAL",
                                  trading_account_id=acct.id, title="down", body="down",
                                  dedup_key=LR._alert_dedup_key(acct.id), status=AlertEvent.Status.OPEN)
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_up)
        self.assertEqual(out["recovered"], 1, out)
        self.assertFalse(AlertEvent.objects.filter(dedup_key=LR._alert_dedup_key(acct.id),
                                                   status=AlertEvent.Status.OPEN).exists())
