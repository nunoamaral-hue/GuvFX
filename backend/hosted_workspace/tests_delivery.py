"""ADR-0034 Workspace Delivery — the owner-authorised delivery seam + read model + DARK API.

The security bar for the delivery linchpin (``authorize_workspace_delivery``):
  - DARK: both flags OFF ⇒ denied with ZERO DB queries (subsystem invisible).
  - Owner-bound: the owner is authorised; ANOTHER user is denied NOT_OWNER (IDOR); NO staff bypass mints.
  - Server-derived: host / username / program / args / credential all come from durable records; the client
    supplies only the workspace uuid; the returned descriptor exposes NONE of the internals.
  - No-secret: the plaintext Windows password never appears in the returned descriptor (it rides ONLY in
    the AES-encrypted token inside ``embed_url``).
  - Fail-closed matrix: missing/malformed workspace, unassigned node, missing/admin/not-provisioned
    identity, missing runtime, unconfigured guac — each a distinct stable reason code, never an authorise.
"""
from __future__ import annotations

import os
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from execution.models import TerminalNode
from terminal_provisioning.models import AccountProvisioning
from trading.crypto import encrypt_password
from trading.models import TradingAccount

from hosted_workspace.delivery import (
    DeliveryReason,
    authorize_workspace_delivery,
)
from hosted_workspace.delivery_read_model import delivery_state_projection
from hosted_workspace.models import HostedMt5Workspace

U = get_user_model()

_SECRET_HEX = "0123456789abcdef0123456789abcdef"  # 32 hex chars (128-bit) — test-only
_WINDOWS_PW = "W1ndows-PLAINTEXT-SECRET"


def _delivery_env(**over):
    env = {
        "HOSTED_PERSISTENT_MT5_ENABLED": "1",
        "HOSTED_MT5_REMOTEAPP_ENABLED": "1",
        "GUAC_BASE_URL": "https://guac.example.test/guacamole",
        "GUAC_JSON_SECRET_KEY_HEX": _SECRET_HEX,
    }
    env.update(over)
    return mock.patch.dict(os.environ, env, clear=False)


class _Base(TestCase):
    def setUp(self):
        self.owner = U.objects.create_user(username="owner", email="owner@example.com", password="x")
        self.other = U.objects.create_user(username="other", email="other@example.com", password="x")
        self.staff = U.objects.create_user(
            username="staff", email="staff@example.com", password="x", is_staff=True)
        self.node = TerminalNode.objects.create(
            hostname="mt5-node-1", rdp_host="10.9.9.9", status=TerminalNode.Status.ACTIVE)
        self.account = TradingAccount.objects.create(
            user=self.owner, name="wd-acct", broker_name="Broker-Demo",
            account_number="700111", is_demo=True)
        self.workspace = HostedMt5Workspace.objects.create(
            trading_account=self.account, workspace_node=self.node)
        self.prov = AccountProvisioning.objects.create(
            trading_account=self.account, windows_username="guvfx_u_700",
            runtime_root=r"C:\GuvFX\accounts\700", is_admin=False,
            status=AccountProvisioning.Status.PROVISIONED,
            password_enc=encrypt_password(_WINDOWS_PW))

    def _wid(self):
        return str(self.workspace.workspace_uuid)


class DeliveryDarkGateTests(_Base):
    def test_both_flags_off_denied_with_zero_queries(self):
        with mock.patch.dict(os.environ, {
                "HOSTED_PERSISTENT_MT5_ENABLED": "0",
                "HOSTED_MT5_REMOTEAPP_ENABLED": "0"}, clear=False):
            with self.assertNumQueries(0):
                auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertFalse(auth.authorized)
        self.assertEqual(auth.reason, DeliveryReason.SUBSYSTEM_DISABLED)
        self.assertIsNone(auth.descriptor)

    def test_master_on_but_delivery_off_still_dark(self):
        with _delivery_env(HOSTED_MT5_REMOTEAPP_ENABLED="0"):
            with self.assertNumQueries(0):
                auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.SUBSYSTEM_DISABLED)


