"""ADR-0034 Workspace Delivery × Execution Engine — the two-node authority separation (integration).

After the capstone (#317) added ``execution_node`` (order-routing host) and Workspace Delivery (#316) added
``workspace_node`` (RemoteApp delivery host), these are DISTINCT durable facts for DISTINCT authorities.
This module pins the integration invariant that the rebase must preserve:

  - Delivery reads ONLY ``workspace_node`` — it never falls back to ``execution_node``.
  - Delivery derives the RDP host from ``workspace_node`` even when a *different* ``execution_node`` exists.
  - A RemoteApp connection (``delivery_state=CONNECTED`` / ``remoteapp_ready``) is NEVER sufficient to
    authorise an order — execution readiness stays gated on the execution conditions (Section 13).
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import readiness as R
from execution.models import TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from terminal_provisioning.models import AccountProvisioning
from trading.crypto import encrypt_password
from trading.models import TradingAccount

from hosted_workspace.delivery import DeliveryReason, authorize_workspace_delivery
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_SECRET_HEX = "0123456789abcdef0123456789abcdef"


def _delivery_env(**over):
    env = {"HOSTED_PERSISTENT_MT5_ENABLED": "1", "HOSTED_MT5_REMOTEAPP_ENABLED": "1",
           "GUAC_BASE_URL": "https://guac.example.test/guacamole", "GUAC_JSON_SECRET_KEY_HEX": _SECRET_HEX}
    env.update(over)
    return mock.patch.dict(os.environ, env, clear=False)


class NodeAuthoritySeparationTests(TestCase):
    def setUp(self):
        self.owner = U.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.delivery_node = TerminalNode.objects.create(
            hostname="delivery-host-A", rdp_host="10.8.8.8", status=TerminalNode.Status.ACTIVE)
        self.execution_node = TerminalNode.objects.create(
            hostname="execution-host-B", rdp_host="10.7.7.7", status=TerminalNode.Status.ACTIVE)
        self.account = TradingAccount.objects.create(
            user=self.owner, name="a", broker_name="B", account_number="700111", is_demo=True,
            readiness_provider=PERSISTENT_WORKSPACE)
        self.workspace = HostedMt5Workspace.objects.create(trading_account=self.account)
        self.prov = AccountProvisioning.objects.create(
            trading_account=self.account, windows_username="guvfx_u_700",
            runtime_root=r"C:\GuvFX\accounts\700", is_admin=False,
            status=AccountProvisioning.Status.PROVISIONED, password_enc=encrypt_password("pw"))

    def _wid(self):
        return str(self.workspace.workspace_uuid)

    def test_delivery_never_falls_back_to_execution_node(self):
        # workspace_node is NULL but execution_node IS set → delivery MUST fail closed (NODE_UNASSIGNED),
        # proving delivery reads ONLY workspace_node and never borrows the execution host.
        self.workspace.execution_node = self.execution_node
        self.workspace.workspace_node = None
        self.workspace.save(update_fields=["execution_node", "workspace_node", "updated_at"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertFalse(auth.authorized)
        self.assertEqual(auth.reason, DeliveryReason.NODE_UNASSIGNED)

    def test_delivery_host_derives_from_workspace_node_not_execution_node(self):
        # Both set to DIFFERENT hosts. Spy on the RemoteApp payload builder: the host it receives MUST be the
        # delivery (workspace_node) host, never the execution host.
        self.workspace.workspace_node = self.delivery_node       # delivery-host-A
        self.workspace.execution_node = self.execution_node      # execution-host-B
        self.workspace.save(update_fields=["workspace_node", "execution_node", "updated_at"])
        import mt5.guac_json as gj
        seen = {}
        real = gj.build_remoteapp_rdp_payload

        def _spy(**kwargs):
            seen["host"] = kwargs.get("host")
            return real(**kwargs)

        with _delivery_env(), mock.patch.object(gj, "build_remoteapp_rdp_payload", _spy):
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertTrue(auth.authorized, auth.reason)
        # The transport host is the workspace_node's rdp_host — NOT the execution_node's, and NEVER either
        # node's logical hostname/identity.
        self.assertEqual(seen["host"], self.delivery_node.rdp_host)        # 10.8.8.8 (workspace_node) …
        self.assertNotEqual(seen["host"], self.execution_node.rdp_host)    # … NOT 10.7.7.7 (execution_node)
        self.assertNotIn(seen["host"], ("delivery-host-A", "execution-host-B"))  # never a hostname/identity

    def test_delivery_works_when_execution_node_unset(self):
        # Delivery does not REQUIRE an execution binding — a workspace with a delivery host but NO execution
        # node still delivers (the two authorities are independent).
        self.workspace.workspace_node = self.delivery_node
        self.workspace.execution_node = None
        self.workspace.save(update_fields=["workspace_node", "execution_node", "updated_at"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertTrue(auth.authorized, auth.reason)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
    def test_remoteapp_connected_never_grants_execution(self):
        # Section 13 invariant: a live RemoteApp (delivery CONNECTED + remoteapp_ready) must NEVER be
        # sufficient to authorise an order. With execution NOT armed (execution_enabled=False), Provider-B
        # readiness still fail-closes — delivery connection is not execution authority.
        self.workspace.workspace_node = self.delivery_node
        self.workspace.delivery_state = HostedMt5Workspace.DeliveryState.CONNECTED
        self.workspace.remoteapp_ready = True
        self.workspace.proj_connected = True
        self.workspace.proj_account_match = True
        self.workspace.execution_enabled = False                 # NOT armed for execution
        self.workspace.canonical_state = S.EXECUTION_READY
        self.workspace.last_decision_at = timezone.now()
        self.workspace.save()
        dec = R.PersistentWorkspaceProvider().evaluate(self.account)
        self.assertFalse(dec.eligible)
        self.assertEqual(dec.reason_code, R.RW_EXECUTION_DISABLED)  # remoteapp_ready did NOT arm execution
