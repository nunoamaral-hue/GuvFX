"""ADR-0034 Workspace Delivery — RDP transport endpoint (``TerminalNode.rdp_host``) correction.

Focused regression bar for the identity-vs-transport separation. ``TerminalNode.hostname`` is the logical
execution-node IDENTITY (e.g. ``guvfx-windows-mt5``); it is NOT necessarily an address guacd can dial. The
RemoteApp delivery descriptor's RDP host is the DEDICATED, additive ``TerminalNode.rdp_host`` transport
endpoint. The two are distinct facts and must never be conflated.

Proves (CA acceptance list A–I):
  A. the minted descriptor's RDP host is ``node.rdp_host`` (recovered by decrypting the AES token);
  B. ``node.hostname`` is NEVER used as the transport host;
  C. a node with no ``rdp_host`` fails delivery CLOSED (``DA_NODE_TRANSPORT_UNCONFIGURED``), never falls back
     to ``hostname`` and never mints a descriptor;
  C'. ordering: no node ⇒ ``NODE_UNASSIGNED``; node-without-hostname ⇒ ``NODE_UNASSIGNED``; node-with-hostname
      but no ``rdp_host`` ⇒ ``NODE_TRANSPORT_UNCONFIGURED`` (the two are separate, ordered gates);
  D. execution routing resolves the node INDEPENDENTLY of ``rdp_host`` (a delivery-only field);
  H. the client cannot override the host — there is no host parameter, and ``rdp_host`` is server-derived;
  I. no secret (Windows password) appears in the returned descriptor.

E/F/G (workspace_node = delivery authority, execution_node = execution authority, IDOR) are covered by
``tests_delivery.py`` / ``tests_delivery_node_authority.py`` and re-asserted here only where ``rdp_host`` is
the moving part.
"""
from __future__ import annotations

import base64
import os
import uuid
from unittest import mock
from urllib.parse import unquote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from execution.models import TerminalNode
from terminal_provisioning.models import AccountProvisioning
from trading.crypto import encrypt_password
from trading.models import TradingAccount

from hosted_workspace.delivery import DeliveryReason, authorize_workspace_delivery
from hosted_workspace.models import HostedMt5Workspace

U = get_user_model()

_SECRET_HEX = "0123456789abcdef0123456789abcdef"  # 32 hex chars (128-bit) — test-only
_WINDOWS_PW = "W1ndows-PLAINTEXT-SECRET"
# The two facts held distinct throughout: a logical node IDENTITY and a routable RDP TRANSPORT address.
_NODE_IDENTITY = "guvfx-windows-mt5"
_RDP_TRANSPORT = "100.79.101.19"


def _delivery_env(**over):
    env = {
        "HOSTED_PERSISTENT_MT5_ENABLED": "1",
        "HOSTED_MT5_REMOTEAPP_ENABLED": "1",
        "GUAC_BASE_URL": "https://guac.example.test/guacamole",
        "GUAC_JSON_SECRET_KEY_HEX": _SECRET_HEX,
    }
    env.update(over)
    return mock.patch.dict(os.environ, env, clear=False)


def _decrypt_token(embed_url: str) -> bytes:
    """Recover the AES-CBC(IV=0) plaintext of the guacamole-auth-json token exactly as the Guacamole server
    would, so the test can inspect what host/credential the descriptor actually carries."""
    data_q = embed_url.split("?data=", 1)[1]
    ct = base64.b64decode(unquote(data_q))
    from mt5.guac_json import _key_bytes_from_hex
    key = _key_bytes_from_hex(_SECRET_HEX)
    dec = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
    return dec.update(ct) + dec.finalize()