class DeliveryOwnershipTests(_Base):
    def test_owner_is_authorised(self):
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertTrue(auth.authorized)
        self.assertEqual(auth.reason, DeliveryReason.OK)
        self.assertEqual(auth.workspace_uuid, self._wid())
        self.assertIsNotNone(auth.descriptor)

    def test_other_user_denied_not_owner(self):
        with _delivery_env():
            auth = authorize_workspace_delivery(self.other, self._wid())
        self.assertFalse(auth.authorized)
        self.assertEqual(auth.reason, DeliveryReason.NOT_OWNER)
        self.assertIsNone(auth.descriptor)
        # IDOR-safe: a non-owner never receives workspace_pk (so no write can target this row for them).
        self.assertIsNone(auth.workspace_pk)

    def test_staff_gets_no_mint_bypass(self):
        """Staff have READ bypass on the state API, but must NOT be able to MINT another user's session."""
        with _delivery_env():
            auth = authorize_workspace_delivery(self.staff, self._wid())
        self.assertFalse(auth.authorized)
        self.assertEqual(auth.reason, DeliveryReason.NOT_OWNER)

    def test_missing_workspace_denied(self):
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, str(uuid.uuid4()))
        self.assertEqual(auth.reason, DeliveryReason.WORKSPACE_MISSING)

    def test_malformed_id_denied(self):
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, "not-a-uuid")
        self.assertEqual(auth.reason, DeliveryReason.INVALID_REQUEST)

    def test_unauthenticated_denied(self):
        class Anon:
            is_authenticated = False
            id = None
        with _delivery_env():
            auth = authorize_workspace_delivery(Anon(), self._wid())
        self.assertEqual(auth.reason, DeliveryReason.INVALID_REQUEST)

    def test_owner_comparison_kills_equality_mutant(self):
        """Behavioural mutation adequacy on the owner comparison: owner authorises AND non-owner is denied.
        A ``!=``→``==`` mutant would authorise the non-owner (and deny the owner) — both asserted here."""
        with _delivery_env():
            self.assertTrue(authorize_workspace_delivery(self.owner, self._wid()).authorized)
            self.assertFalse(authorize_workspace_delivery(self.other, self._wid()).authorized)


