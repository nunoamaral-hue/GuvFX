"""AJ#6.3 Shape-3 — hosted_workspace.capability_recovery tests.

Proves: DARK unless master + recovery flags on; a CONNECTED + matched + trade_allowed=False workspace triggers
re-assert-config + graceful relaunch of the tenant's OWN terminal; bounded + loop-safe (cooldown + max
attempts, never restart-loops); Customer Zero excluded; non-stuck / stale / non-demo workspaces are not acted
on; a DARK/absent host executor burns no attempt; and it NEVER arms execution or places an order.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import readiness as R
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import capability_recovery as CR
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_n = 0

FLAGS = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_CAPABILITY_RECOVERY_ENABLED="1")


def _uniq():
    global _n
    _n += 1
    return f"95{_n:04d}"


class FakeExecutor:
    """Records host calls; returns configurable ok/fail. No real host contact."""

    def __init__(self, *, config_ok=True, relaunch_ok=True):
        self.calls = []
        self.config_ok = config_ok
        self.relaunch_ok = relaunch_ok

    def apply_autotrading_config(self, runtime_root, rdp_host=None):
        self.calls.append(("apply_autotrading_config", runtime_root, rdp_host))
        return {"ok": self.config_ok}

    def relaunch_terminal(self, username, runtime_root, rdp_host=None):
        self.calls.append(("relaunch_terminal", username, runtime_root, rdp_host))
        return {"ok": self.relaunch_ok}


def _account(*, account_id=None, is_demo=True, rdp_host="100.79.101.19"):
    login = _uniq()
    user = U.objects.create_user(username=f"cr{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    node = TerminalNode.objects.create(hostname=f"n-{login}", status=TerminalNode.Status.ACTIVE,
                                       rdp_host=rdp_host)
    kw = dict(user=user, name="a", broker_name="B", account_number=login, is_demo=is_demo, is_active=True,
              broker_server=srv, readiness_provider=R.PERSISTENT_WORKSPACE, terminal_node=node)
    if account_id is not None:
        kw["id"] = account_id
    return TradingAccount.objects.create(**kw), node


def _stuck_ws(acct, node, **kw):
    """CONNECTED + matched + trade_allowed=False + fresh — the AJ#6.2 stuck state."""
    base = dict(canonical_state=S.CONNECTED, proj_connected=True, proj_account_match=True,
                proj_trade_allowed=False, proj_execution_ready=False, last_decision_at=timezone.now(),
                execution_node=node)
    base.update(kw)
    return HostedMt5Workspace.objects.create(trading_account=acct, **base)


def _resolver(ex):
    return lambda account_id, rdp_host: ex


class DarkTests(TestCase):
    def test_dark_when_flags_off(self):
        acct, node = _account()
        _stuck_ws(acct, node)
        ex = FakeExecutor()
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        self.assertFalse(out["enabled"])
        self.assertEqual(ex.calls, [])

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_CAPABILITY_RECOVERY_ENABLED="0")
    def test_dark_when_only_master_on(self):
        acct, node = _account()
        _stuck_ws(acct, node)
        ex = FakeExecutor()
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        self.assertFalse(out["enabled"])
        self.assertEqual(ex.calls, [])


