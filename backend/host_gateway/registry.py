"""host_gateway.registry — the gateway's LOCAL registry of per-account bridge instances (ADR-0075).

Stdlib-only. The registry is the ONLY source of routing targets: the gateway routes a request to a local
bridge port looked up by registered account identity, never to any host:port taken from the request. A
registration carries the occupancy generation and a lifecycle state so a stale/tombstoned/draining entry
is refused (fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RegisteredBridge:
    """One registered local bridge behind the gateway. ``local_port`` is a host-LOOPBACK port (never
    Tailnet-exposed). ``state`` must be SERVING for the gateway to route to it."""
    account_id: int
    workspace_uuid: str
    local_port: int
    generation: int = 1
    verified_login: str = ""
    verified_server: str = ""
    verified_is_demo: bool | None = None
    capabilities: tuple[str, ...] = ()
    state: str = "SERVING"          # SERVING | DRAINING | STALE | TOMBSTONED

    def is_serving(self) -> bool:
        return self.state == "SERVING"


class BridgeRegistry:
    """In-memory registry keyed by account id. On a real host this would be persisted and reloaded on
    boot; for the repository component (and CI) it is an in-process map. Deliberately tiny and pure."""

    def __init__(self):
        self._by_account: dict[int, RegisteredBridge] = {}

    def register(self, bridge: RegisteredBridge) -> None:
        self._by_account[int(bridge.account_id)] = bridge

    def deregister(self, account_id: int) -> None:
        self._by_account.pop(int(account_id), None)

    def mark(self, account_id: int, state: str) -> None:
        b = self._by_account.get(int(account_id))
        if b is not None:
            b.state = state

    def get(self, account_id: int) -> RegisteredBridge | None:
        return self._by_account.get(int(account_id))

    def __len__(self) -> int:
        return len(self._by_account)
