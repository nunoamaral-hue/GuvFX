"""
Serializers for the Admin Operations Console API.

Each domain has list/detail serializers plus action-input serializers
where mutation is required.  Immutable objects (PaymentEvent, Trade,
execution payloads) have read-only serializers only.
"""

from rest_framework import serializers

from billing.models import PaymentEvent
from execution.models import ExecutionJob, WorkerIdentity, TerminalNode
from reconciliation.reconciliation_models import ReconciliationEvent
from trading.models import TradingAccount

from .models import EntitlementOverride


# =========================================================================
# 3A — Reconciliation Dashboard
# =========================================================================


class ReconciliationEventListSerializer(serializers.ModelSerializer):
    account_display = serializers.SerializerMethodField()

    class Meta:
        model = ReconciliationEvent
        fields = [
            "id",
            "account_id",
            "account_display",
            "reconciliation_run_id",
            "reconciliation_type",
            "ticket",
            "field_name",
            "mt5_value",
            "platform_value",
            "severity",
            "resolution_status",
            "created_at",
        ]

    def get_account_display(self, obj) -> str:
        return str(obj.account) if obj.account else ""


class ReconciliationEventDetailSerializer(serializers.ModelSerializer):
    account_display = serializers.SerializerMethodField()

    class Meta:
        model = ReconciliationEvent
        fields = [
            "id",
            "account_id",
            "account_display",
            "reconciliation_run_id",
            "reconciliation_type",
            "ticket",
            "field_name",
            "mt5_value",
            "platform_value",
            "severity",
            "resolution_status",
            "signature",
            "metadata",
            "created_at",
        ]

    def get_account_display(self, obj) -> str:
        return str(obj.account) if obj.account else ""


class ReconciliationAcknowledgeSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ReconciliationResolveSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


# =========================================================================
# 3B — Payment Event Viewer (read-only)
# =========================================================================


class PaymentEventListSerializer(serializers.ModelSerializer):
    """List view — excludes raw_payload for safety."""

    class Meta:
        model = PaymentEvent
        fields = [
            "id",
            "provider_name",
            "provider_event_id",
            "event_type",
            "subscription_reference",
            "processing_status",
            "provider_timestamp",
            "created_at",
            "correlation_id",
        ]


class PaymentEventDetailSerializer(serializers.ModelSerializer):
    """
    Detail view — includes sanitized payload metadata.

    ``raw_payload`` is re-sanitized on read to ensure no sensitive data
    ever reaches the admin UI, even if an earlier bug allowed storage.
    """
    sanitized_payload = serializers.SerializerMethodField()

    class Meta:
        model = PaymentEvent
        fields = [
            "id",
            "provider_name",
            "provider_event_id",
            "event_type",
            "idempotency_key",
            "subscription_reference",
            "processing_status",
            "provider_timestamp",
            "signature_verified_at",
            "processed_at",
            "created_at",
            "correlation_id",
            "sanitized_payload",
        ]

    def get_sanitized_payload(self, obj) -> dict:
        """Re-sanitize payload on read — defense in depth."""
        from billing.models import _sanitize_payload
        return _sanitize_payload(obj.raw_payload or {})


# =========================================================================
# 3C — Worker Identity Management
# =========================================================================


class WorkerIdentityListSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkerIdentity
        fields = [
            "id",
            "worker_id",
            "status",
            "worker_permissions",
            "created_at",
        ]


class WorkerIdentityDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkerIdentity
        fields = [
            "id",
            "worker_id",
            "status",
            "worker_permissions",
            "created_at",
        ]
        # worker_secret_hash is NEVER exposed


class WorkerCreateSerializer(serializers.Serializer):
    worker_id = serializers.CharField(max_length=64)
    worker_permissions = serializers.JSONField(required=False, default=dict)


class WorkerRotateSecretSerializer(serializers.Serializer):
    """No input required — new secret is generated server-side."""
    pass


class WorkerRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


# =========================================================================
# 3D — Entitlement Override Tools
# =========================================================================


class EntitlementOverrideSerializer(serializers.ModelSerializer):
    created_by_email = serializers.SerializerMethodField()
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = EntitlementOverride
        fields = [
            "id",
            "user_id",
            "capability",
            "override_value",
            "reason",
            "is_active",
            "is_expired",
            "expires_at",
            "created_by_id",
            "created_by_email",
            "created_at",
            "updated_at",
        ]

    def get_created_by_email(self, obj) -> str | None:
        return obj.created_by.email if obj.created_by else None


class EntitlementOverrideApplySerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    capability = serializers.CharField(max_length=64)
    override_value = serializers.JSONField(default=dict)
    reason = serializers.CharField()
    expires_at = serializers.DateTimeField()


class EntitlementOverrideRenewSerializer(serializers.Serializer):
    expires_at = serializers.DateTimeField()
    reason = serializers.CharField()


class EntitlementOverrideCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class EntitlementSummarySerializer(serializers.Serializer):
    """Read-only effective entitlement summary for a user."""
    user_id = serializers.IntegerField()
    email = serializers.CharField()
    source_plan = serializers.CharField(allow_null=True)
    source_plan_status = serializers.CharField()
    viewer_mode = serializers.BooleanField()
    resolved_access_mode = serializers.CharField()
    capabilities = serializers.DictField()
    active_overrides = EntitlementOverrideSerializer(many=True)


# =========================================================================
# 3E — Execution Job Diagnostics
# =========================================================================


class AdminExecutionJobListSerializer(serializers.ModelSerializer):
    account_display = serializers.SerializerMethodField()
    strategy_name = serializers.SerializerMethodField()
    terminal_node_hostname = serializers.SerializerMethodField()

    class Meta:
        model = ExecutionJob
        fields = [
            "id",
            "job_type",
            "account_id",
            "account_display",
            "strategy_id",
            "strategy_name",
            "terminal_node_id",
            "terminal_node_hostname",
            "status",
            "worker_id",
            "created_at",
            "started_at",
            "finished_at",
            "error_message",
        ]

    def get_account_display(self, obj) -> str:
        return str(obj.account) if obj.account else ""

    def get_strategy_name(self, obj) -> str | None:
        return obj.strategy.name if obj.strategy else None

    def get_terminal_node_hostname(self, obj) -> str | None:
        return obj.terminal_node.hostname if obj.terminal_node else None


class AdminExecutionJobDetailSerializer(serializers.ModelSerializer):
    """
    Detail serializer.  ``payload`` is exposed READ-ONLY — no edit path.
    """
    account_display = serializers.SerializerMethodField()
    strategy_name = serializers.SerializerMethodField()
    terminal_node_hostname = serializers.SerializerMethodField()

    class Meta:
        model = ExecutionJob
        fields = [
            "id",
            "job_type",
            "account_id",
            "account_display",
            "strategy_id",
            "strategy_name",
            "assignment_id",
            "terminal_node_id",
            "terminal_node_hostname",
            "payload",
            "status",
            "worker_id",
            "result",
            "error_message",
            "created_at",
            "started_at",
            "finished_at",
            "created_by_id",
        ]

    def get_account_display(self, obj) -> str:
        return str(obj.account) if obj.account else ""

    def get_strategy_name(self, obj) -> str | None:
        return obj.strategy.name if obj.strategy else None

    def get_terminal_node_hostname(self, obj) -> str | None:
        return obj.terminal_node.hostname if obj.terminal_node else None


class ExecutionJobRetrySerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ExecutionJobCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")
