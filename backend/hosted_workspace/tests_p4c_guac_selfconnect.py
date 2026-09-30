"""P4-c: the system-initiated guacd self-connect driver (guac_selfconnect) + delivery.build_recovery_connection.

Proves (host-free, injected seams): a SHORT-lived recovery token is built with the STABLE per-workspace conn id
(reconnect rejoins, never a 2nd session); the establish orchestration mints -> drives the tunnel -> ALWAYS revokes
the authToken (even on failure); fail-closed on every missing-config / mint / tunnel failure with SANITISED reasons
(no password/token/blob in the result); ABSENT->established vs DISCONNECTED->reconnected; and the arm-gate resolver
returns None (probe-only) unless the self-connect sub-gate is on. The real guac WS wire is validated at the final
reboot; here the pure-stdlib tunnel is only smoke-checked for fail-closed behaviour.
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import readiness as R
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import guac_selfconnect as GSC
from hosted_workspace.models import HostedMt5Workspace

U = get_user_model()
_n = 0
_GUAC_ENV = {"GUAC_BASE_URL": "https://guac.invalid/guacamole", "GUAC_JSON_SECRET_KEY_HEX": "00" * 16}


def _mk():
    global _n
    _n += 1
    login = f"98{_n:04d}"
    aid = 600_000 + _n
    user = U.objects.create_user(username=f"gsc{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    node = TerminalNode.objects.create(hostname=f"n-{login}", status=TerminalNode.Status.ACTIVE,
                                       rdp_host="100.79.101.19")
    acct = TradingAccount.objects.create(
        id=aid, user=user, name="a", broker_name="B", account_number=login, is_demo=True, is_active=True,
        broker_server=srv, readiness_provider=R.PERSISTENT_WORKSPACE, terminal_node=node,
        workspace_confirmed_at=timezone.now())
    ws = HostedMt5Workspace.objects.create(trading_account=acct, workspace_node=node, execution_node=node,
                                           canonical_state="EXECUTION_READY")
    from terminal_provisioning.models import AccountProvisioning
    prov = AccountProvisioning.objects.create(
        trading_account=acct, windows_username=f"guvfx_u_{aid}", password_enc="fernet-blob-not-real",
        is_admin=False, runtime_root=rf"C:\GuvFX\accounts\{aid}",
        status=AccountProvisioning.Status.PROVISIONED)
    return ws, acct, node, prov


class _RecTokens:
    def __init__(self, *, token="tok-abc", mint_raises=False, revoke_ok=True):
        self.calls = []
        self._token = token
        self._mint_raises = mint_raises
        self._revoke_ok = revoke_ok

    def mint(self, base_url, data_b64, *, timeout_s=10.0):
        self.calls.append(("mint", base_url, data_b64))
        if self._mint_raises:
            raise RuntimeError("mint boom")
        return self._token

    def revoke(self, base_url, auth_token, *, timeout_s=10.0):
        self.calls.append(("revoke", base_url, auth_token))
        return self._revoke_ok


class _RecTunnel:
    def __init__(self, *, confirm=True, raises=False):
        self.calls = []
        self._confirm = confirm
        self._raises = raises

    def connect_and_confirm(self, *, base_url, auth_token, conn_id, timeout_s=25.0):
        self.calls.append((base_url, auth_token, conn_id))
        if self._raises:
            raise RuntimeError("tunnel boom")
        return self._confirm

    @property
    def mint_count(self):
        return None


_FAKE_CONN = {"data_b64": "BLOB", "conn_id": "mt5-workspace-uuid", "client_id": "CID",
              "base_url": "https://guac.invalid/guacamole", "expiry": 1}


def _run(status, *, tokens, tunnel, conn=None, delivery_ready=True):
    ws, acct, node, prov = _mk()
    with override_settings(**_GUAC_ENV), mock.patch.dict(os.environ, _GUAC_ENV), \
            mock.patch("hosted_workspace.delivery.workspace_delivery_ready", return_value=delivery_ready), \
            mock.patch("hosted_workspace.delivery.build_recovery_connection", return_value=(conn or _FAKE_CONN)):
        return GSC.establish_session(ws, acct, node, status, token_client=tokens, tunnel_client=tunnel)


class EstablishOrchestrationTests(TestCase):
    def test_absent_establishes_and_revokes(self):
        tokens, tunnel = _RecTokens(), _RecTunnel(confirm=True)
        out = _run("ABSENT", tokens=tokens, tunnel=tunnel)
        self.assertEqual(out, {"ok": True, "reason": "established"})
        self.assertEqual([c[0] for c in tokens.calls], ["mint", "revoke"])   # minted then ALWAYS revoked
        self.assertEqual(tokens.calls[1][2], "tok-abc")                       # revoked the exact minted token
        self.assertEqual(len(tunnel.calls), 1)

    def test_disconnected_reconnects_and_revokes(self):
        tokens, tunnel = _RecTokens(), _RecTunnel(confirm=True)
        out = _run("DISCONNECTED", tokens=tokens, tunnel=tunnel)
        self.assertEqual(out, {"ok": True, "reason": "reconnected"})
        self.assertIn(("revoke", "https://guac.invalid/guacamole", "tok-abc"), tokens.calls)

    def test_tunnel_not_confirmed_still_revokes(self):
        tokens, tunnel = _RecTokens(), _RecTunnel(confirm=False)
        out = _run("ABSENT", tokens=tokens, tunnel=tunnel)
        self.assertEqual(out, {"ok": False, "reason": "tunnel_not_confirmed"})
        self.assertEqual([c[0] for c in tokens.calls], ["mint", "revoke"])   # revoke ALWAYS happens (finally)

    def test_tunnel_exception_is_fail_closed_and_revokes(self):
        tokens, tunnel = _RecTokens(), _RecTunnel(raises=True)
        out = _run("ABSENT", tokens=tokens, tunnel=tunnel)
        self.assertEqual(out, {"ok": False, "reason": "tunnel_not_confirmed"})
        self.assertEqual([c[0] for c in tokens.calls], ["mint", "revoke"])

    def test_mint_failure_is_fail_closed_no_revoke(self):
        tokens, tunnel = _RecTokens(mint_raises=True), _RecTunnel()
        out = _run("ABSENT", tokens=tokens, tunnel=tunnel)
        self.assertEqual(out, {"ok": False, "reason": "token_mint_failed"})
        self.assertEqual([c[0] for c in tokens.calls], ["mint"])             # no token minted -> nothing to revoke
        self.assertEqual(tunnel.calls, [])                                   # never reached the tunnel

    def test_revoke_failure_does_not_flip_success(self):
        tokens, tunnel = _RecTokens(revoke_ok=False), _RecTunnel(confirm=True)
        out = _run("ABSENT", tokens=tokens, tunnel=tunnel)
        self.assertEqual(out, {"ok": True, "reason": "established"})          # short TTL is the backstop
        self.assertEqual([c[0] for c in tokens.calls], ["mint", "revoke"])

    def test_delivery_not_ready_is_fail_closed_before_mint(self):
        tokens, tunnel = _RecTokens(), _RecTunnel()
        out = _run("ABSENT", tokens=tokens, tunnel=tunnel, delivery_ready=False)
        self.assertEqual(out, {"ok": False, "reason": "delivery_not_ready"})
        self.assertEqual(tokens.calls, [])                                   # zero decryption / zero mint

    def test_result_is_secret_free(self):
        tokens, tunnel = _RecTokens(), _RecTunnel(confirm=True)
        out = _run("ABSENT", tokens=tokens, tunnel=tunnel)
        blob = repr(out)
        for secret in ("fernet", "BLOB", "tok-abc", "password", "00" * 16):
            self.assertNotIn(secret, blob)                                   # only ok + enum-ish reason

    def test_guac_unconfigured_is_fail_closed(self):
        ws, acct, node, prov = _mk()
        tokens = _RecTokens()
        with mock.patch.dict(os.environ, {"GUAC_BASE_URL": "", "GUAC_JSON_SECRET_KEY_HEX": ""}, clear=False), \
                mock.patch("hosted_workspace.delivery.workspace_delivery_ready", return_value=True):
            out = GSC.establish_session(ws, acct, node, "ABSENT", token_client=tokens, tunnel_client=_RecTunnel())
        self.assertEqual(out, {"ok": False, "reason": "guac_unconfigured"})
        self.assertEqual(tokens.calls, [])

    def test_node_divergence_fails_closed_before_mint(self):
        # MED-2: the mint host must be ws.workspace_node; a passed node that diverges fails closed (no decrypt/mint).
        ws, acct, node, prov = _mk()
        other = TerminalNode.objects.create(hostname="n-x", status=TerminalNode.Status.ACTIVE, rdp_host="1.2.3.4")
        tokens = _RecTokens()
        with override_settings(**_GUAC_ENV), mock.patch.dict(os.environ, _GUAC_ENV), \
                mock.patch("hosted_workspace.delivery.workspace_delivery_ready", return_value=True):
            out = GSC.establish_session(ws, acct, other, "ABSENT", token_client=tokens, tunnel_client=_RecTunnel())
        self.assertEqual(out, {"ok": False, "reason": "node_divergence"})
        self.assertEqual(tokens.calls, [])

    def test_logs_are_secret_free(self):
        # NIT-1: force the revoke-failure log path and assert no token/blob/password reaches the log lines.
        tokens, tunnel = _RecTokens(revoke_ok=False), _RecTunnel(confirm=True)
        with self.assertLogs("guvfx.hosted_workspace", level="WARNING") as cm:
            _run("ABSENT", tokens=tokens, tunnel=tunnel)
        logtext = "\n".join(cm.output)
        for secret in ("tok-abc", "BLOB", "fernet", "password", "00" * 16):
            self.assertNotIn(secret, logtext)

    def test_establish_forwards_short_ttl(self):
        # NIT-1: prove establish_session forwards the ~45s recovery TTL end-to-end (never the 1h default).
        captured = {}

        def _cap(**kw):
            captured.update(kw)
            return _FAKE_CONN
        ws, acct, node, prov = _mk()
        with override_settings(**_GUAC_ENV), mock.patch.dict(os.environ, _GUAC_ENV), \
                mock.patch("hosted_workspace.delivery.workspace_delivery_ready", return_value=True), \
                mock.patch("hosted_workspace.delivery.build_recovery_connection", side_effect=_cap):
            GSC.establish_session(ws, acct, node, "ABSENT", token_client=_RecTokens(), tunnel_client=_RecTunnel())
        self.assertEqual(captured.get("ttl_ms"), GSC._RECOVERY_TOKEN_TTL_MS)
        self.assertLessEqual(captured.get("ttl_ms"), 60_000)


class RecoveryConnectionTests(TestCase):
    def test_short_ttl_and_stable_conn_id(self):
        from hosted_workspace.delivery import build_recovery_connection
        ws, acct, node, prov = _mk()
        with mock.patch("trading.crypto.decrypt_password", return_value="win-pw"):
            conn = build_recovery_connection(workspace=ws, prov=prov, node=node,
                                             base_url="https://guac.invalid/guacamole", secret_hex="00" * 16,
                                             ttl_ms=45_000)
        self.assertEqual(conn["conn_id"], f"mt5-workspace-{ws.workspace_uuid}")   # stable id => reconnect rejoins
        # MED-1: recovery + human paths derive the conn id from the SAME shared helper (no drift => single-session).
        from hosted_workspace.delivery import workspace_conn_id
        self.assertEqual(conn["conn_id"], workspace_conn_id(ws))
        self.assertTrue(conn["data_b64"])
        self.assertTrue(conn["client_id"])
        offset = conn["expiry"] - int(timezone.now().timestamp() * 1000)
        self.assertGreater(offset, 40_000)
        self.assertLess(offset, 60_000)                                          # short, never the 1h member token

    def test_recovery_token_never_logs_the_windows_password(self):
        from hosted_workspace.delivery import build_recovery_connection
        ws, acct, node, prov = _mk()
        with mock.patch("trading.crypto.decrypt_password", return_value="SUPERSECRET-PW"):
            conn = build_recovery_connection(workspace=ws, prov=prov, node=node,
                                             base_url="https://guac.invalid/guacamole", secret_hex="00" * 16)
        # the password rides ONLY inside the encrypted blob, never as a plaintext field
        self.assertNotIn("SUPERSECRET-PW", repr({k: v for k, v in conn.items() if k != "data_b64"}))


class ArmGateResolverTests(TestCase):
    def test_resolver_none_when_arm_gate_off(self):
        with override_settings(HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED="0"):
            self.assertIsNone(GSC.resolve_establish_fn())

    def test_resolver_returns_driver_when_armed(self):
        with override_settings(HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED="1"):
            self.assertIs(GSC.resolve_establish_fn(), GSC.establish_session)


def _sframe(text):
    """A server->client guacamole WS text frame (server frames are NOT masked; payloads here are < 126 bytes)."""
    b = text.encode("utf-8")
    return bytes([0x81, len(b)]) + b


class _FakeSock:
    """A minimal server socket that computes the correct Sec-WebSocket-Accept from the client's key and then
    replays canned WS frames. Segmented delivery (header, then frames) so the client's header read never over-reads
    into the frames."""

    def __init__(self, *, accept_ok=True, frames=b""):
        self._sent = b""
        self._accept_ok = accept_ok
        self._frames = frames
        self._segments = None
        self._i = 0

    def settimeout(self, _t):
        pass

    def sendall(self, data):
        self._sent += data

    def _build(self):
        import base64 as _b64, hashlib as _h, re as _re
        m = _re.search(rb"Sec-WebSocket-Key: (.+?)\r\n", self._sent)
        key = m.group(1).decode("ascii") if m else "x"
        acc = _b64.b64encode(_h.sha1((key + GSC._GUAC_WS_GUID).encode("ascii")).digest()).decode("ascii")
        if not self._accept_ok:
            acc = "WRONG-ACCEPT-KEY"
        header = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode("ascii")
        self._segments = [bytearray(header)] + ([bytearray(self._frames)] if self._frames else [])

    def recv(self, n):
        if self._segments is None:
            self._build()
        while self._i < len(self._segments) and not self._segments[self._i]:
            self._i += 1
        if self._i >= len(self._segments):
            return b""
        seg = self._segments[self._i]
        out = bytes(seg[:n])
        del seg[:n]
        return out

    def close(self):
        pass


class StdlibTunnelConfirmTests(TestCase):
    """MED-3: pin the false-positive-critical confirm logic against canned bytes (no guacd). A False here is always
    the safe direction (reconciler retries + alerts); a wrong True would mask a real outage, so these matter most."""

    def _confirm(self, fake, timeout_s=1.0):
        tunnel = GSC._StdlibGuacTunnel(open_socket=lambda host, port, secure, ts: fake)
        return tunnel.connect_and_confirm(base_url="https://guac.invalid/guacamole", auth_token="t",
                                          conn_id="c", timeout_s=timeout_s)

    def test_accept_and_sync_confirms(self):
        self.assertTrue(self._confirm(_FakeSock(accept_ok=True, frames=_sframe("4.sync,8.31245867;"))))

    def test_accept_and_img_confirms(self):
        self.assertTrue(self._confirm(_FakeSock(accept_ok=True, frames=_sframe("3.img,1.0,4.blob;"))))

    def test_error_opcode_fails_closed(self):
        self.assertFalse(self._confirm(_FakeSock(accept_ok=True, frames=_sframe("5.error,3.foo,1.0;"))))

    def test_disconnect_opcode_fails_closed(self):
        self.assertFalse(self._confirm(_FakeSock(accept_ok=True, frames=_sframe("10.disconnect;"))))

    def test_wrong_accept_key_fails_closed(self):
        self.assertFalse(self._confirm(_FakeSock(accept_ok=False, frames=_sframe("4.sync;"))))

    def test_no_display_data_before_deadline_fails_closed(self):
        self.assertFalse(self._confirm(_FakeSock(accept_ok=True, frames=b""), timeout_s=0.3))


class StdlibTunnelFailClosedTests(TestCase):
    def test_unreachable_host_returns_false_never_raises(self):
        # The pure-stdlib tunnel must fail closed (False), never raise, when guacd is unreachable. (Real guac wire
        # behaviour is validated at the final Sponsor-authorised reboot; this only pins the fail-closed contract.)
        tunnel = GSC._StdlibGuacTunnel()
        ok = tunnel.connect_and_confirm(base_url="http://127.0.0.1:1", auth_token="t", conn_id="c", timeout_s=0.2)
        self.assertFalse(ok)


class P4cTunnelIdentifierRegressionTests(TestCase):
    """REGRESSION (post-reboot 2026-09-30 cold-boot cert failure): the recovery WS tunnel MUST send the RAW conn_id as
    GUAC_ID, never the base64 ``guac_client_identifier`` ClientIdentifier. Sending the ClientIdentifier (while
    GUAC_TYPE=c + GUAC_DATA_SOURCE=json are supplied separately) made prod Guacamole report
    "Requested tunnel destination does not exist" and every cold-boot self-connect failed tunnel_not_confirmed. The
    isolated 12/12 harness + prior unit tests never asserted the ON-WIRE GUAC_ID, so the defect shipped. These pin the
    wire value AND the forwarding so it cannot regress. (The human/browser delivery path is unchanged and untested
    here — it decodes its ClientIdentifier route client-side and its JS sends this same raw id.)"""

    def test_establish_sends_raw_conn_id_as_guac_id_on_the_wire(self):
        from urllib.parse import urlsplit, parse_qs
        from mt5.guac_json import guac_client_identifier
        fake = _FakeSock(accept_ok=True, frames=_sframe("4.sync,8.31245867;"))
        tunnel = GSC._StdlibGuacTunnel(open_socket=lambda host, port, secure, ts: fake)
        out = _run("ABSENT", tokens=_RecTokens(), tunnel=tunnel)
        self.assertEqual(out, {"ok": True, "reason": "established"})
        req_line = fake._sent.split(b"\r\n", 1)[0].decode("ascii", "replace")
        q = parse_qs(urlsplit(req_line.split(" ", 2)[1]).query)
        # GUAC_ID is the RAW conn id; type + datasource are separate params (guacd resolves the destination from all 3)
        self.assertEqual(q.get("GUAC_ID"), [_FAKE_CONN["conn_id"]])
        self.assertEqual(q.get("GUAC_TYPE"), ["c"])
        self.assertEqual(q.get("GUAC_DATA_SOURCE"), ["json"])
        # and NEVER the base64 ClientIdentifier (the exact prod defect that yielded "destination does not exist")
        self.assertNotEqual(q.get("GUAC_ID"), [guac_client_identifier(_FAKE_CONN["conn_id"], "json")])
        self.assertNotEqual(q.get("GUAC_ID"), [_FAKE_CONN["client_id"]])

    def test_establish_forwards_conn_id_not_client_identifier(self):
        tokens, tunnel = _RecTokens(), _RecTunnel(confirm=True)
        _run("ABSENT", tokens=tokens, tunnel=tunnel)
        self.assertEqual(len(tunnel.calls), 1)
        forwarded = tunnel.calls[0][2]                       # (base_url, auth_token, conn_id)
        self.assertEqual(forwarded, _FAKE_CONN["conn_id"])   # RAW stable per-workspace id
        self.assertNotEqual(forwarded, _FAKE_CONN["client_id"])  # NOT the base64 ClientIdentifier
