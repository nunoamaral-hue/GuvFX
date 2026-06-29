"""
SessionAdapter — abstract interface / contract.

All concrete adapters (Guacamole+VNC, future alternatives) must
implement this interface.  Domain services call ONLY these methods.

Contract:
    launch()     — initiate a new adapter session
    resume()     — reconnect to an existing adapter session
    terminate()  — tear down an adapter session
    get_status() — query current adapter-level session status

All methods accept and return normalized, adapter-safe types
(AdapterLaunchRequest / AdapterResult / AdapterStatus).
No Guacamole-specific raw payloads cross this boundary.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# =========================================================================
# Contract data types
# =========================================================================


@dataclass(frozen=True)
class AdapterLaunchRequest:
    """
    Normalized launch request passed INTO the adapter.

    Contains only domain-safe, backend-only inputs.
    No raw credentials — the adapter resolves those internally.
    """

    terminal_binding_id: int
    terminal_node_id: int
    terminal_identifier: str
    mt5_account_login: str
    environment_type: str
    interaction_session_id: int
    mt5_session_id: int


@dataclass(frozen=True)
class AdapterResumeRequest:
    """
    Normalized resume request passed INTO the adapter.

    Carries the prior adapter_session_id so the concrete adapter
    can attempt reconnection.
    """

    terminal_binding_id: int
    terminal_node_id: int
    terminal_identifier: str
    mt5_account_login: str
    interaction_session_id: int
    mt5_session_id: int
    prior_adapter_session_id: str
    prior_adapter_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterTerminateRequest:
    """
    Normalized terminate request passed INTO the adapter.
    """

    adapter_session_id: str
    terminal_binding_id: int
    reason: str = ""


@dataclass
class AdapterResult:
    """
    Normalized result returned FROM the adapter.

    Every adapter operation returns one of these.  The ``success``
    flag and ``mapped_state`` are the only fields that domain
    services should branch on.
    """

    success: bool
    mapped_state: str  # one of the Phase 2 MT5Session.state strings
    adapter_session_id: str = ""
    adapter_type: str = ""
    error_message: str = ""
    adapter_metadata: dict = field(default_factory=dict)
    # Structured launch descriptor safe for persistence
    launch_descriptor: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterStatus:
    """
    Normalized adapter-level session status.

    Returned by get_status().  ``mapped_state`` is the Phase 2
    MT5Session.state string equivalent.
    """

    mapped_state: str
    adapter_raw_status: str = ""  # internal only, not consumed by services
    is_connected: bool = False
    error_message: str = ""


# =========================================================================
# Abstract interface
# =========================================================================


class SessionAdapter(abc.ABC):
    """
    Abstract session adapter interface.

    Concrete implementations (GuacamoleVncAdapter, etc.) must
    implement all four methods.
    """

    @abc.abstractmethod
    def launch(self, request: AdapterLaunchRequest) -> AdapterResult:
        """
        Initiate a new adapter session.

        Returns AdapterResult with:
        - success=True, mapped_state="launching" or "connected"
        - success=False, mapped_state="failed", error_message set
        """
        ...

    @abc.abstractmethod
    def resume(self, request: AdapterResumeRequest) -> AdapterResult:
        """
        Reconnect to an existing adapter session.

        Returns AdapterResult with:
        - success=True, mapped_state="connected" (reconnected)
        - success=False, mapped_state="failed", error_message set
        """
        ...

    @abc.abstractmethod
    def terminate(self, request: AdapterTerminateRequest) -> AdapterResult:
        """
        Tear down an adapter session.

        Returns AdapterResult with:
        - success=True, mapped_state="ended"
        - success=False, mapped_state="ended" (best-effort), error_message set
        """
        ...

    @abc.abstractmethod
    def get_status(self, adapter_session_id: str) -> AdapterStatus:
        """
        Query the current adapter-level status of a session.

        Returns AdapterStatus with mapped_state reflecting the
        current state of the underlying connection.
        """
        ...
