"""ADR-0034 Execution Engine (G5) — provision vs arm. Provisioning never arms; arming is explicit + fully
preconditioned + audited; disarming is immediate. No auto-arm from any lifecycle event."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from trading.models import BrokerServer, TradingAccount

from execution import hosted_provisioning as P
from execution.models import TerminalNode
from execution import readiness as R
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()


def _account(*, is_demo=True, with_node=True, provider=R.PERSISTENT_WORKSPACE, login="700900"):
    user = U.objects.create_user(username=f"p{login}{is_demo}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    node = TerminalNode.objects.create(hostname=f"node-{login}") if with_node else None
    return TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                         is_demo=is_demo, broker_server=srv, readiness_provider=provider,
                                         terminal_node=node)


def _ready_ws(acct, **kw):
    base = dict(canonical_state=S.EXECUTION_READY, proj_connected=True, proj_trade_allowed=True,
                proj_account_match=True, proj_execution_ready=True, last_decision_at=timezone.now())
    base.update(kw)
    return HostedMt5Workspace.objects.create(trading_account=acct, **base)


class ProvisionTests(TestCase):
    def test_provision_never_arms(self):
        acct = _account(provider=R.TEMPORARY_VALIDATION)
        ws = P.provision_hosted_workspace(acct, attach_path="C:/disp/terminal64.exe")
        self.assertIs(ws.execution_enabled, False)                 # provisioned, NOT armed
        acct.refresh_from_db()
        self.assertEqual(acct.readiness_provider, R.PERSISTENT_WORKSPACE)
        self.assertEqual(ws.attach_path, "C:/disp/terminal64.exe")


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ArmTests(TestCase):
    def test_healthy_ready_workspace_is_not_armed_until_explicit(self):
        acct = _account()
        _ready_ws(acct)  # fully execution-ready, but NOT armed
        acct.refresh_from_db()
        self.assertIs(acct.hosted_workspace.execution_enabled, False)  # no auto-arm from readiness

    def test_explicit_arm_succeeds_and_sets_flag(self):
        acct = _account()
        _ready_ws(acct)
        acct.refresh_from_db()
        r = P.arm_hosted_workspace_execution(acct, actor="admin")
        self.assertTrue(r.ok, r.reason_code)
        acct.hosted_workspace.refresh_from_db()
        self.assertIs(acct.hosted_workspace.execution_enabled, True)

    def test_arm_refuses_real_account(self):
        acct = _account(is_demo=False)
        _ready_ws(acct)
        acct.refresh_from_db()
        r = P.arm_hosted_workspace_execution(acct)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, R.RW_REAL_ACCOUNT_NOT_ENABLED)
        self.assertIs(acct.hosted_workspace.execution_enabled, False)  # unchanged on refusal

    def test_arm_refuses_stale_observation(self):
        acct = _account()
        _ready_ws(acct, last_decision_at=timezone.now() - timezone.timedelta(
            seconds=R.WORKSPACE_OBSERVATION_FRESH_SECONDS + 60))
        acct.refresh_from_db()
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, R.RW_OBSERVATION_STALE)

    def test_arm_refuses_missing_route(self):
        acct = _account(with_node=False)  # NULL node
        _ready_ws(acct)
        acct.refresh_from_db()
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, P.ARM_ROUTE_MISSING)

    def test_arm_refuses_mismatch(self):
        acct = _account()
        _ready_ws(acct, proj_account_match=False)
        acct.refresh_from_db()
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, R.RW_ACTIVE_ACCOUNT_MISMATCH)

    def test_disarm_is_immediate(self):
        acct = _account()
        _ready_ws(acct, execution_enabled=True)
        acct.refresh_from_db()
        r = P.disarm_hosted_workspace_execution(acct, actor="admin")
        self.assertTrue(r.ok)
        acct.hosted_workspace.refresh_from_db()
        self.assertIs(acct.hosted_workspace.execution_enabled, False)

    def test_arm_refuses_when_execution_flag_off(self):
        acct = _account()
        _ready_ws(acct)
        acct.refresh_from_db()
        with override_settings(HOSTED_MT5_EXECUTION_ENABLED="0"):
            self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code,
                             R.RW_EXECUTION_FEATURE_DISABLED)