class _Base(TestCase):
    """A delivery-ready workspace whose node deliberately has DISTINCT hostname and rdp_host."""

    def setUp(self):
        self.owner = U.objects.create_user(username="own", email="own@example.com", password="x")
        self.node = TerminalNode.objects.create(
            hostname=_NODE_IDENTITY, rdp_host=_RDP_TRANSPORT, status=TerminalNode.Status.ACTIVE)
        self.account = TradingAccount.objects.create(
            user=self.owner, name="rdp-acct", broker_name="Broker-Demo",
            account_number="700333", is_demo=True)
        self.workspace = HostedMt5Workspace.objects.create(
            trading_account=self.account, workspace_node=self.node)
        self.prov = AccountProvisioning.objects.create(
            trading_account=self.account, windows_username="guvfx_u_333",
            runtime_root=r"C:\GuvFX\accounts\333", is_admin=False,
            status=AccountProvisioning.Status.PROVISIONED,
            password_enc=encrypt_password(_WINDOWS_PW))

    def _wid(self):
        return str(self.workspace.workspace_uuid)


class DescriptorUsesRdpHostTests(_Base):
    def test_A_descriptor_host_is_rdp_host(self):
        """A: the RDP host embedded in the minted token is node.rdp_host (the transport), not the identity."""
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertTrue(auth.authorized, auth.reason)
        plain = _decrypt_token(auth.descriptor["embed_url"])
        self.assertIn(_RDP_TRANSPORT.encode(), plain)          # transport IS in the token
        self.assertIn(b"||terminal64", plain)                  # single-app RemoteApp alias, /portable

    def test_B_hostname_is_never_the_transport(self):
        """B: the logical identity hostname must NOT appear as the descriptor host (no identity->transport)."""
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        plain = _decrypt_token(auth.descriptor["embed_url"])
        # hostname and rdp_host are deliberately different values, so this assertion is meaningful.
        self.assertNotEqual(self.node.hostname, self.node.rdp_host)
        self.assertNotIn(_NODE_IDENTITY.encode(), plain)

    def test_I_no_plaintext_password_in_descriptor(self):
        """I: the Windows password never appears in the returned descriptor (rides only inside the token)."""
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertNotIn(_WINDOWS_PW, repr(auth))
        self.assertNotIn(_WINDOWS_PW, auth.descriptor["embed_url"])

    def test_H_no_client_host_parameter(self):
        """H: the seam takes only (user, workspace_id); the host is server-derived from the node record, so a
        client has no channel to override it. Passing a UUID object vs string derives the SAME transport."""
        with _delivery_env():
            a1 = authorize_workspace_delivery(self.owner, self._wid())
            a2 = authorize_workspace_delivery(self.owner, self.workspace.workspace_uuid)
        self.assertTrue(a1.authorized and a2.authorized)
        self.assertIn(_RDP_TRANSPORT.encode(), _decrypt_token(a1.descriptor["embed_url"]))
        self.assertIn(_RDP_TRANSPORT.encode(), _decrypt_token(a2.descriptor["embed_url"]))


