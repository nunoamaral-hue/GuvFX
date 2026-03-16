"""
Launch Descriptor Generation.

Produces a structured, backend-only launch descriptor suitable
for persistence in MT5Session.launch_descriptor_snapshot.

The descriptor contains ONLY:
- routing/identification fields (binding, node, terminal IDs)
- adapter type and connection mode
- generation timestamp
- optional resume hint

It NEVER contains:
- passwords or credentials
- auth tokens
- raw Guacamole API responses
- any field that would leak secret material if persisted
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def build_launch_descriptor(
    adapter_type: str,
    connection_mode: str,
    terminal_binding_id: int,
    terminal_node_id: int,
    terminal_identifier: str,
    mt5_account_login: str,
    guacamole_connection_id: str = "",
    adapter_session_id: str = "",
    resume_hint: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a launch descriptor dictionary.

    All fields are backend-safe and secret-free.  This dict is
    intended for MT5Session.launch_descriptor_snapshot.

    Args:
        adapter_type: Concrete adapter identifier (e.g. "guacamole_vnc").
        connection_mode: Connection protocol mode (e.g. "guacamole_vnc").
        terminal_binding_id: TerminalBinding PK.
        terminal_node_id: TerminalNode PK.
        terminal_identifier: Slot identifier on the node.
        mt5_account_login: MT5 account login number.
        guacamole_connection_id: Guacamole connection ID (string, no auth).
        adapter_session_id: Adapter-assigned session ID.
        resume_hint: Hint for resume path (e.g. "reconnect_same_connection").
        extra: Additional safe metadata (no credentials).

    Returns:
        Serializable dict safe for JSON persistence.
    """
    descriptor: dict[str, Any] = {
        "adapter_type": adapter_type,
        "connection_mode": connection_mode,
        "terminal_binding_id": terminal_binding_id,
        "terminal_node_id": terminal_node_id,
        "terminal_identifier": terminal_identifier,
        "mt5_account_login": mt5_account_login,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if guacamole_connection_id:
        descriptor["guacamole_connection_id"] = guacamole_connection_id

    if adapter_session_id:
        descriptor["adapter_session_id"] = adapter_session_id

    if resume_hint:
        descriptor["resume_hint"] = resume_hint

    if extra:
        # Defensive: strip any key that looks like a credential
        safe_extra = _strip_sensitive_keys(extra)
        descriptor.update(safe_extra)

    return descriptor


_SENSITIVE_SUBSTRINGS = frozenset({
    "password", "secret", "token", "credential",
    "api_key", "apikey", "private", "auth",
})


def _strip_sensitive_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Remove any keys whose name contains a sensitive substring."""
    safe = {}
    for key, value in data.items():
        key_lower = key.lower()
        if any(s in key_lower for s in _SENSITIVE_SUBSTRINGS):
            continue  # silently drop
        if isinstance(value, dict):
            safe[key] = _strip_sensitive_keys(value)
        else:
            safe[key] = value
    return safe
