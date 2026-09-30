"""hosted_workspace.guac_selfconnect — P4-c system-initiated guacd self-connect driver (DARK; ARM-gated).

The ``establish_fn`` the P4 session reconciler calls (ONLY when
``hosted_session_reconciler_arm_selfconnect_enabled()`` is on) to re-establish or reconnect a tenant's Windows/MT5
RDS session after a cold boot. It:

  1. server-derives a SHORT-LIVED (~45 s) guacamole-auth-json token (delivery.build_recovery_connection) — the
     tenant's Windows password is decrypted into that token ONLY, never logged/returned/persisted (no new store);
  2. mints an authToken from guacd (POST /api/tokens);
  3. drives the guac tunnel just enough for guacd to open the RDP connection as guvfx_u_<id> — which creates the
     interactive session and auto-launches the /portable RemoteApp — and confirms it via streamed display data;
  4. ALWAYS revokes the authToken immediately afterwards (DELETE /api/tokens/<t>, in a finally); the short TTL is
     the backstop if revoke fails.

Safety / invariants:
  * ABSENT -> establish a fresh session; DISCONNECTED -> reconnect the SAME stable connection id (Windows
    single-session rejoins, never a 2nd session). The reconciler passes ``status``; the connection id is derived
    server-side from the workspace uuid (build_recovery_connection), never caller-supplied.
  * It NEVER logs in to the broker, arms execution, or places an order — it only causes the runtime/session to
    exist. TRADING still requires the independent observe->capability->auto-arm chain + freshness gate.
  * Customer Zero / account 18 are already excluded by the reconciler's candidate query AND the executor/.ps1;
    this driver additionally refuses a workspace whose delivery is not READY (workspace_delivery_ready).
  * Fail-closed: any missing config / mint / tunnel / verify failure returns {"ok": False, "reason": <code>} with
    a SANITISED reason (no password, token, path, or broker secret ever reaches a return value or a log line).
  * DARK: this module is imported + wired ONLY when the arm sub-gate is on; with the sub-gate off the reconciler
    passes establish_fn=None and this code never runs.

Dependency posture: the REST calls use ``requests`` (already a backend dependency); the tunnel uses ONLY the
standard library (no new WebSocket dependency, per architecture.md "no speculative infrastructure"). The tunnel's
real-guacd behaviour is validated at the final Sponsor-authorised reboot (the isolated 12/12 harness is the
approved feasibility proof); here it is exercised through an injected seam so the orchestration + revocation +
fail-closed contract are fully unit-tested without guacd.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import ssl
import struct
from urllib.parse import quote, urlsplit

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.guac_selfconnect"

# Bounds (conservative). The whole establish is a short operation; guacd should stream display data within a few
# seconds once the RDP connect succeeds.
_MINT_TIMEOUT_S = 10.0
_REVOKE_TIMEOUT_S = 10.0
_TUNNEL_TIMEOUT_S = 25.0
_RECOVERY_TOKEN_TTL_MS = 45_000        # ~45 s short-lived recovery token (NOT the 1 h member token)


# ── REST token client (requests-based; injectable) ───────────────────────────────────────────────────────────
class _RequestsTokenClient:
    """Mint + revoke a guacamole-auth-json authToken via the guac REST API. The ``data`` blob carries the sealed
    Windows password; it is POSTed as a form field and NEVER logged."""

    def mint(self, base_url: str, data_b64: str, *, timeout_s: float = _MINT_TIMEOUT_S) -> str:
        import requests
        r = requests.post(f"{base_url}/api/tokens", data={"data": data_b64}, timeout=timeout_s)
        r.raise_for_status()
        token = (r.json() or {}).get("authToken")
        if not token:
            raise RuntimeError("no_authtoken")
        return str(token)

    def revoke(self, base_url: str, auth_token: str, *, timeout_s: float = _REVOKE_TIMEOUT_S) -> bool:
        import requests
        try:
            resp = requests.delete(f"{base_url}/api/tokens/{quote(auth_token, safe='')}", timeout=timeout_s)
            return resp.status_code in (200, 204)
        except Exception:  # noqa: BLE001 — revoke is best-effort; the short TTL is the backstop
            return False


# ── Pure-stdlib guacamole WebSocket tunnel (no new dependency; injectable) ────────────────────────────────────
_GUAC_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # RFC 6455 accept-key magic


def _guac_instr(*parts) -> bytes:
    """Encode ONE guacamole protocol instruction: ``len.value,len.value,...;`` (lengths are Unicode code points)."""
    body = ",".join(f"{len(str(p))}.{p}" for p in parts) + ";"
    return body.encode("utf-8")


class _StdlibGuacTunnel:
    """Open the guac WebSocket tunnel, complete the handshake, and return True once guacd streams display data
    (``sync``/``img``/``blob``) — i.e. guacd connected the RDP host and the session/RemoteApp are up. Closing the
    socket leaves the Windows session DISCONNECTED, not logged off (harness-proven), so /portable keeps running.
    Pure stdlib; bounded by ``timeout_s``; fail-closed (returns False, never raises) on any error."""

    def __init__(self, *, open_socket=None):
        # ``open_socket(host, port, secure, timeout_s) -> socket-like`` is injectable so the confirm logic can be
        # unit-tested against canned bytes (no guacd). Default: real TCP/TLS.
        self._open_socket = open_socket

    def _connect(self, host, port, secure, timeout_s):
        if self._open_socket is not None:
            return self._open_socket(host, port, secure, timeout_s)
        raw = socket.create_connection((host, port), timeout=timeout_s)
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host) if secure else raw

    def connect_and_confirm(self, *, base_url: str, auth_token: str, conn_id: str,
                            timeout_s: float = _TUNNEL_TIMEOUT_S) -> bool:
        sock = None
        try:
            parts = urlsplit(base_url)
            secure = parts.scheme == "https"
            host = parts.hostname or ""
            port = parts.port or (443 if secure else 80)
            path = (parts.path or "").rstrip("/") + "/websocket-tunnel"
            # GUAC_ID MUST be the RAW connection id (the connection's identifier within the ``json`` data source),
            # NOT the base64 Guacamole ClientIdentifier. The websocket-tunnel endpoint resolves the destination from
            # (GUAC_ID, GUAC_TYPE, GUAC_DATA_SOURCE) supplied SEPARATELY here; passing the base64
            # ``guac_client_identifier(conn_id)`` form (the shape the browser uses in its ``#/client/<id>`` ROUTE)
            # double-encodes and makes guacd report "Requested tunnel destination does not exist". The human/browser
            # delivery path is UNCHANGED: the browser decodes its ClientIdentifier route and its JS sends this same
            # RAW id on the tunnel — so both paths converge on the same wire value here.
            query = (f"?token={quote(auth_token, safe='')}&GUAC_DATA_SOURCE=json"
                     f"&GUAC_ID={quote(conn_id, safe='')}&GUAC_TYPE=c"
                     "&GUAC_WIDTH=1024&GUAC_HEIGHT=768&GUAC_DPI=96")
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            sock = self._connect(host, port, secure, timeout_s)
            sock.settimeout(timeout_s)
            handshake = (
                f"GET {path}{query} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: guacamole\r\n\r\n"
            ).encode("ascii")
            sock.sendall(handshake)
            resp = self._read_until(sock, b"\r\n\r\n", timeout_s)
            if b" 101 " not in resp.split(b"\r\n", 1)[0]:
                return False
            accept = base64.b64encode(hashlib.sha1((key + _GUAC_WS_GUID).encode("ascii")).digest()).decode("ascii")
            if accept.encode("ascii") not in resp:
                return False
            # Guac handshake: guacd sends ``args``; reply size/audio/video/image then ``connect`` (empty args ->
            # use the connection's stored params). Then wait for the first display/sync = connection is up.
            self._send_text(sock, _guac_instr("size", "1024", "768", "96"))
            self._send_text(sock, _guac_instr("audio"))
            self._send_text(sock, _guac_instr("video"))
            self._send_text(sock, _guac_instr("image"))
            self._send_text(sock, _guac_instr("connect"))
            import time
            deadline = time.monotonic() + timeout_s
            buf = ""
            while time.monotonic() < deadline:
                chunk = self._recv_text(sock, timeout_s)
                if chunk is None:
                    break
                buf += chunk
                # A ``sync``/``img``/``blob`` opcode means guacd is streaming the live RDP display -> session up.
                for op in ("5.error", "10.disconnect"):
                    if op in buf:
                        return False
                if ("4.sync" in buf) or ("3.img" in buf) or ("4.blob" in buf):
                    return True
            return False
        except Exception:  # noqa: BLE001 — any tunnel error is fail-closed + sanitised
            return False
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:  # pragma: no cover
                pass

    # ---- minimal RFC 6455 client framing (text frames only; client frames MUST be masked) ----
    @staticmethod
    def _read_until(sock, marker: bytes, timeout_s: float) -> bytes:
        import time
        data = b""
        deadline = time.monotonic() + timeout_s
        while marker not in data and time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _send_text(sock, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        header = bytearray([0x81])                       # FIN + text opcode
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        sock.sendall(bytes(header) + mask + masked)

    @staticmethod
    def _recv_text(sock, timeout_s: float):
        try:
            first = sock.recv(2)
            if len(first) < 2:
                return None
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", sock.recv(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", sock.recv(8))[0]
            data = b""
            while len(data) < length:
                more = sock.recv(length - len(data))
                if not more:
                    break
                data += more
            return data.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None


# ── the establish_fn the reconciler calls ────────────────────────────────────────────────────────────────────
def establish_session(ws, account, node, status, *, token_client=None, tunnel_client=None) -> dict:
    """Establish (ABSENT) or reconnect (DISCONNECTED) the tenant's RDS session via a system-initiated guacd
    self-connect. Returns a sanitised ``{"ok": bool, "reason": <code>}``. Fail-closed on any missing config or
    error. Injectable ``token_client`` / ``tunnel_client`` seams make the orchestration testable without guacd."""
    from terminal_provisioning.models import AccountProvisioning
    from hosted_workspace.delivery import build_recovery_connection, workspace_delivery_ready

    # Server-derived deliverability gate (defence in depth over the reconciler): flags + node.rdp_host +
    # PROVISIONED non-admin identity + credential + GUAC configured. Never decrypt/mint if not deliverable.
    if not workspace_delivery_ready(ws):
        return {"ok": False, "reason": "delivery_not_ready"}
    # MED-2: the deliverability gate above validates ws.workspace_node's host; re-derive the MINT host from the SAME
    # FK (never trust the caller-passed node) so the host we authorise is provably the host we self-connect to. Fail
    # closed if the passed node diverges from ws.workspace_node.
    wsnode = getattr(ws, "workspace_node", None)
    if wsnode is None or (node is not None and getattr(node, "id", None) != getattr(wsnode, "id", None)):
        return {"ok": False, "reason": "node_divergence"}
    node = wsnode
    base_url = os.getenv("GUAC_BASE_URL", "").rstrip("/")
    secret_hex = os.getenv("GUAC_JSON_SECRET_KEY_HEX", "").strip()
    if not base_url or not secret_hex:
        return {"ok": False, "reason": "guac_unconfigured"}
    prov = AccountProvisioning.objects.filter(trading_account_id=account.id).first()
    if prov is None or not prov.password_enc or not prov.runtime_root:
        return {"ok": False, "reason": "identity_incomplete"}

    tokens = token_client or _RequestsTokenClient()
    tunnel = tunnel_client or _StdlibGuacTunnel()

    try:
        conn = build_recovery_connection(workspace=ws, prov=prov, node=node, base_url=base_url,
                                         secret_hex=secret_hex, ttl_ms=_RECOVERY_TOKEN_TTL_MS)
    except Exception:  # noqa: BLE001 — never leak a decrypt/crypto error detail (could echo config); sanitise
        logger.warning("guac_selfconnect: recovery-connection build failed acct %s", account.id)
        return {"ok": False, "reason": "recovery_build_failed"}

    auth_token = None
    try:
        try:
            auth_token = tokens.mint(base_url, conn["data_b64"])
        except Exception:  # noqa: BLE001 — sanitised; never log the data blob or token
            logger.warning("guac_selfconnect: token mint failed acct %s", account.id)
            return {"ok": False, "reason": "token_mint_failed"}
        try:
            # Pass the RAW stable per-workspace conn id as GUAC_ID (NOT conn["client_id"], the base64 ClientIdentifier
            # used only by the browser ROUTE). See _StdlibGuacTunnel.connect_and_confirm for why double-encoding here
            # yields guacd "Requested tunnel destination does not exist".
            up = tunnel.connect_and_confirm(base_url=base_url, auth_token=auth_token, conn_id=conn["conn_id"])
        except Exception:  # noqa: BLE001
            up = False
        if up:
            # ABSENT -> a fresh session was created; DISCONNECTED -> the same stable conn_id was rejoined.
            return {"ok": True, "reason": ("established" if status == "ABSENT" else "reconnected")}
        return {"ok": False, "reason": "tunnel_not_confirmed"}
    finally:
        # ALWAYS revoke the short-lived authToken immediately after establish (never leave it live).
        if auth_token:
            if not tokens.revoke(base_url, auth_token):
                logger.warning("guac_selfconnect: token revoke failed acct %s (short TTL is the backstop)", account.id)


def resolve_establish_fn():
    """Return the establish_fn for the reconciler, or None (probe-only). Non-None ONLY when the arm sub-gate is on
    — so the reconciler cannot self-connect / decrypt unless HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED."""
    from hosted_workspace.flags import hosted_session_reconciler_arm_selfconnect_enabled
    if not hosted_session_reconciler_arm_selfconnect_enabled():
        return None
    return establish_session
