import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import quote

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _key_bytes_from_hex(hex_key: str) -> bytes:
    hex_key = (hex_key or "").strip().lower()
    if len(hex_key) != 32:
        raise ValueError("GUAC_JSON_SECRET_KEY_HEX must be 32 hex chars (128-bit)")
    return bytes.fromhex(hex_key)


def sign_and_encrypt_json(payload: dict, *, secret_hex: str) -> str:
    """
    Encrypted JSON auth (guacamole-auth-json):
    - HMAC-SHA256 over JSON plaintext using secret key bytes
    - prepend signature bytes to plaintext bytes
    - AES-128-CBC encrypt with IV=all zero bytes
    - base64 encode ciphertext
    """
    key = _key_bytes_from_hex(secret_hex)

    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = hmac.new(key, plaintext, hashlib.sha256).digest()
    signed = sig + plaintext

    padder = padding.PKCS7(128).padder()
    padded = padder.update(signed) + padder.finalize()

    iv = b"\x00" * 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    ciphertext = enc.update(padded) + enc.finalize()

    return base64.b64encode(ciphertext).decode("ascii")


def guac_client_identifier(conn_id: str, datasource: str = "json") -> str:
    """
    Build a Guacamole client identifier for a deep-link into a specific
    connection (so the viewer opens the connection directly instead of the
    Guacamole home / connection-list page).

    Format (Guacamole 1.x ClientIdentifier): base64( <id> 0x00 'c' 0x00 <dataSource> ).
    For guacamole-auth-json the dataSource identifier is "json" and a
    connection's id equals its name in the JSON-auth payload.
    """
    raw = conn_id.encode("utf-8") + b"\x00" + b"c" + b"\x00" + datasource.encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def build_guac_data_url(
    *,
    base_url: str,
    data_b64: str,
    conn_id: str = "mt5-terminal",
    datasource: str = "json",
) -> str:
    """
    Deep-link directly into the MT5 connection's client view.

    PX-7B: the previous form ("#/client/c/mt5-terminal") was NOT a valid
    Guacamole client identifier, so Guacamole authenticated and then fell
    back to the home / connection-list page (Recent/All Connections UI
    leaked to traders). The correct form embeds the base64 client identifier
    so the iframe opens straight into the MT5 session.
    """
    base_url = base_url.rstrip("/")
    client_id = quote(guac_client_identifier(conn_id, datasource), safe="")
    data_q = quote(data_b64, safe="")
    return f"{base_url}/#/client/{client_id}?data={data_q}"


def build_dedicated_rdp_payload(
    *,
    username: str,
    windows_username: str,
    windows_password: str,
    host: str,
    port: str = "3389",
    conn_id: str = "mt5-terminal",
) -> dict:
    """
    TX-CUT1 — dedicated per-account RDP session as a NON-ADMIN kiosk identity.

    Connects Guacamole via RDP to ``windows_username`` (guvfx_u_<id>). That
    user's per-user kiosk shell (Winlogon\\Shell = the account's viewer MT5)
    launches MT5 instead of explorer, so the session exposes NO Administrator
    desktop / Start menu / Explorer. The Windows password is carried inside the
    AES-encrypted guacamole-auth-json token (same protection as the VNC password
    today). View-only (no broker login) is enforced by the runtime config.
    """
    expires_ms = int(time.time() * 1000) + 3_600_000
    unique_username = f"{username}-{uuid.uuid4().hex[:12]}"
    return {
        "username": unique_username,
        "expires": expires_ms,
        "connections": {
            conn_id: {
                "protocol": "rdp",
                "parameters": {
                    "hostname": host,
                    "port": str(port),
                    "username": windows_username,
                    "password": windows_password,
                    "security": "any",
                    "ignore-cert": "true",
                    "color-depth": "24",
                    "resize-method": "display-update",
                    "enable-drive": "false",
                    "enable-audio": "false",
                    "disable-copy": "true",
                    "disable-paste": "true",
                },
            }
        },
    }


def build_mt5_desktop_payload(*, username: str, host_override: str | None = None) -> dict:
    """
    Build a Guacamole JSON-auth payload for MT5 terminal access.

    Supports both VNC and RDP protocols via GUAC_MT5_PROTOCOL env var.
    Default: VNC (attaches to existing interactive desktop).
    """
    expires_ms = int(time.time() * 1000) + 3_600_000

    proto = os.getenv("GUAC_MT5_PROTOCOL", "vnc")
    host = (host_override or os.getenv("GUAC_MT5_HOST", "100.79.101.19"))
    port = os.getenv("GUAC_MT5_PORT", "5900")
    password = os.getenv("GUAC_MT5_PASS", "")

    if not password:
        raise RuntimeError("GUAC_MT5_PASS is not set")

    conn_id = "mt5-terminal"

    if proto == "vnc":
        parameters = {
            "hostname": host,
            "port": str(port),
            "password": password,
            "color-depth": "24",
            "cursor": "local",
            "swap-red-blue": "false",
            "read-only": "false",
        }
    else:
        # RDP fallback
        rdp_user = os.getenv("GUAC_MT5_USER", "Administrator")
        parameters = {
            "hostname": host,
            "port": str(port),
            "username": rdp_user,
            "password": password,
            "ignore-cert": "true",
            "security": "any",
            "color-depth": "24",
            "dpi": "96",
            "width": "1920",
            "height": "1080",
            "disable-copy": "true",
            "disable-paste": "true",
            "enable-font-smoothing": "false",
            "enable-wallpaper": "false",
            "enable-themes": "false",
            "enable-printing": "false",
            "enable-drive": "false",
            "enable-audio": "false",
        }

    # PX-7B: make the JSON-auth username unique per launch. The Guacamole
    # webapp re-uses an existing stored token when the re-authenticated user
    # matches (server returns the same authToken), silently ignoring the fresh
    # connection payload → "The requested connection does not exist" on
    # reload/reconnect. A unique username forces the server to mint a NEW token
    # (authToken changes), so the webapp revokes the stale session and adopts
    # the fresh one that actually carries the mt5-terminal connection.
    unique_username = f"{username}-{uuid.uuid4().hex[:12]}"

    return {
        "username": unique_username,
        "expires": expires_ms,
        "connections": {
            conn_id: {
                "protocol": proto,
                "parameters": parameters,
            }
        },
    }
