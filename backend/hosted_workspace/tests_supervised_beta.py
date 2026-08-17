"""ADR-0044 — SUPERVISED_SINGLE_TENANT_BETA.

Proves (a) the bounded fail-closed gate ``supervised_single_tenant_beta_active`` opens ONLY for a single non-CZ
DEMO tenant alone on a dedicated ACTIVE non-CZ node while the flag is on; (b) it composes into
``live_observe.live_observe_fn`` as an OR with the isolation cert WITHOUT touching the cert marker, and stays
fail-closed at the trust anchor otherwise; and (c) ``confirm_broker_account`` activates the intent account
(``is_active``) so the journey can become execution-ready.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import UserSubscriptionState
from execution import readiness as R
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import provisioning as P
from hosted_workspace import supervised_beta as SB
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
# The gate reaches Customer Zero via tenant_isolation.customer_zero_account_ids; patch it so test account ids
# (which Django does not reset per-test) are deterministically non-CZ unless a test says otherwise.
CZ = "hosted_workspace.tenant_isolation.customer_zero_account_ids"

_n = 0


def _uniq():
    global _n
    _n += 1
    return f"9{_n:05d}"


def _node(*, status=TerminalNode.Status.ACTIVE, rdp="10.9.9.9", host=None):
    return TerminalNode.objects.create(hostname=host or f"beta-{_uniq()}", rdp_host=rdp, status=status)


def _acct(node, *, is_demo=True, is_active=True):
    login = _uniq()
    user = U.objects.create_user(username=f"u{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    return TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                         is_demo=is_demo, is_active=is_active, broker_server=srv,
                                         readiness_provider=R.PERSISTENT_WORKSPACE, terminal_node=node)


def _ws(acct, node, *, exec_node=True, deliver_node=True):
    return HostedMt5Workspace.objects.create(
        trading_account=acct, canonical_state=S.EXECUTION_READY,
        execution_node=(node if exec_node else None), workspace_node=(node if deliver_node else None))


@override_settings(SUPERVISED_SINGLE_TENANT_BETA_ENABLED="1", HOSTED_BETA_FORBIDDEN_RDP_HOSTS=())
@mock.patch(CZ, return_value=frozenset())
class SupervisedGateTests(TestCase):
    def _happy(self):
        node = _node()
        acct = _acct(node)
        return node, acct, _ws(acct, node)

    def test_active_when_all_conditions_hold(self, _cz):
        _, _, ws = self._happy()
        self.assertTrue(SB.supervised_single_tenant_beta_active(ws))

    def test_flag_off_is_inactive(self, _cz):
        _, _, ws = self._happy()
        with override_settings(SUPERVISED_SINGLE_TENANT_BETA_ENABLED="0"):
            self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_customer_zero_account_is_inactive(self, _cz):
        _, acct, ws = self._happy()
        with mock.patch(CZ, return_value=frozenset({acct.id})):
            self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_non_demo_account_is_inactive(self, _cz):
        node = _node()
        ws = _ws(_acct(node, is_demo=False), node)
        self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_no_execution_node_is_inactive(self, _cz):
        node = _node()
        ws = _ws(_acct(node), node, exec_node=False)
        self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_non_active_node_is_inactive(self, _cz):
        node = _node(status=TerminalNode.Status.DRAINING)
        ws = _ws(_acct(node), node)
        self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_forbidden_rdp_host_node_is_inactive(self, _cz):
        node = _node(rdp="1.2.3.4")
        ws = _ws(_acct(node), node)
        with override_settings(HOSTED_BETA_FORBIDDEN_RDP_HOSTS=("1.2.3.4",)):
            self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_second_live_account_on_node_breaks_single_tenant(self, _cz):
        node, _, ws = self._happy()
        _acct(node)  # a SECOND live legacy account pinned to the same node
        self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_second_hosted_execution_binding_breaks_single_tenant(self, _cz):
        node, _, ws = self._happy()
        other = _acct(node, is_active=False)
        HostedMt5Workspace.objects.create(trading_account=other, execution_node=node)
        self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_second_hosted_delivery_binding_breaks_single_tenant(self, _cz):
        node, _, ws = self._happy()
        other = _acct(node, is_active=False)
        HostedMt5Workspace.objects.create(trading_account=other, workspace_node=node)
        self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_no_account_is_inactive(self, _cz):
        # A workspace whose trading_account relation is missing -> fail closed.
        ws = self._happy()[2]
        ws.trading_account = None
        self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_beta_coresident_on_shared_host_own_node_is_single_tenant(self, _cz):
        # ADR-0044 AMENDMENT (CLOSED TRUSTED BETA co-residency): two ACTIVE TerminalNode rows on ONE physical
        # box (same rdp_host). A beta tenant ALONE on ITS OWN node IS single-tenant even though a DIFFERENT
        # tenant shares the physical host via a DIFFERENT node — single-tenancy is per-TerminalNode, not per-host.
        node1 = _node(rdp="10.60.0.9", host="beta-a")
        node2 = _node(rdp="10.60.0.9", host="beta-b")
        acct1 = _acct(node1)
        ws1 = _ws(acct1, node1)
        _ws(_acct(node2), node2)                       # a DIFFERENT tenant on the SAME physical host via node2
        self.assertTrue(SB.supervised_single_tenant_beta_active(ws1))

    def test_beta_forbidden_on_customer_zero_own_node(self, _cz):
        # CZ protection under co-residency: even on a shared host, a beta may NEVER bind to CZ's OWN node.
        # forbidden_execution_node_ids (condition 6, checked unconditionally) rejects it regardless of the
        # per-node single-tenant relaxation.
        node_cz = _node(rdp="10.60.0.9", host="cz-node")
        ws = _ws(_acct(node_cz), node_cz)
        with mock.patch("hosted_workspace.tenant_isolation.forbidden_execution_node_ids",
                        return_value={node_cz.id}):
            self.assertFalse(SB.supervised_single_tenant_beta_active(ws))

    def test_blank_rdp_host_fails_closed(self, _cz):
        # Host identity unknown -> cannot prove single-tenant -> fail closed.
        node = _node(rdp="")
        ws = _ws(_acct(node), node)
        self.assertFalse(SB.supervised_single_tenant_beta_active(ws))


class LiveObserveTrustAnchorTests(TestCase):
    """The gate composes as an OR with the isolation cert at the live-observe trust anchor, never faking it."""

    @override_settings(SUPERVISED_SINGLE_TENANT_BETA_ENABLED="0", HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="0",
                       HOSTED_MT5_OBSERVATION_ENABLED="1")
    def test_neither_cert_nor_supervised_fails_closed_without_host_contact(self):
        node = _node()
        ws = _ws(_acct(node), node)
        from hosted_workspace.live_observe import live_observe_fn
        with mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor") as res:
            self.assertIsNone(live_observe_fn(ws))
            res.assert_not_called()  # closed AT the trust anchor — no executor ever resolved

    @override_settings(SUPERVISED_SINGLE_TENANT_BETA_ENABLED="1", HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="0",
                       HOSTED_MT5_OBSERVATION_ENABLED="1", HOSTED_BETA_FORBIDDEN_RDP_HOSTS=())
    @mock.patch(CZ, return_value=frozenset())
    def test_supervised_active_opens_trust_anchor_cert_still_off(self, _cz):
        node = _node()
        ws = _ws(_acct(node), node)
        from hosted_workspace.flags import hosted_remoteapp_isolation_certified
        self.assertFalse(hosted_remoteapp_isolation_certified())  # marker NOT set/implied
        from hosted_workspace.live_observe import live_observe_fn
        with mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor",
                        return_value=None) as res:
            self.assertIsNone(live_observe_fn(ws))   # executor None -> None, but AFTER passing the anchor
            res.assert_called_once()                 # proves the supervised branch opened the anchor


class ConfirmActivationTests(TestCase):
    """ADR-0044 Decision 2: confirmation activates the intent account so it can become execution-ready."""

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1")
    def test_confirm_activates_intent_account(self):
        u = U.objects.create_user(username="conf", email="conf@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(
            user=u, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))
        res = P.request_hosted_workspace(u, expected_login="700123", expected_server="IS6-Demo")
        self.assertTrue(res.ok, res.reason)
        ws = res.workspace
        acct = ws.trading_account
        self.assertIs(acct.is_active, False)  # intent account starts inactive
        ws.canonical_state = S.CONNECTED
        ws.proj_connected = True
        ws.proj_account_match = True
        ws.save(update_fields=["canonical_state", "proj_connected", "proj_account_match"])
        c = P.confirm_broker_account(u, ws)
        self.assertTrue(c.ok, c.reason)
        acct.refresh_from_db()
        self.assertIs(acct.is_active, True)                 # activated by confirmation
        self.assertIsNotNone(acct.workspace_confirmed_at)   # ACK stamped atomically
        self.assertIs(ws.execution_enabled, False)          # activation is NOT arming


def _exec_ready(acct, node, **kw):
    base = dict(canonical_state=S.EXECUTION_READY, proj_connected=True, proj_trade_allowed=True,
                proj_account_match=True, proj_execution_ready=True, last_decision_at=timezone.now(),
                execution_node=node, execution_enabled=True,
                execution_authorized_at=timezone.now())  # ADR-0047: a ready workspace is customer-authorized
    base.update(kw)
    acct.workspace_confirmed_at = timezone.now()
    acct.save(update_fields=["workspace_confirmed_at"])
    return HostedMt5Workspace.objects.create(trading_account=acct, **base)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1",
                   SUPERVISED_SINGLE_TENANT_BETA_ENABLED="1", HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="0",
                   HOSTED_BETA_FORBIDDEN_RDP_HOSTS=())
@mock.patch(CZ, return_value=frozenset())
class ExecutionPostureTests(TestCase):
    """Finding I4: under the supervised posture the single-tenant boundary is enforced at the ORDER/arm gate,
    so a second tenant landing fails execution closed immediately, not only at observation time."""

    def test_readiness_ok_when_single_tenant(self, _cz):
        node = _node()
        acct = _acct(node)
        _exec_ready(acct, node)
        from execution.readiness import evaluate_readiness
        self.assertTrue(evaluate_readiness(acct).eligible)

    def test_readiness_fails_boundary_when_second_tenant_lands(self, _cz):
        node = _node()
        acct = _acct(node)
        _exec_ready(acct, node)
        _acct(node)  # a SECOND live tenant lands on the host
        from execution import readiness as R
        d = R.evaluate_readiness(acct)
        self.assertFalse(d.eligible)
        self.assertEqual(d.reason_code, R.RW_SUPERVISED_BOUNDARY)

    def test_arm_refuses_boundary_when_second_tenant(self, _cz):
        node = _node()
        acct = _acct(node)
        _exec_ready(acct, node, execution_enabled=False)
        _acct(node)
        from execution import readiness as R
        from execution.hosted_provisioning import arm_hosted_workspace_execution
        self.assertEqual(arm_hosted_workspace_execution(acct).reason_code, R.RW_SUPERVISED_BOUNDARY)

    @override_settings(HOSTED_REMOTEAPP_ISOLATION_CERTIFIED="1")
    def test_certified_path_allows_co_residency(self, _cz):
        # When the full cert is held, the single-tenant requirement is NOT applied (co-residency allowed).
        node = _node()
        acct = _acct(node)
        _exec_ready(acct, node)
        _acct(node)  # second tenant present, but certified -> boundary check skipped
        from execution.readiness import evaluate_readiness
        self.assertTrue(evaluate_readiness(acct).eligible)


class CapacityRobustnessTests(TestCase):
    """Finding I3: node capacity counts DISTINCT occupant accounts across BOTH binding sources, so an activated
    hosted account whose terminal_node was cleared (desync) still fills its slot — the allocator cannot over-fill."""

    def test_activated_hosted_account_with_cleared_terminal_node_still_counts(self):
        from hosted_workspace.provisioning import _node_has_capacity
        node = _node(rdp="10.9.9.9")
        node.max_accounts = 1
        node.save(update_fields=["max_accounts"])
        acct = _acct(node, is_active=True)      # activated hosted account …
        HostedMt5Workspace.objects.create(trading_account=acct, execution_node=node)
        acct.terminal_node = None               # … whose terminal_node got cleared (desync)
        acct.save(update_fields=["terminal_node"])
        # Old logic counted this in NEITHER term -> node looked empty. Now it is counted via execution_node.
        self.assertFalse(_node_has_capacity(node))