class MissingRdpHostFailsClosedTests(_Base):
    def test_C_no_rdp_host_denies_transport_unconfigured(self):
        """C: a node WITH an identity but NO rdp_host fails closed — never silently reuses hostname."""
        self.node.rdp_host = ""
        self.node.save(update_fields=["rdp_host"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertFalse(auth.authorized)
        self.assertEqual(auth.reason, DeliveryReason.NODE_TRANSPORT_UNCONFIGURED)
        self.assertIsNone(auth.descriptor)                     # no dud descriptor minted
        # Owned → workspace_pk is carried so the FAILED attempt can be recorded on THEIR OWN row.
        self.assertEqual(auth.workspace_pk, self.workspace.pk)

    def test_C_ordering_no_node_is_node_unassigned_not_transport(self):
        """C': an entirely unassigned node is NODE_UNASSIGNED (a different, earlier gate than transport)."""
        self.workspace.workspace_node = None
        self.workspace.save(update_fields=["workspace_node", "updated_at"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.NODE_UNASSIGNED)

    def test_C_ordering_node_without_hostname_is_node_unassigned(self):
        """C': a node with no hostname is NODE_UNASSIGNED (identity gate) even though it also lacks nothing
        else — the hostname/identity check precedes the transport check."""
        self.node.hostname = ""
        self.node.save(update_fields=["hostname"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.NODE_UNASSIGNED)

    def test_transport_reason_code_is_distinct_and_stable(self):
        self.assertEqual(DeliveryReason.NODE_TRANSPORT_UNCONFIGURED, "DA_NODE_TRANSPORT_UNCONFIGURED")
        self.assertNotEqual(DeliveryReason.NODE_TRANSPORT_UNCONFIGURED, DeliveryReason.NODE_UNASSIGNED)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ExecutionRoutingIsIndependentOfRdpHostTests(TestCase):
    """D: execution routing is a SEPARATE authority. It resolves the execution node from ``execution_node`` /
    ``terminal_node`` and never consults ``rdp_host`` — so the delivery-transport field can be empty, present,
    or changed with zero effect on whether an order may route. Uses the certified ``_armed_account`` fixture."""

    def _armed(self, **over):
        from execution.tests_hosted_routing import _armed_account
        return _armed_account(**over)

    def test_D_execution_routes_with_rdp_host_unset(self):
        from execution.hosted_routing import ER_ROUTE_OK, resolve_hosted_route
        acct = self._armed(login="701000")
        # The armed fixture creates its execution node with NO rdp_host (default "").
        self.assertEqual(acct.terminal_node.rdp_host, "")
        r = resolve_hosted_route(acct)
        self.assertTrue(r.ok, r.reason_code)
        self.assertEqual(r.reason_code, ER_ROUTE_OK)

    def test_D_execution_decision_unchanged_when_rdp_host_toggled(self):
        """Toggling rdp_host on the execution node does not change the routing decision — it is not read."""
        from execution.hosted_routing import ER_ROUTE_OK, resolve_hosted_route
        acct = self._armed(login="701001")
        before = resolve_hosted_route(acct)
        acct.terminal_node.rdp_host = "203.0.113.7"            # set a transport address
        acct.terminal_node.save(update_fields=["rdp_host"])
        acct.refresh_from_db()
        after = resolve_hosted_route(acct)
        self.assertEqual((before.ok, before.reason_code), (True, ER_ROUTE_OK))
        self.assertEqual((after.ok, after.reason_code), (True, ER_ROUTE_OK))
        self.assertEqual(after.expected_login, before.expected_login)  # server-derived identity unchanged


class DeliveryAndExecutionUseDistinctNodeFieldsTests(TestCase):
    """E/F cross-check: the SAME physical node can be the delivery authority (needs rdp_host) while its
    execution authority is unaffected by rdp_host. Delivery reads workspace_node.rdp_host; execution reads
    execution_node / terminal_node — the fields do not cross."""

    def test_delivery_requires_rdp_host_while_execution_does_not(self):
        # Build a delivery-shaped workspace on a node with a hostname but NO rdp_host.
        owner = U.objects.create_user(username="dx", email="dx@example.com", password="x")
        node = TerminalNode.objects.create(
            hostname=_NODE_IDENTITY, rdp_host="", status=TerminalNode.Status.ACTIVE)
        account = TradingAccount.objects.create(
            user=owner, name="dx-acct", broker_name="B", account_number="702000", is_demo=True)
        ws = HostedMt5Workspace.objects.create(trading_account=account, workspace_node=node)
        AccountProvisioning.objects.create(
            trading_account=account, windows_username="guvfx_u_2000",
            runtime_root=r"C:\GuvFX\accounts\2000", is_admin=False,
            status=AccountProvisioning.Status.PROVISIONED,
            password_enc=encrypt_password(_WINDOWS_PW))
        # DELIVERY: fails closed with no rdp_host…
        with _delivery_env():
            auth = authorize_workspace_delivery(owner, str(ws.workspace_uuid))
        self.assertEqual(auth.reason, DeliveryReason.NODE_TRANSPORT_UNCONFIGURED)
        # …while the node's execution identity (hostname) is intact and untouched by the transport gap.
        node.refresh_from_db()
        self.assertEqual(node.hostname, _NODE_IDENTITY)
        self.assertEqual(node.rdp_host, "")