class DeliveryFailClosedMatrixTests(_Base):
    def test_node_unassigned(self):
        self.workspace.workspace_node = None
        self.workspace.save(update_fields=["workspace_node", "updated_at"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.NODE_UNASSIGNED)
        self.assertEqual(auth.workspace_pk, self.workspace.pk)  # owned → pk carried for state write

    def test_identity_missing(self):
        self.prov.delete()
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.IDENTITY_MISSING)

    def test_identity_admin_hard_fail(self):
        self.prov.is_admin = True
        self.prov.save(update_fields=["is_admin"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.IDENTITY_ADMIN)

    def test_identity_not_provisioned(self):
        self.prov.status = AccountProvisioning.Status.DISABLED
        self.prov.save(update_fields=["status"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.IDENTITY_NOT_PROVISIONED)

    def test_runtime_missing(self):
        self.prov.runtime_root = ""
        self.prov.save(update_fields=["runtime_root"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.RUNTIME_MISSING)

    def test_empty_credential_fails_closed(self):
        self.prov.password_enc = ""
        self.prov.save(update_fields=["password_enc"])
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.IDENTITY_NO_CREDENTIAL)
        self.assertIsNone(auth.descriptor)

    def test_guac_unconfigured(self):
        with _delivery_env(GUAC_JSON_SECRET_KEY_HEX=""):
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertEqual(auth.reason, DeliveryReason.GUAC_UNCONFIGURED)


class DeliveryServerDerivationTests(_Base):
    def test_descriptor_is_safe_four_fields_only(self):
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        self.assertTrue(auth.authorized)
        self.assertEqual(set(auth.descriptor), {"transport_type", "embed_url", "session_token", "expiry"})
        self.assertEqual(auth.descriptor["transport_type"], "rdp_remoteapp")
        self.assertEqual(auth.descriptor["session_token"], "")  # everything rides in the AES blob
        self.assertTrue(auth.descriptor["embed_url"].startswith("https://guac.example.test/guacamole"))

    def test_no_plaintext_password_anywhere_in_result(self):
        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        # The plaintext Windows password must NOT appear in the returned descriptor / result at all.
        blob = repr(auth)
        self.assertNotIn(_WINDOWS_PW, blob)
        self.assertNotIn(_WINDOWS_PW, auth.descriptor["embed_url"])

    def test_credential_is_inside_the_encrypted_token(self):
        """Prove the password DID make it into the connection — but only as ciphertext. Decrypting the
        AES token recovers the plaintext, proving the credential rides inside the token (not the return)."""
        import base64
        from urllib.parse import unquote

        from mt5.guac_json import _key_bytes_from_hex
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        with _delivery_env():
            auth = authorize_workspace_delivery(self.owner, self._wid())
        # build_guac_data_url puts ?data=<b64> in the URL FRAGMENT (after '#'), not the query string.
        # Extract it and AES-decrypt (IV=0, key=secret) exactly as the Guacamole server would.
        data_q = auth.descriptor["embed_url"].split("?data=", 1)[1]
        ct = base64.b64decode(unquote(data_q))
        key = _key_bytes_from_hex(_SECRET_HEX)
        dec = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
        plain = dec.update(ct) + dec.finalize()
        self.assertIn(_WINDOWS_PW.encode(), plain)               # inside the token…
        self.assertIn(b"||terminal64", plain)                    # …with the single-app RemoteApp alias
        # …and the server-derived delivery TRANSPORT is node.rdp_host, NOT the execution identity hostname.
        self.assertIn(self.node.rdp_host.encode(), plain)
        self.assertNotIn(self.node.hostname.encode(), plain)     # identity is never used as the RDP host

    def test_client_cannot_influence_derivation(self):
        """The seam takes only (user, workspace_id). Even if a caller passes a UUID object vs string, the
        derived host/username come solely from the server records — proven by the token content above and by
        there being no client-supplied host/username parameter to begin with."""
        with _delivery_env():
            a1 = authorize_workspace_delivery(self.owner, self._wid())
            a2 = authorize_workspace_delivery(self.owner, self.workspace.workspace_uuid)  # UUID object
        self.assertTrue(a1.authorized and a2.authorized)


class DeliveryReadModelTests(_Base):
    def test_customer_projection_is_secret_free(self):
        proj = delivery_state_projection(self.workspace, staff=False)
        flat = repr(proj)
        self.assertNotIn(self.prov.windows_username, flat)   # no Windows username
        self.assertNotIn(self.prov.runtime_root, flat)       # no runtime path
        self.assertNotIn(self.node.hostname, flat)           # identity is operator-only, not customer-facing
        self.assertNotIn(self.node.rdp_host, flat)           # transport host is operator-only too
        self.assertNotIn("operator", proj)
        self.assertIn("delivery_state", proj)
        self.assertIn("remoteapp_ready", proj)

    def test_staff_projection_adds_host_but_no_secret(self):
        proj = delivery_state_projection(self.workspace, staff=True)
        # Operator sees BOTH: delivery_host is the RDP transport (rdp_host); node_identity is the logical
        # execution-node name (hostname). They are deliberately distinct and must not be conflated.
        self.assertEqual(proj["operator"]["delivery_host"], self.node.rdp_host)
        self.assertEqual(proj["operator"]["node_identity"], self.node.hostname)
        flat = repr(proj)
        self.assertNotIn(self.prov.windows_username, flat)
        self.assertNotIn(self.prov.runtime_root, flat)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_MT5_REMOTEAPP_ENABLED=True)
class DeliveryApiTests(_Base):
    """DRF auth here is JWT-only (``CookieJWTAuthentication``) — ``force_login`` does NOT authenticate it,
    so we drive the view via ``APIRequestFactory`` + ``force_authenticate`` (parity with the M3c API tests).
    Flags are toggled with ``override_settings`` because ``_flag`` reads settings before env."""

    def setUp(self):
        super().setUp()
        from rest_framework.test import APIRequestFactory
        self.factory = APIRequestFactory()

    def _get(self, user=None, query=""):
        from rest_framework.test import force_authenticate

        from hosted_workspace.delivery_views import HostedWorkspaceDeliveryStateView
        req = self.factory.get(f"/api/hosted-workspace/delivery-state/{query}")
        if user is not None:
            force_authenticate(req, user=user)
        return HostedWorkspaceDeliveryStateView.as_view()(req)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=False, HOSTED_MT5_REMOTEAPP_ENABLED=False)
    def test_dark_404_when_flags_off(self):
        r = self._get(self.owner, f"?account_id={self.account.id}")
        self.assertEqual(r.status_code, 404)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_MT5_REMOTEAPP_ENABLED=False)
    def test_delivery_flag_off_still_dark(self):
        r = self._get(self.owner, f"?account_id={self.account.id}")
        self.assertEqual(r.status_code, 404)

    def test_owner_reads_own(self):
        r = self._get(self.owner, f"?account_id={self.account.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["delivery_state"], "NONE")
        self.assertNotIn("operator", r.data)

    def test_non_owner_gets_404_idor(self):
        r = self._get(self.other, f"?account_id={self.account.id}")
        self.assertEqual(r.status_code, 404)

    def test_staff_read_bypass(self):
        r = self._get(self.staff, f"?account_id={self.account.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["operator"]["delivery_host"], self.node.rdp_host)     # RDP transport
        self.assertEqual(r.data["operator"]["node_identity"], self.node.hostname)     # logical identity

    def test_missing_account_id_400(self):
        r = self._get(self.owner)
        self.assertEqual(r.status_code, 400)

    def test_out_of_range_id_404(self):
        r = self._get(self.owner, f"?account_id={1 << 64}")
        self.assertEqual(r.status_code, 404)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_MT5_REMOTEAPP_ENABLED=True)
class DeliveryConnectApiTests(_Base):
    """POST /api/hosted-workspace/delivery-connect/ — owner-scoped RemoteApp descriptor mint.

    Security bar: DARK-invisible; owner mints; non-owner AND staff both 404 (NO staff mint bypass — the
    difference from the read endpoint); owned-but-not-deliverable is a 409 with a stable non-secret reason;
    the Windows password never appears in the response; and minting records the attempt but touches NO
    execution state (delivery-only)."""

    def setUp(self):
        super().setUp()
        from rest_framework.test import APIRequestFactory
        self.factory = APIRequestFactory()
        # authorize_workspace_delivery reads GUAC_* from the environment (never settings) — set them so the
        # mint can complete inside these override_settings-flagged tests.
        p = mock.patch.dict(os.environ, {
            "GUAC_BASE_URL": "https://guac.example.test/guacamole",
            "GUAC_JSON_SECRET_KEY_HEX": _SECRET_HEX}, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def _post(self, user=None, body=None, omit_account=False):
        from rest_framework.test import force_authenticate

        from hosted_workspace.delivery_views import HostedWorkspaceDeliveryConnectView
        payload = {} if omit_account else {"account_id": self.account.id}
        if body is not None:
            payload = body
        req = self.factory.post("/api/hosted-workspace/delivery-connect/", payload, format="json")
        if user is not None:
            force_authenticate(req, user=user)
        return HostedWorkspaceDeliveryConnectView.as_view()(req)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=False, HOSTED_MT5_REMOTEAPP_ENABLED=False)
    def test_dark_404_when_flags_off(self):
        self.assertEqual(self._post(self.owner).status_code, 404)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_MT5_REMOTEAPP_ENABLED=False)
    def test_delivery_flag_off_still_dark(self):
        self.assertEqual(self._post(self.owner).status_code, 404)

    def test_owner_mints_safe_descriptor(self):
        r = self._post(self.owner)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.data), {"transport_type", "embed_url", "session_token", "expiry"})
        self.assertEqual(r.data["transport_type"], "rdp_remoteapp")
        self.assertEqual(r.data["session_token"], "")
        self.assertTrue(r.data["embed_url"].startswith("https://guac.example.test/guacamole"))

    def test_non_owner_404_idor(self):
        self.assertEqual(self._post(self.other).status_code, 404)

    def test_staff_gets_NO_mint_bypass(self):
        # Staff have a READ bypass on the state endpoint, but must NOT be able to MINT another user's session.
        self.assertEqual(self._post(self.staff).status_code, 404)

    def test_missing_account_id_400(self):
        self.assertEqual(self._post(self.owner, omit_account=True).status_code, 400)

    def test_no_hosted_workspace_404(self):
        self.workspace.delete()
        self.assertEqual(self._post(self.owner).status_code, 404)

    def test_owned_but_not_deliverable_returns_409_with_reason(self):
        # Owner, but the node has no transport endpoint -> fail closed with the stable, non-secret reason.
        self.node.rdp_host = ""
        self.node.save(update_fields=["rdp_host"])
        r = self._post(self.owner)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["reason"], DeliveryReason.NODE_TRANSPORT_UNCONFIGURED)

    def test_no_plaintext_password_in_response(self):
        r = self._post(self.owner)
        self.assertEqual(r.status_code, 200)
        blob = repr(r.data)
        self.assertNotIn(_WINDOWS_PW, blob)
        self.assertNotIn(_WINDOWS_PW, r.data["embed_url"])

    def test_mint_records_attempt_authorized(self):
        self._post(self.owner)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.delivery_state, HostedMt5Workspace.DeliveryState.AUTHORIZED)

    def test_mint_touches_no_execution(self):
        # Delivery-only: minting a connection must never create/route an execution job.
        from execution.models import ExecutionJob
        before = ExecutionJob.objects.count()
        r = self._post(self.owner)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ExecutionJob.objects.count(), before)  # no execution side effect

    def test_client_cannot_override_host_or_program(self):
        # Even if the client sends host/username/program/args, they are ignored — the server derives them.
        r = self._post(self.owner, body={
            "account_id": self.account.id, "host": "1.2.3.4", "windows_username": "attacker",
            "remote_app": "cmd", "remote_app_args": "/c calc"})
        self.assertEqual(r.status_code, 200)
        import base64
        from urllib.parse import unquote
        from mt5.guac_json import _key_bytes_from_hex
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        ct = base64.b64decode(unquote(r.data["embed_url"].split("?data=", 1)[1]))
        dec = Cipher(algorithms.AES(_key_bytes_from_hex(_SECRET_HEX)), modes.CBC(b"\x00" * 16)).decryptor()
        plain = dec.update(ct) + dec.finalize()
        self.assertIn(self.node.rdp_host.encode(), plain)   # server host, not the client's 1.2.3.4
        self.assertNotIn(b"1.2.3.4", plain)
        self.assertIn(b"||terminal64", plain)               # server program, not the client's cmd
        self.assertNotIn(b"attacker", plain)
