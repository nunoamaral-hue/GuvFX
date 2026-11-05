"""Shared fixtures for the ADR-0075 monitoring-gateway tests (NOT a test module — no ``tests_`` prefix so
Django discovery ignores it). Builds the minimal real object graph: node → tenant (account+workspace+
isolation profile) → per-account endpoint → gateway → route."""
from __future__ import annotations

from django.contrib.auth import get_user_model

from execution.models import HostedExecutionEndpoint, TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from hosted_workspace.models import HostedMt5Workspace
from terminal_provisioning.models import AccountProvisioning
from trading.models import TradingAccount

FLAGS_ON = {"MONITORING_GATEWAY_ENABLED": "1"}
FLAGS_RECON = {"MONITORING_GATEWAY_ENABLED": "1", "MONITORING_ROUTE_RECONCILE_ENABLED": "1"}


def make_node(hostname="beta-node-1", rdp_host="100.79.101.19", max_accounts=50):
    return TerminalNode.objects.create(hostname=hostname, rdp_host=rdp_host, max_accounts=max_accounts)


def make_tenant(login, *, node, server="IS6Technologies-Live", is_demo=False, is_active=False):
    """A hosted (Provider-B) tenant. ``broker_name`` carries the expected server pin (no BrokerServer row
    needed — the derivation falls back to broker_name)."""
    user = get_user_model().objects.create_user(
        username=f"t-{login}", email=f"t-{login}@x.invalid", password="x")
    acct = TradingAccount.objects.create(
        user=user, name=f"a{login}", broker_name=server, account_number=str(login),
        is_demo=is_demo, is_active=is_active, readiness_provider=PERSISTENT_WORKSPACE,
        terminal_node=node)
    ws = HostedMt5Workspace.objects.create(trading_account=acct, execution_node=node, workspace_node=node)
    AccountProvisioning.objects.create(
        trading_account=acct, windows_username=f"guvfx_u_{login}",
        runtime_root=fr"C:\GuvFX\accounts\{acct.id}", status=AccountProvisioning.Status.PROVISIONED)
    return acct, ws


def make_endpoint(acct, ws, node, *, host="100.79.101.19", port=8800, is_demo=False,
                  state=None):
    state = state or HostedExecutionEndpoint.State.READY
    return HostedExecutionEndpoint.objects.create(
        workspace=ws, trading_account=acct, terminal_node=node, host=host, port=port,
        base_url=f"http://{host}:{port}", windows_username=f"guvfx_u_{acct.account_number}",
        runtime_path="x", workspace_uuid=ws.workspace_uuid, is_demo=is_demo, state=state)


def gateway_for(node, **kw):
    from execution.monitoring_allocation import ensure_gateway
    return ensure_gateway(node.rdp_host, gateway_host=node.rdp_host, gateway_port=8790, **kw)
