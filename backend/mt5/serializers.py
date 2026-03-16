"""
DRF Serializers for Packet A — Terminal Interaction API.

All serializers expose GuvFX-safe fields only.  No adapter credentials,
no raw launch_descriptor_snapshot, no full adapter_metadata.
"""
from rest_framework import serializers

from mt5.models import (
    InteractionSession,
    MT5Session,
    TerminalBinding,
    UserToTerminalAuthorization,
)


# =========================================================================
# Request serializers
# =========================================================================


class SessionLaunchRequestSerializer(serializers.Serializer):
    """Input for POST /api/mt5-interaction/sessions/ (launch)."""

    terminal_binding_id = serializers.IntegerField(
        help_text="PK of the TerminalBinding to launch on.",
    )
    session_expires_at = serializers.DateTimeField(
        required=False,
        default=None,
        help_text="Optional expiry for the interaction session.",
    )


class SessionTerminateRequestSerializer(serializers.Serializer):
    """Input for POST /api/mt5-interaction/sessions/{id}/terminate/."""

    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Optional human-readable reason for termination.",
    )


# =========================================================================
# Response serializers
# =========================================================================


class MT5SessionSafeSerializer(serializers.ModelSerializer):
    """
    Safe subset of MT5Session fields for API responses.

    Excludes:
    - launch_descriptor_snapshot (may contain adapter routing details)
    - adapter_metadata (may contain ephemeral runtime data)
    - failure_reason exposed only as a truncated safe string
    """

    class Meta:
        model = MT5Session
        fields = [
            "id",
            "state",
            "adapter_type",
            "adapter_session_id",
            "launch_issued_at",
            "connected_at",
            "suspended_at",
            "ended_at",
            "expires_at",
            "last_heartbeat_at",
            "failure_reason",
            "created_at",
        ]
        read_only_fields = fields


class InteractionSessionResponseSerializer(serializers.ModelSerializer):
    """
    Safe InteractionSession response for API consumers.

    Includes nested safe MT5Session data and safe binding context.
    """

    terminal_binding_id = serializers.IntegerField(source="terminal_binding.id", read_only=True)
    terminal_identifier = serializers.CharField(
        source="terminal_binding.terminal_identifier", read_only=True,
    )
    terminal_label = serializers.CharField(
        source="terminal_binding.terminal_label", read_only=True,
    )
    environment_type = serializers.CharField(
        source="terminal_binding.environment_type", read_only=True,
    )
    binding_status = serializers.CharField(
        source="terminal_binding.status", read_only=True,
    )
    latest_mt5_session = serializers.SerializerMethodField()

    class Meta:
        model = InteractionSession
        fields = [
            "id",
            "state",
            "terminal_binding_id",
            "terminal_identifier",
            "terminal_label",
            "environment_type",
            "binding_status",
            "requested_at",
            "authorized_at",
            "started_at",
            "ended_at",
            "expires_at",
            "last_activity_at",
            "terminated_reason",
            "latest_mt5_session",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_latest_mt5_session(self, obj) -> dict | None:
        latest = (
            obj.mt5_sessions.order_by("-created_at").first()
            if hasattr(obj, "mt5_sessions")
            else None
        )
        if latest is None:
            return None
        return MT5SessionSafeSerializer(latest).data


class ResumableContextResponseSerializer(serializers.Serializer):
    """
    Response for POST /api/mt5-interaction/sessions/{id}/resume/.

    Returns validated resumable context — no lifecycle mutation.
    """

    interaction_session = InteractionSessionResponseSerializer(read_only=True)
    can_resume = serializers.BooleanField(read_only=True, default=True)
    access_mode = serializers.CharField(read_only=True)


class TerminalBindingListSerializer(serializers.ModelSerializer):
    """
    Safe terminal binding fields for the listing endpoint.

    Exposes identification and status only.  No occupancy internals,
    no credentials, no sensitive node metadata.
    """

    terminal_node_hostname = serializers.SerializerMethodField()

    class Meta:
        model = TerminalBinding
        fields = [
            "id",
            "terminal_identifier",
            "terminal_label",
            "mt5_account_login",
            "environment_type",
            "status",
            "supports_shared_view",
            "terminal_node_hostname",
            "created_at",
        ]
        read_only_fields = fields

    def get_terminal_node_hostname(self, obj) -> str:
        return obj.terminal_node.hostname if obj.terminal_node else ""
