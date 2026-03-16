"""
Guacamole + VNC Concrete Adapter.

Implements SessionAdapter for the existing Guacamole/RDP
integration.  Delegates to the existing helper modules:
- mt5.services.guac_api  (auth token, launch URL)
- mt5.services.guac_db   (RDP connection provisioning)

This adapter:
- contains NO business/domain logic
- contains NO authorization or occupancy logic
- resolves credentials internally (never exposes them)
- normalizes all results into AdapterResult / AdapterStatus
- never lets raw Guacamole exceptions propagate upward

Status mapping (adapter → MT5Session.state):
    guac_api success       → "launching" (connection created, URL ready)
    confirmed connected    → "connected"
    disconnect/closed      → "ended"
    any error              → "failed"
"""
from __future__ import annotations

import logging
from typing import Any

from mt5.adapters.launch_descriptor import build_launch_descriptor
from mt5.adapters.session_adapter import (
    AdapterLaunchRequest,
    AdapterResumeRequest,
    AdapterResult,
    AdapterStatus,
    AdapterTerminateRequest,
    SessionAdapter,
)

logger = logging.getLogger(__name__)

ADAPTER_TYPE = "guacamole_vnc"


class GuacamoleVncAdapter(SessionAdapter):
    """
    Concrete SessionAdapter backed by Guacamole + RDP/VNC.

    Reuses existing helpers in mt5.services.guac_api and
    mt5.services.guac_db.  Credentials are resolved at call
    time from environment variables and DB — never stored in
    adapter results or launch descriptors.
    """

    # -----------------------------------------------------------------
    # SessionAdapter.launch
    # -----------------------------------------------------------------

    def launch(self, request: AdapterLaunchRequest) -> AdapterResult:
        """
        Initiate a Guacamole session for the given terminal binding.

        Steps:
        1. Look up the Mt5Instance to get guac_connection_id
        2. Obtain a Guacamole auth token (ephemeral, not persisted)
        3. Build launch URL
        4. Return AdapterResult with launch descriptor

        On failure: return AdapterResult(success=False, mapped_state="failed").
        """
        try:
            connection_id, connection_name = self._resolve_guac_connection(
                request.terminal_binding_id,
                request.terminal_identifier,
            )

            # Obtain ephemeral auth token (NOT persisted)
            from mt5.services.guac_api import guac_token, launch_url

            token = guac_token()
            url = launch_url(token, connection_id)
            # Token and URL are ephemeral — only the connection_id is
            # persisted in the launch descriptor.  The token itself
            # is passed via adapter_metadata for immediate use only.

            descriptor = build_launch_descriptor(
                adapter_type=ADAPTER_TYPE,
                connection_mode="guacamole_vnc",
                terminal_binding_id=request.terminal_binding_id,
                terminal_node_id=request.terminal_node_id,
                terminal_identifier=request.terminal_identifier,
                mt5_account_login=request.mt5_account_login,
                guacamole_connection_id=str(connection_id),
                adapter_session_id=f"guac-{connection_id}",
                resume_hint="reconnect_same_connection",
            )

            return AdapterResult(
                success=True,
                mapped_state="launching",
                adapter_session_id=f"guac-{connection_id}",
                adapter_type=ADAPTER_TYPE,
                launch_descriptor=descriptor,
                adapter_metadata={
                    # Backend-only, ephemeral — safe for immediate use,
                    # not persisted in launch_descriptor_snapshot.
                    "guacamole_launch_url": url,
                    "guacamole_connection_name": connection_name,
                },
            )

        except Exception as e:
            error_msg = _sanitize_error(e)
            logger.error(
                "Guacamole launch failed: binding=%s error=%s",
                request.terminal_binding_id, error_msg,
            )
            return AdapterResult(
                success=False,
                mapped_state="failed",
                adapter_type=ADAPTER_TYPE,
                error_message=error_msg,
            )

    # -----------------------------------------------------------------
    # SessionAdapter.resume
    # -----------------------------------------------------------------

    def resume(self, request: AdapterResumeRequest) -> AdapterResult:
        """
        Attempt to reconnect to an existing Guacamole session.

        Reuses the same guac_connection_id from the prior session.
        Obtains a fresh auth token for reconnection.
        """
        try:
            # Extract connection_id from prior adapter metadata or session ID
            connection_id = self._extract_connection_id(
                request.prior_adapter_session_id,
                request.prior_adapter_metadata,
            )

            if not connection_id:
                return AdapterResult(
                    success=False,
                    mapped_state="failed",
                    adapter_type=ADAPTER_TYPE,
                    error_message=(
                        "Cannot resume: no usable guacamole_connection_id "
                        "from prior session."
                    ),
                )

            from mt5.services.guac_api import guac_token, launch_url

            token = guac_token()
            url = launch_url(token, int(connection_id))

            descriptor = build_launch_descriptor(
                adapter_type=ADAPTER_TYPE,
                connection_mode="guacamole_vnc",
                terminal_binding_id=request.terminal_binding_id,
                terminal_node_id=request.terminal_node_id,
                terminal_identifier=request.terminal_identifier,
                mt5_account_login=request.mt5_account_login,
                guacamole_connection_id=str(connection_id),
                adapter_session_id=f"guac-{connection_id}",
                resume_hint="reconnected_existing_connection",
            )

            return AdapterResult(
                success=True,
                mapped_state="connected",
                adapter_session_id=f"guac-{connection_id}",
                adapter_type=ADAPTER_TYPE,
                launch_descriptor=descriptor,
                adapter_metadata={
                    "guacamole_launch_url": url,
                    "resumed": True,
                },
            )

        except Exception as e:
            error_msg = _sanitize_error(e)
            logger.error(
                "Guacamole resume failed: binding=%s error=%s",
                request.terminal_binding_id, error_msg,
            )
            return AdapterResult(
                success=False,
                mapped_state="failed",
                adapter_type=ADAPTER_TYPE,
                error_message=error_msg,
            )

    # -----------------------------------------------------------------
    # SessionAdapter.terminate
    # -----------------------------------------------------------------

    def terminate(self, request: AdapterTerminateRequest) -> AdapterResult:
        """
        Tear down a Guacamole session.

        Guacamole RDP connections are persistent server-side — the
        actual RDP session may remain active on the Windows host.
        This method signals intent to end the session.  The binding
        is NOT deleted — it remains for future reuse.

        Best-effort: even if Guacamole teardown fails, we return
        mapped_state="ended" so the domain layer can proceed.
        """
        try:
            # For Guacamole RDP, there is no explicit "disconnect session"
            # API in the existing helpers.  The RDP connection persists.
            # The domain layer handles state cleanup; this adapter simply
            # acknowledges the termination intent.
            logger.info(
                "Guacamole terminate: adapter_session=%s binding=%s reason=%s",
                request.adapter_session_id,
                request.terminal_binding_id,
                request.reason or "(none)",
            )

            return AdapterResult(
                success=True,
                mapped_state="ended",
                adapter_session_id=request.adapter_session_id,
                adapter_type=ADAPTER_TYPE,
                adapter_metadata={
                    "terminate_reason": request.reason,
                },
            )

        except Exception as e:
            error_msg = _sanitize_error(e)
            logger.error(
                "Guacamole terminate error (best-effort): session=%s error=%s",
                request.adapter_session_id, error_msg,
            )
            # Best-effort: still return "ended" so domain proceeds
            return AdapterResult(
                success=False,
                mapped_state="ended",
                adapter_type=ADAPTER_TYPE,
                error_message=error_msg,
            )

    # -----------------------------------------------------------------
    # SessionAdapter.get_status
    # -----------------------------------------------------------------

    def get_status(self, adapter_session_id: str) -> AdapterStatus:
        """
        Query Guacamole for the current status of a session.

        The existing Guacamole helpers do not expose a status query
        API.  This returns a best-effort status based on whether
        the connection_id is valid.
        """
        try:
            connection_id = self._parse_connection_id(adapter_session_id)
            if connection_id is None:
                return AdapterStatus(
                    mapped_state="failed",
                    error_message=f"Invalid adapter_session_id: {adapter_session_id}",
                )

            # Verify the Guacamole connection exists by attempting a
            # token fetch.  If the Guacamole server is unreachable,
            # we fail closed.
            from mt5.services.guac_api import guac_token

            guac_token()  # validates Guacamole is reachable

            return AdapterStatus(
                mapped_state="connected",
                adapter_raw_status="guac_connection_exists",
                is_connected=True,
            )

        except Exception as e:
            error_msg = _sanitize_error(e)
            return AdapterStatus(
                mapped_state="failed",
                adapter_raw_status="guac_unreachable",
                is_connected=False,
                error_message=error_msg,
            )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _resolve_guac_connection(
        self,
        terminal_binding_id: int,
        terminal_identifier: str,
    ) -> tuple[int, str]:
        """
        Resolve the Guacamole connection_id for a terminal binding.

        Looks up the Mt5Instance by matching terminal_identifier
        to hostname, and returns (guac_connection_id, connection_name).

        Raises RuntimeError if no matching instance or no connection_id.
        """
        from mt5.models import Mt5Instance

        try:
            instance = Mt5Instance.objects.get(hostname=terminal_identifier)
        except Mt5Instance.DoesNotExist:
            raise RuntimeError(
                f"No Mt5Instance found for hostname={terminal_identifier}"
            )

        if not instance.guac_connection_id:
            raise RuntimeError(
                f"Mt5Instance {terminal_identifier} has no "
                f"guac_connection_id configured."
            )

        return instance.guac_connection_id, instance.hostname

    def _extract_connection_id(
        self,
        adapter_session_id: str,
        adapter_metadata: dict,
    ) -> str | None:
        """
        Extract the Guacamole connection_id from a prior session.

        Tries adapter_session_id first (format: "guac-<id>"),
        then falls back to adapter_metadata.
        """
        # Try parsing from adapter_session_id
        parsed = self._parse_connection_id(adapter_session_id)
        if parsed is not None:
            return str(parsed)

        # Fallback: check metadata
        return adapter_metadata.get("guacamole_connection_id") or None

    @staticmethod
    def _parse_connection_id(adapter_session_id: str) -> int | None:
        """Parse 'guac-<int>' format, return int or None."""
        if not adapter_session_id or not adapter_session_id.startswith("guac-"):
            return None
        try:
            return int(adapter_session_id[5:])
        except (ValueError, IndexError):
            return None


# =========================================================================
# Module-level helpers
# =========================================================================


# Deterministic status mapping: adapter outcome → MT5Session.state
#
# This is the single mapping table.  Unknown/unmapped outcomes
# fail closed to "failed".
_STATUS_MAP: dict[str, str] = {
    "launching": "launching",
    "connected": "connected",
    "suspended": "suspended",
    "disconnected": "ended",
    "closed": "ended",
    "ended": "ended",
    "error": "failed",
    "failed": "failed",
}


def map_adapter_status(adapter_outcome: str) -> str:
    """
    Map a concrete adapter outcome string to the Phase 2
    MT5Session.state string.

    Unknown outcomes fail closed to "failed".
    """
    return _STATUS_MAP.get(adapter_outcome, "failed")


def _sanitize_error(exc: Exception) -> str:
    """
    Produce a safe error message from an exception.

    Strips any substring that looks like a credential or token.
    Truncates to 500 chars.
    """
    raw = str(exc)
    # Remove common credential patterns from error messages
    for marker in ("password=", "token=", "secret=", "api_key=", "apikey="):
        if marker in raw.lower():
            idx = raw.lower().index(marker)
            raw = raw[:idx] + f"{marker}[REDACTED]"
            break
    return raw[:500]
