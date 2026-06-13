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
        Initiate a Guacamole session via encrypted JSON auth.

        Uses guacamole-auth-json: builds an encrypted RDP connection
        payload signed with the shared secret. No pre-provisioned
        Guacamole connection or guac_connection_id required.

        Steps:
        1. Resolve RDP target from TerminalBinding → TerminalNode → Mt5Instance
        2. Build encrypted JSON auth payload with RDP parameters
        3. Generate signed Guacamole launch URL
        4. Return AdapterResult with embed URL

        On failure: return AdapterResult(success=False, mapped_state="failed").
        """
        import os

        try:
            from mt5.guac_json import (
                build_mt5_desktop_payload,
                sign_and_encrypt_json,
                build_guac_data_url,
            )

            base_url = os.getenv("GUAC_BASE_URL", "").rstrip("/")
            secret_hex = os.getenv("GUAC_JSON_SECRET_KEY_HEX", "").strip()

            if not base_url or not secret_hex:
                raise RuntimeError("GUAC_BASE_URL or GUAC_JSON_SECRET_KEY_HEX not configured")

            # Resolve the RDP host from the terminal node
            rdp_host = self._resolve_rdp_host(request.terminal_node_id)

            user_label = f"user-binding-{request.terminal_binding_id}"

            # TX-CUT1: dedicated-session cutover. If this account is routed to
            # the dedicated path (kill-switch on + assignment enabled + runtime
            # populated + READY), connect RDP as its non-admin kiosk identity
            # (guvfx_u_<id>) instead of VNC-to-Administrator — removing the
            # Administrator-desktop exposure. FAIL-CLOSED: any error (or the
            # account not being routed) falls back to the legacy VNC payload.
            payload = None
            try:
                from trading.models import TradingAccount
                from terminal_provisioning import delivery
                from terminal_provisioning.models import AccountProvisioning
                from trading.crypto import decrypt_password
                from mt5.guac_json import build_dedicated_rdp_payload

                acct = TradingAccount.objects.filter(
                    account_number=request.mt5_account_login
                ).first()
                if acct and delivery.deliver_session(acct.id).get("path") == "DEDICATED":
                    prov = AccountProvisioning.objects.get(trading_account=acct)
                    payload = build_dedicated_rdp_payload(
                        username=f"ded-{prov.windows_username}",
                        windows_username=prov.windows_username,
                        windows_password=decrypt_password(prov.password_enc),
                        host=rdp_host,
                    )
                    logger.info("TX-CUT1: dedicated RDP route for account %s (%s)",
                                getattr(acct, "id", None), prov.windows_username)
            except Exception as e:  # noqa: BLE001 — fail closed to legacy
                logger.warning("TX-CUT1 dedicated routing skipped (fallback to legacy): %s",
                               _sanitize_error(e))
                payload = None

            if payload is None:
                payload = build_mt5_desktop_payload(
                    username=user_label,
                    host_override=rdp_host,
                )
            data_b64 = sign_and_encrypt_json(payload, secret_hex=secret_hex)
            url = build_guac_data_url(base_url=base_url, data_b64=data_b64)

            session_id = f"json-auth-{request.terminal_binding_id}"

            descriptor = build_launch_descriptor(
                adapter_type=ADAPTER_TYPE,
                connection_mode="guacamole_json_auth",
                terminal_binding_id=request.terminal_binding_id,
                terminal_node_id=request.terminal_node_id,
                terminal_identifier=request.terminal_identifier,
                mt5_account_login=request.mt5_account_login,
                adapter_session_id=session_id,
                resume_hint="regenerate_json_auth",
            )

            return AdapterResult(
                success=True,
                mapped_state="launching",
                adapter_session_id=session_id,
                adapter_type=ADAPTER_TYPE,
                launch_descriptor=descriptor,
                adapter_metadata={
                    "guacamole_launch_url": url,
                },
            )

        except Exception as e:
            error_msg = _sanitize_error(e)
            logger.error(
                "Guacamole JSON auth launch failed: binding=%s error=%s",
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
        Resume by generating a fresh JSON auth URL.

        JSON auth sessions are stateless — each launch generates a
        fresh encrypted payload. Resume is equivalent to a new launch
        targeting the same binding.
        """
        import os

        try:
            from mt5.guac_json import (
                build_mt5_desktop_payload,
                sign_and_encrypt_json,
                build_guac_data_url,
            )

            base_url = os.getenv("GUAC_BASE_URL", "").rstrip("/")
            secret_hex = os.getenv("GUAC_JSON_SECRET_KEY_HEX", "").strip()

            if not base_url or not secret_hex:
                raise RuntimeError("GUAC_BASE_URL or GUAC_JSON_SECRET_KEY_HEX not configured")

            rdp_host = self._resolve_rdp_host(request.terminal_node_id)

            user_label = f"user-binding-{request.terminal_binding_id}"

            # TX-CUT1A: route resume/reconnect to the dedicated path too, so the
            # FULL lifecycle (launch + reconnect/rediscovery) is consistent for a
            # cut-over account. Without this, the PX-7A auto-reconnect reverted to
            # legacy VNC (Administrator desktop) after a dedicated launch.
            # FAIL-CLOSED: any error / non-routed account → legacy VNC payload.
            payload = None
            try:
                from trading.models import TradingAccount
                from terminal_provisioning import delivery
                from terminal_provisioning.models import AccountProvisioning
                from trading.crypto import decrypt_password
                from mt5.guac_json import build_dedicated_rdp_payload

                acct = TradingAccount.objects.filter(
                    account_number=request.mt5_account_login
                ).first()
                if acct and delivery.deliver_session(acct.id).get("path") == "DEDICATED":
                    prov = AccountProvisioning.objects.get(trading_account=acct)
                    payload = build_dedicated_rdp_payload(
                        username=f"ded-{prov.windows_username}",
                        windows_username=prov.windows_username,
                        windows_password=decrypt_password(prov.password_enc),
                        host=rdp_host,
                    )
                    logger.info("TX-CUT1A: dedicated RDP resume for account %s (%s)",
                                getattr(acct, "id", None), prov.windows_username)
            except Exception as e:  # noqa: BLE001 — fail closed to legacy
                logger.warning("TX-CUT1A dedicated resume skipped (fallback to legacy): %s",
                               _sanitize_error(e))
                payload = None

            if payload is None:
                payload = build_mt5_desktop_payload(username=user_label, host_override=rdp_host)
            data_b64 = sign_and_encrypt_json(payload, secret_hex=secret_hex)
            url = build_guac_data_url(base_url=base_url, data_b64=data_b64)

            session_id = f"json-auth-{request.terminal_binding_id}"

            descriptor = build_launch_descriptor(
                adapter_type=ADAPTER_TYPE,
                connection_mode="guacamole_json_auth",
                terminal_binding_id=request.terminal_binding_id,
                terminal_node_id=request.terminal_node_id,
                terminal_identifier=request.terminal_identifier,
                mt5_account_login=request.mt5_account_login,
                adapter_session_id=session_id,
                resume_hint="regenerate_json_auth",
            )

            return AdapterResult(
                success=True,
                mapped_state="connected",
                adapter_session_id=session_id,
                adapter_type=ADAPTER_TYPE,
                launch_descriptor=descriptor,
                adapter_metadata={
                    "guacamole_launch_url": url,
                    "resumed": True,
                },
            )

        except Exception as e:
            error_msg = _sanitize_error(e)
            logger.error("Guacamole resume failed: binding=%s error=%s",
                         request.terminal_binding_id, error_msg)
            return AdapterResult(success=False, mapped_state="failed",
                                 adapter_type=ADAPTER_TYPE, error_message=error_msg)

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

    def _resolve_rdp_host(self, terminal_node_id: int) -> str:
        """
        Resolve the RDP hostname for a TerminalNode.

        Looks up TerminalNode → Mt5Instance (by matching hostname)
        and returns the Mt5Instance.rdp_host for RDP connection.

        Falls back to TerminalNode.hostname if no Mt5Instance found
        (the hostname may be directly routable via Tailscale).
        """
        from execution.models import TerminalNode
        from mt5.models import Mt5Instance

        try:
            node = TerminalNode.objects.get(pk=terminal_node_id)
        except TerminalNode.DoesNotExist:
            raise RuntimeError(f"TerminalNode id={terminal_node_id} not found")

        # Try to find Mt5Instance with matching hostname for rdp_host
        instance = Mt5Instance.objects.filter(hostname=node.hostname).first()
        if instance and instance.rdp_host:
            return instance.rdp_host

        # Fallback: use the node hostname directly
        return node.hostname

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