@override_settings(**FLAGS)
class RecoveryTests(TestCase):
    def test_stuck_workspace_reasserts_config_then_relaunches(self):
        acct, node = _account()
        ws = _stuck_ws(acct, node)
        ex = FakeExecutor()
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        self.assertEqual(out["candidates"], 1, out)
        self.assertEqual(out["attempted"], 1, out)
        self.assertEqual(out["config_reasserted"], 1, out)
        self.assertEqual(out["relaunched"], 1, out)
        # order matters: config re-assert BEFORE relaunch
        self.assertEqual([c[0] for c in ex.calls], ["apply_autotrading_config", "relaunch_terminal"])
        ws.refresh_from_db()
        self.assertEqual(ws.capability_recovery_count, 1)
        self.assertIsNotNone(ws.capability_recovery_at)

    def test_never_arms_execution(self):
        acct, node = _account()
        ws = _stuck_ws(acct, node)
        CR.run_hosted_capability_recovery(executor_resolver=_resolver(FakeExecutor()))
        ws.refresh_from_db()
        self.assertFalse(ws.execution_enabled)                 # capability recovery NEVER arms
        self.assertIsNone(ws.execution_authorized_at)          # ...and never authorizes

    def test_second_immediate_pass_is_cooldown_skipped(self):
        acct, node = _account()
        ex = FakeExecutor()
        _stuck_ws(acct, node)
        CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        ex2 = FakeExecutor()
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex2))
        self.assertEqual(out["skipped_cooldown"], 1, out)
        self.assertEqual(out["attempted"], 0, out)
        self.assertEqual(ex2.calls, [])                        # MT5 not restarted again within cooldown

    def test_bounded_by_max_attempts(self):
        acct, node = _account()
        # Pre-seed a workspace already at the attempt cap, last attempt long ago (cooldown elapsed).
        old = timezone.now() - timezone.timedelta(seconds=CR.RECOVERY_COOLDOWN_S + 10)
        _stuck_ws(acct, node, capability_recovery_count=CR.MAX_RECOVERY_ATTEMPTS, capability_recovery_at=old)
        ex = FakeExecutor()
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        self.assertEqual(out["skipped_cooldown"], 1, out)      # cap reached ⇒ never restart-loops
        self.assertEqual(ex.calls, [])

    def test_customer_zero_excluded(self):
        acct, node = _account(account_id=1)                    # Customer Zero
        _stuck_ws(acct, node)
        ex = FakeExecutor()
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        self.assertEqual(out["candidates"], 0, out)            # never a candidate
        self.assertEqual(ex.calls, [])

    def test_execution_ready_is_not_a_candidate(self):
        acct, node = _account()
        _stuck_ws(acct, node, canonical_state=S.EXECUTION_READY, proj_trade_allowed=True,
                  proj_execution_ready=True)
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(FakeExecutor()))
        self.assertEqual(out["candidates"], 0, out)            # recovered ⇒ not restarted

    def test_unmatched_is_not_a_candidate(self):
        acct, node = _account()
        _stuck_ws(acct, node, proj_account_match=False)
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(FakeExecutor()))
        self.assertEqual(out["candidates"], 0, out)

    def test_stale_observation_not_acted_on(self):
        acct, node = _account()
        _stuck_ws(acct, node, last_decision_at=timezone.now() - timezone.timedelta(hours=1))
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(FakeExecutor()))
        self.assertEqual(out["skipped_not_ready"], 1, out)

    def test_non_demo_not_acted_on(self):
        acct, node = _account(is_demo=False)
        _stuck_ws(acct, node)
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(FakeExecutor()))
        self.assertEqual(out["skipped_not_ready"], 1, out)

    def test_dark_executor_burns_no_attempt(self):
        acct, node = _account()
        ws = _stuck_ws(acct, node)
        out = CR.run_hosted_capability_recovery(executor_resolver=lambda a, r: None)  # DARK executor
        self.assertEqual(out["skipped_no_executor"], 1, out)
        self.assertEqual(out["attempted"], 0, out)
        ws.refresh_from_db()
        self.assertEqual(ws.capability_recovery_count, 0)      # no attempt burned when no host is contacted

    def test_config_failure_does_not_relaunch_but_claims_attempt(self):
        acct, node = _account()
        ws = _stuck_ws(acct, node)
        ex = FakeExecutor(config_ok=False)
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        self.assertEqual(out["config_reasserted"], 0, out)
        self.assertEqual(out["relaunched"], 0, out)
        self.assertEqual(out["errors"], 1, out)
        self.assertEqual([c[0] for c in ex.calls], ["apply_autotrading_config"])   # NO relaunch on config fail
        ws.refresh_from_db()
        self.assertEqual(ws.capability_recovery_count, 1)      # attempt still claimed (loop-safety backs off)
