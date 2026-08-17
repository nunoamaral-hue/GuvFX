"""ADR-0044 Decision 2 — autonomous hosted-execution arming driver (``auto_arm_runner``).

Proves: DARK unless master + execution flags on; arms an EXECUTION_READY-but-unarmed workspace by calling the
certified arm action (which re-proves every precondition); refuses (leaves unarmed) when a precondition fails;
idempotent (an already-armed / non-EXECUTION_READY workspace is not a candidate). Never places an order.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import readiness as R
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace.auto_arm_runner import run_hosted_auto_arm
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return f"81{_n:04d}"


def _account(*, is_demo=True, with_node=True):
    login = _uniq()
    user = U.objects.create_user(username=f"aa{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    node = TerminalNode.objects.create(hostname=f"n-{login}", status=TerminalNode.Status.ACTIVE) if with_node else None
    return TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                         is_demo=is_demo, is_active=True, broker_server=srv,
                                         readiness_provider=R.PERSISTENT_WORKSPACE, terminal_node=node)


def _ready_ws(acct, *, bind_node=True, **kw):
    base = dict(canonical_state=S.EXECUTION_READY, proj_connected=True, proj_trade_allowed=True,
                proj_account_match=True, proj_execution_ready=True, last_decision_at=timezone.now(),
                workspace_confirmed_at=timezone.now(),
                execution_authorized_at=timezone.now())  # ADR-0047: a ready+armable ws is customer-authorized
    if bind_node and getattr(acct, "terminal_node_id", None):
        base["execution_node"] = acct.terminal_node
    base.update(kw)
    # workspace_confirmed_at lives on the account (the human ACK), not the workspace.
    acct.workspace_confirmed_at = base.pop("workspace_confirmed_at")
    acct.save(update_fields=["workspace_confirmed_at"])
    return HostedMt5Workspace.objects.create(trading_account=acct, **base)


class AutoArmDarkTests(TestCase):
    def test_dark_when_flags_off(self):
        acct = _account()
        _ready_ws(acct)
        out = run_hosted_auto_arm()
        self.assertFalse(out["enabled"])
        acct.hosted_workspace.refresh_from_db()
        self.assertIs(acct.hosted_workspace.execution_enabled, False)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="0")
    def test_dark_when_only_master_on(self):
        acct = _account()
        _ready_ws(acct)
        self.assertFalse(run_hosted_auto_arm()["enabled"])


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class AutoArmActiveTests(TestCase):
    def test_arms_execution_ready_unarmed_workspace(self):
        acct = _account()
        ws = _ready_ws(acct)
        self.assertIs(ws.execution_enabled, False)
        out = run_hosted_auto_arm()
        self.assertTrue(out["enabled"])
        self.assertEqual(out["armed"], 1)
        self.assertEqual(out["refused"], 0)
        ws.refresh_from_db()
        self.assertIs(ws.execution_enabled, True)

    def test_refuses_and_leaves_unarmed_when_precondition_fails(self):
        # A workspace stamped EXECUTION_READY but with the account/node route missing -> arm refuses.
        acct = _account(with_node=False)     # NULL route
        ws = _ready_ws(acct, bind_node=False)
        out = run_hosted_auto_arm()
        self.assertEqual(out["armed"], 0)
        self.assertEqual(out["refused"], 1)
        ws.refresh_from_db()
        self.assertIs(ws.execution_enabled, False)   # unchanged on refusal

    def test_real_account_never_auto_armed(self):
        acct = _account(is_demo=False)
        ws = _ready_ws(acct)
        run_hosted_auto_arm()
        ws.refresh_from_db()
        self.assertIs(ws.execution_enabled, False)   # demo-only wall holds through the auto path

    def test_already_armed_is_not_a_candidate(self):
        acct = _account()
        _ready_ws(acct, execution_enabled=True)
        out = run_hosted_auto_arm()
        self.assertEqual(out["candidates"], 0)       # filtered by execution_enabled=False
        self.assertEqual(out["armed"], 0)

    def test_non_execution_ready_is_not_a_candidate(self):
        acct = _account()
        _ready_ws(acct, canonical_state=S.CONNECTED)
        out = run_hosted_auto_arm()
        self.assertEqual(out["candidates"], 0)

    def test_idempotent_second_pass_no_new_arm(self):
        acct = _account()
        ws = _ready_ws(acct)
        self.assertEqual(run_hosted_auto_arm()["armed"], 1)
        second = run_hosted_auto_arm()
        self.assertEqual(second["candidates"], 0)    # now armed -> no longer a candidate
        self.assertEqual(second["armed"], 0)
        ws.refresh_from_db()
        self.assertIs(ws.execution_enabled, True)

    def test_operator_disarm_survives_next_auto_arm_cycle(self):
        # ADR-0044 finding I5 (reversibility): a deliberate disarm must NOT be silently reverted by auto-arm.
        from execution.hosted_provisioning import disarm_hosted_workspace_execution
        acct = _account()
        ws = _ready_ws(acct)
        self.assertEqual(run_hosted_auto_arm()["armed"], 1)          # auto-armed once
        acct = TradingAccount.objects.get(pk=acct.pk)                # fresh load (as an endpoint would)
        disarm_hosted_workspace_execution(acct, actor="op")          # operator safety-stop
        ws.refresh_from_db()
        self.assertIs(ws.execution_enabled, False)
        self.assertIs(ws.auto_arm_suppressed, True)                  # durable operator intent
        out = run_hosted_auto_arm()                                  # next cron cycle
        self.assertEqual(out["candidates"], 0)                       # suppressed -> not a candidate
        ws.refresh_from_db()
        self.assertIs(ws.execution_enabled, False)                   # stayed disarmed

    def test_explicit_rearm_clears_suppression(self):
        from execution.hosted_provisioning import arm_hosted_workspace_execution
        acct = _account()
        ws = _ready_ws(acct, execution_enabled=False, auto_arm_suppressed=True)
        r = arm_hosted_workspace_execution(acct, actor="op")         # EXPLICIT re-arm
        self.assertTrue(r.ok, r.reason_code)
        ws.refresh_from_db()
        self.assertIs(ws.execution_enabled, True)
        self.assertIs(ws.auto_arm_suppressed, False)                 # cleared, auto-arm may resume later
