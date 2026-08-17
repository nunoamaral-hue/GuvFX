"""ADR-0044 E1 — ``_account_execution_ready`` (the self-serve arm readiness gate) is READINESS-PROVIDER aware.

A hosted (Provider B) account has NO legacy ``AccountRuntime``; before this fix it fail-closed on
``runtime_not_ready`` and the ONLY self-serve arm path could never work for a hosted account. It now delegates
to the certified persistent-workspace gate for Provider B, while Provider A (every existing account) is
byte-unchanged.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import readiness as R
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from strategies.views import _account_execution_ready

U = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return f"72{_n:04d}"


def _account(*, provider=R.PERSISTENT_WORKSPACE, is_demo=True, with_node=True):
    login = _uniq()
    user = U.objects.create_user(username=f"ha{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    node = TerminalNode.objects.create(hostname=f"n-{login}", status=TerminalNode.Status.ACTIVE) if with_node else None
    return TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                         is_demo=is_demo, is_active=True, broker_server=srv,
                                         readiness_provider=provider, terminal_node=node)


def _ready_ws(acct, **kw):
    base = dict(canonical_state=S.EXECUTION_READY, proj_connected=True, proj_trade_allowed=True,
                proj_account_match=True, proj_execution_ready=True, last_decision_at=timezone.now(),
                execution_authorized_at=timezone.now())  # ADR-0047: a ready workspace is customer-authorized
    if getattr(acct, "terminal_node_id", None):
        base["execution_node"] = acct.terminal_node
    base.update(kw)
    acct.workspace_confirmed_at = timezone.now()
    acct.save(update_fields=["workspace_confirmed_at"])
    return HostedMt5Workspace.objects.create(trading_account=acct, **base)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ProviderBReadinessTests(TestCase):
    def test_hosted_armed_execution_ready_is_ready(self):
        acct = _account()
        _ready_ws(acct, execution_enabled=True)   # arm precondition for Provider B eligibility
        self.assertEqual(_account_execution_ready(acct), (True, "ready"))

    def test_hosted_unarmed_is_not_ready(self):
        acct = _account()
        _ready_ws(acct, execution_enabled=False)
        ready, why = _account_execution_ready(acct)
        self.assertFalse(ready)
        self.assertEqual(why, R.RW_EXECUTION_DISABLED)

    def test_hosted_without_workspace_is_not_ready(self):
        acct = _account()
        ready, why = _account_execution_ready(acct)
        self.assertFalse(ready)
        self.assertEqual(why, R.RW_WORKSPACE_MISSING)

    def test_hosted_does_not_report_legacy_runtime_reason(self):
        # The core defect: a hosted account must NEVER be denied with the legacy 'runtime_not_ready' code.
        acct = _account()
        _ready_ws(acct, execution_enabled=False)
        _, why = _account_execution_ready(acct)
        self.assertNotEqual(why, "runtime_not_ready")


class ProviderARegressionTests(TestCase):
    def test_provider_a_without_runtime_still_runtime_not_ready(self):
        acct = _account(provider=R.TEMPORARY_VALIDATION, with_node=False)
        self.assertEqual(_account_execution_ready(acct), (False, "runtime_not_ready"))
