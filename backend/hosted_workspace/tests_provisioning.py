"""ADR-0034 Onboarding — provisioning orchestrator (request → allocate → confirm).

Proves the repository-side customer journey driver: admission-gated + idempotent workspace request creating
an INTENT-ONLY account that NEVER carries a password; atomic node allocation that advances
PROVISIONING → WAITING_FOR_LOGIN through the certified single writer and keeps the
account.terminal_node == workspace.execution_node agreement; and a confirm step that is owner-scoped,
gated on a POSITIVE observed active-account match, and idempotent. Nothing here arms execution or places an
order.
"""
import inspect

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.models import UserSubscriptionState
from execution.models import TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from trading.models import TradingAccount

from hosted_workspace import entitlement as E
from hosted_workspace import provisioning as P
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_FLAGS_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1")


def _user(name="u1", *, entitled=True):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan=("beta" if entitled else "starter_trial"),
                              plan_status="active", viewer_mode=False))
    return u


def _node(hostname="node-a", **kw):
    # G12: nodes are DELIVERABLE by default (a durable rdp_host); pass rdp_host="" to exercise the
    # fail-closed "node has capacity but no rdp_host" allocation path.
    kw.setdefault("rdp_host", "10.9.9.9")
    return TerminalNode.objects.create(hostname=hostname, status=TerminalNode.Status.ACTIVE, **kw)


class RequestTests(TestCase):
    def test_dark_subsystem_denies_request(self):
        res = P.request_hosted_workspace(_user(), expected_login="700900")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, E.DENY_SUBSYSTEM_DARK)
        self.assertFalse(HostedMt5Workspace.objects.exists())     # nothing created while dark

    @override_settings(**_FLAGS_ON)
    def test_login_required(self):
        res = P.request_hosted_workspace(_user(), expected_login="   ")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_LOGIN_REQUIRED)

    @override_settings(**_FLAGS_ON)
    def test_creates_intent_only_account_no_password(self):
        u = _user()
        res = P.request_hosted_workspace(u, expected_login="700900",
                                         expected_server="IS6-Demo", broker_name="IS6", is_demo=True)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.reason, P.REQ_CREATED)
        self.assertTrue(res.created)
        acct = res.workspace.trading_account
        self.assertEqual(acct.account_number, "700900")
        self.assertFalse(acct.is_active)                          # intent only — never live on creation
        self.assertEqual(acct.readiness_provider, PERSISTENT_WORKSPACE)
        self.assertEqual(acct.password_enc or "", "")             # THE product invariant — no broker password
        self.assertEqual(acct.user_id, u.pk)                      # ownership = trading_account.user (single source)

    @override_settings(**_FLAGS_ON)
    def test_request_is_idempotent(self):
        u = _user()
        first = P.request_hosted_workspace(u, expected_login="700900")
        second = P.request_hosted_workspace(u, expected_login="999999")   # different login, same user
        self.assertTrue(second.ok)
        self.assertEqual(second.reason, P.REQ_EXISTS)
        self.assertFalse(second.created)
        self.assertEqual(first.workspace.pk, second.workspace.pk)
        self.assertEqual(HostedMt5Workspace.objects.filter(trading_account__user=u).count(), 1)   # never a second
        self.assertEqual(TradingAccount.objects.filter(user=u).count(), 1)

    @override_settings(**_FLAGS_ON)
    def test_non_entitled_user_denied(self):
        res = P.request_hosted_workspace(_user(entitled=False), expected_login="700900")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, E.DENY_NOT_ENTITLED)

    def test_no_password_parameter_in_signature(self):
        # Structural product guard: neither entry point may EVER accept a broker password.
        for fn in (P.request_hosted_workspace, P.confirm_broker_account):
            params = set(inspect.signature(fn).parameters)
            self.assertFalse(any("password" in p.lower() or "pwd" in p.lower() for p in params), params)


