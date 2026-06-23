"""Read-only history-export transport boundary (GFX-PKT-006C).

A transport is injected into the orchestrator. Every test/smoke uses the synthetic
fixture transport — no network occurs. The network-capable client is inert unless
explicitly constructed with network permission, base URL and token; it never reads
a token at import and never exposes the token or response body in repr/exceptions.
"""

from __future__ import annotations

import contextlib
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

# Future agent endpoint (NOT called in this packet).
HISTORY_EXPORT_PATH = "/mt5/history/rates/export"
AGENT_TOKEN_HEADER = "X-GuvFX-Agent-Token"
DEFAULT_TIMEOUT_S = 30
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class TransportError(RuntimeError):
    pass


class Transport(Protocol):
    def export_rates(self, request_bytes: bytes) -> bytes:  # pragma: no cover - protocol
        ...


class FixtureTransport:
    """Synthetic transport: returns canned response bytes; performs no I/O."""

    def __init__(self, response_bytes: bytes):
        if not isinstance(response_bytes, (bytes, bytearray)):
            raise TransportError("FixtureTransport requires response bytes")
        self._response_bytes = bytes(response_bytes)
        self.calls = 0

    def export_rates(self, request_bytes: bytes) -> bytes:
        self.calls += 1
        return self._response_bytes


class NetworkAgentClient:
    """Standard-library HTTP client. Inert unless allow_network=True is passed.

    Constructed only by a FUTURE authorised packet. Never used against a real
    endpoint in tests — tests inject a fake opener and run inside the egress guard.
    The token and any response body never appear in repr or exceptions.
    """

    def __init__(self, base_url: str, token: str, *, allow_network: bool = False,
                 timeout_s: int = DEFAULT_TIMEOUT_S,
                 max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
                 opener=None):
        if not base_url or not isinstance(base_url, str):
            raise TransportError("base_url is required")
        if not token or not isinstance(token, str):
            raise TransportError("token is required")
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise TransportError("base_url must be an http(s) URL")
        if parts.query or parts.fragment:
            raise TransportError("base_url must not contain a query or fragment")
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise TransportError("timeout_s must be a positive number")
        self._base_url = base_url.rstrip("/")
        self._token = token  # never logged or repr'd
        self._allow_network = bool(allow_network)
        self._timeout_s = timeout_s
        self._max_response_bytes = int(max_response_bytes)
        # Injectable opener (tests pass a fake); defaults to the stdlib opener.
        self._opener = opener or urllib.request.urlopen

    def __repr__(self) -> str:  # token and body never appear
        return (
            f"NetworkAgentClient(base_url={self._base_url!r}, "
            f"allow_network={self._allow_network}, token=<redacted>)"
        )

    @property
    def url(self) -> str:
        return f"{self._base_url}{HISTORY_EXPORT_PATH}"

    def export_rates(self, request_bytes: bytes) -> bytes:
        # Fail before constructing/opening any request when disabled.
        if not self._allow_network:
            raise TransportError(
                "network disabled: NetworkAgentClient requires allow_network=True "
                "from a future authorised packet"
            )
        if not isinstance(request_bytes, (bytes, bytearray)):
            raise TransportError("request_bytes must be bytes")

        request = urllib.request.Request(
            self.url,
            data=bytes(request_bytes),
            method="POST",
            headers={
                AGENT_TOKEN_HEADER: self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            # One attempt, no retry, explicit timeout.
            response = self._opener(request, timeout=self._timeout_s)
        except urllib.error.HTTPError as exc:
            # Do NOT read the error body; close it to release resources.
            status = getattr(exc, "code", "unknown")
            try:
                exc.close()
            except Exception:
                pass
            raise TransportError(f"agent returned non-success HTTP status {status}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Redacted: no token, request body or response body in the message.
            raise TransportError("history export transport error") from None

        try:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status != 200:
                raise TransportError(f"agent returned non-success HTTP status {status}")
            # Read at most max+1 to detect oversize without retaining a large body.
            body = response.read(self._max_response_bytes + 1)
            if len(body) > self._max_response_bytes:
                raise TransportError("agent response exceeded the byte limit")
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        return bytes(body)


@contextlib.contextmanager
def network_blocked():
    """Block all outbound socket connections within the context (egress guard).

    Tests and the smoke install this so any attempted network call fails loudly.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def _blocked(*args, **kwargs):
        raise RuntimeError("network egress is blocked (synthetic guard)")

    socket.socket.connect = _blocked  # type: ignore[assignment]
    socket.socket.connect_ex = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[assignment]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[assignment]
        socket.create_connection = original_create  # type: ignore[assignment]
