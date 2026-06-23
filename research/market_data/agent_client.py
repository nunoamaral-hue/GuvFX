"""Read-only history-export transport boundary (GFX-PKT-006C).

A transport is injected into the orchestrator. Every test/smoke uses the synthetic
fixture transport — no network occurs. The network-capable client is inert unless
explicitly constructed with network permission, base URL and token; it never reads
a token at import and never exposes the token or response body in repr/exceptions.
"""

from __future__ import annotations

import contextlib
import socket
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
    """Network-capable client. Inert unless allow_network=True is passed.

    Constructed lazily by a FUTURE authorised packet only; never used in tests.
    """

    def __init__(self, base_url: str, token: str, *, allow_network: bool = False,
                 timeout_s: int = DEFAULT_TIMEOUT_S,
                 max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES):
        if not base_url:
            raise TransportError("base_url is required")
        self._base_url = base_url.rstrip("/")
        self._token = token  # never logged or repr'd
        self._allow_network = bool(allow_network)
        self._timeout_s = timeout_s
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:  # token and body never appear
        return (
            f"NetworkAgentClient(base_url={self._base_url!r}, "
            f"allow_network={self._allow_network}, token=<redacted>)"
        )

    def export_rates(self, request_bytes: bytes) -> bytes:
        if not self._allow_network:
            raise TransportError(
                "network disabled: NetworkAgentClient requires allow_network=True "
                "from a future authorised packet"
            )
        # Real call deliberately left to a future authorised packet. No automatic
        # retries; explicit timeout and byte cap would be enforced here.
        raise TransportError(
            "live history export is not authorised under GFX-PKT-006C"
        )


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