@override_settings(**_FLAGS_ON)
class AllocateTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.ws = P.request_hosted_workspace(self.user, expected_login="700900").workspace

    def test_allocation_binds_node_and_advances_to_waiting_for_login(self):
        node = _node()
        res = P.allocate_workspace_node(self.ws)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.reason, P.ALLOC_OK)
        self.assertEqual(res.node_hostname, node.hostname)
        ws = HostedMt5Workspace.objects.get(pk=self.ws.pk)
        self.assertEqual(ws.execution_node_id, node.pk)
        self.assertEqual(ws.trading_account.terminal_node_id, node.pk)   # route agreement invariant
        # Two explicit authority assignments to the SAME host (single-host model): execution AND delivery.
        self.assertEqual(ws.workspace_node_id, node.pk)                  # delivery host (ADR-0034 §9)
        self.assertEqual(str(ws.canonical_state), S.WAITING_FOR_LOGIN)   # driven via the single writer
        self.assertEqual(int(ws.observation_version), 1)

    def test_allocation_is_idempotent(self):
        _node()
        P.allocate_workspace_node(self.ws)
        second = P.allocate_workspace_node(self.ws)
        self.assertTrue(second.ok)
        self.assertEqual(second.reason, P.ALLOC_ALREADY)

    def test_retry_converges_stuck_provisioning(self):
        # Simulate a bind that succeeded but whose advance never ran: bind the node directly, leaving canonical
        # at PROVISIONING. A retry must return ALLOC_ALREADY *and* drive it to WAITING_FOR_LOGIN (converge).
        from execution.hosted_provisioning import assign_workspace_execution_node
        node = _node()
        assign_workspace_execution_node(self.ws.trading_account, node)
        ws = HostedMt5Workspace.objects.get(pk=self.ws.pk)
        self.assertEqual(str(ws.canonical_state), S.PROVISIONING)   # bound but not advanced
        res = P.allocate_workspace_node(ws)
        self.assertTrue(res.ok)
        self.assertEqual(res.reason, P.ALLOC_ALREADY)
        self.assertEqual(str(HostedMt5Workspace.objects.get(pk=self.ws.pk).canonical_state),
                         S.WAITING_FOR_LOGIN)

    def test_retry_does_not_regress_connected(self):
        # A workspace already progressed to CONNECTED must NEVER be knocked back toward login by a re-allocate.
        _node()
        P.allocate_workspace_node(self.ws)
        ws = HostedMt5Workspace.objects.get(pk=self.ws.pk)
        ws.canonical_state = S.CONNECTED
        ws.save(update_fields=["canonical_state"])
        P.allocate_workspace_node(ws)                               # retry
        self.assertEqual(str(HostedMt5Workspace.objects.get(pk=self.ws.pk).canonical_state), S.CONNECTED)

    def test_no_capacity_fails_closed(self):
        _node(max_accounts=0)                                   # node present but full
        res = P.allocate_workspace_node(self.ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.ALLOC_NO_CAPACITY)
        ws = HostedMt5Workspace.objects.get(pk=self.ws.pk)
        self.assertIsNone(ws.execution_node_id)                 # not bound
        self.assertEqual(str(ws.canonical_state), S.PROVISIONING)   # not advanced

    def test_capacity_counts_hosted_bindings_not_just_active(self):
        # THE capacity defect: hosted intent accounts are is_active=False and thus invisible to the legacy
        # live-account metric. A node with max_accounts=1 must accept exactly ONE hosted workspace; the 2nd
        # allocate (a different user) must fail closed — proving occupancy counts the bindings themselves.
        _node(max_accounts=1)
        first = P.allocate_workspace_node(self.ws)
        self.assertTrue(first.ok)
        self.assertEqual(first.reason, P.ALLOC_OK)
        ws2 = P.request_hosted_workspace(_user("second"), expected_login="800800").workspace
        second = P.allocate_workspace_node(ws2)
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, P.ALLOC_NO_CAPACITY)     # node full of hosted bindings, not active accts
        self.assertIsNone(HostedMt5Workspace.objects.get(pk=ws2.pk).execution_node_id)

    def test_no_node_at_all_fails_closed(self):
        res = P.allocate_workspace_node(self.ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.ALLOC_NO_CAPACITY)

    def test_inactive_node_not_selected(self):
        TerminalNode.objects.create(hostname="off", status=TerminalNode.Status.OFFLINE, max_accounts=50)
        res = P.allocate_workspace_node(self.ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.ALLOC_NO_CAPACITY)


@override_settings(**_FLAGS_ON)
class ConfirmTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.ws = P.request_hosted_workspace(self.user, expected_login="700900").workspace

    def _make_connected_matched(self):
        self.ws.canonical_state = S.CONNECTED
        self.ws.proj_connected = True
        self.ws.proj_account_match = True
        self.ws.save(update_fields=["canonical_state", "proj_connected", "proj_account_match"])

    def test_confirm_requires_owner(self):
        self._make_connected_matched()
        other = _user("intruder")
        res = P.confirm_broker_account(other, self.ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.CONFIRM_NOT_OWNER)
        self.assertIsNone(self.ws.trading_account.workspace_confirmed_at)

    def test_confirm_requires_observed_match(self):
        # connected but NOT matched — cannot confirm a broker account we have not observed as theirs
        self.ws.canonical_state = S.CONNECTED
        self.ws.proj_account_match = False
        self.ws.save(update_fields=["canonical_state", "proj_account_match"])
        res = P.confirm_broker_account(self.user, self.ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.CONFIRM_NO_MATCH)

    def test_confirm_success_stamps_ack(self):
        self._make_connected_matched()
        res = P.confirm_broker_account(self.user, self.ws)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.reason, P.CONFIRM_OK)
        acct = TradingAccount.objects.get(pk=self.ws.trading_account_id)
        self.assertIsNotNone(acct.workspace_confirmed_at)

    def test_confirm_is_idempotent(self):
        self._make_connected_matched()
        P.confirm_broker_account(self.user, self.ws)
        second = P.confirm_broker_account(self.user, self.ws)
        self.assertTrue(second.ok)
        self.assertEqual(second.reason, P.CONFIRM_ALREADY)

    def test_confirm_denied_when_dark(self):
        self._make_connected_matched()
        with override_settings(HOSTED_WORKSPACE_ONBOARDING_ENABLED="0"):
            res = P.confirm_broker_account(self.user, self.ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, E.DENY_ONBOARDING_DARK)
