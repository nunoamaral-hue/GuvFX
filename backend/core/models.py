"""
Core models for GuvFX platform.

Includes audit logging infrastructure for security compliance.
"""
import uuid
from django.db import models
from django.conf import settings


# ---------------------------------------------------------------------------
# Immutable QuerySet / Manager for AuditEvent
# ---------------------------------------------------------------------------

class AuditEventQuerySet(models.QuerySet):
    """QuerySet that blocks bulk update and delete to enforce immutability."""

    def update(self, **kwargs):
        raise ValueError("AuditEvent records are immutable and cannot be updated.")

    def delete(self):
        raise ValueError("AuditEvent records cannot be deleted.")


class AuditEventManager(models.Manager):
    def get_queryset(self):
        return AuditEventQuerySet(self.model, using=self._db)


class AuditEvent(models.Model):
    """
    Append-only audit log for security-relevant events.

    This model captures all user actions that should be tracked for
    compliance, debugging, and security monitoring purposes.

    Design principles:
    - Append-only: Records are never updated or deleted
    - Fail-open: Logging failures must not block business operations
    - Non-sensitive: No passwords, tokens, or PII in metadata
    """

    class Severity(models.TextChoices):
        DEBUG = "DEBUG", "Debug"
        INFO = "INFO", "Info"
        WARN = "WARN", "Warning"
        ERROR = "ERROR", "Error"
        CRITICAL = "CRITICAL", "Critical"

    # Event types - keep consistent with frontend/backend usage
    class EventType(models.TextChoices):
        # Authentication
        AUTH_LOGIN = "AUTH_LOGIN", "User Login"
        AUTH_LOGOUT = "AUTH_LOGOUT", "User Logout"
        AUTH_REFRESH = "AUTH_REFRESH", "Token Refresh"
        AUTH_FAILED = "AUTH_FAILED", "Authentication Failed"

        # Strategy management
        STRATEGY_CREATED = "STRATEGY_CREATED", "Strategy Created"
        STRATEGY_UPDATED = "STRATEGY_UPDATED", "Strategy Updated"
        STRATEGY_DELETED = "STRATEGY_DELETED", "Strategy Deleted"

        # Backtest operations
        BACKTEST_CONFIG_CREATED = "BACKTEST_CONFIG_CREATED", "Backtest Config Created"
        BACKTEST_RUN_CREATED = "BACKTEST_RUN_CREATED", "Backtest Run Created"
        BACKTEST_RUNS_PROCESSED = "BACKTEST_RUNS_PROCESSED", "Backtest Runs Processed"

        # Account operations
        ACCOUNT_LINKED = "ACCOUNT_LINKED", "Account Linked"
        ACCOUNT_UNLINKED = "ACCOUNT_UNLINKED", "Account Unlinked"

        # Assignment operations
        ASSIGNMENT_CREATED = "ASSIGNMENT_CREATED", "Strategy Assigned"
        ASSIGNMENT_UPDATED = "ASSIGNMENT_UPDATED", "Assignment Updated"
        ASSIGNMENT_REMOVED = "ASSIGNMENT_REMOVED", "Assignment Removed"

        # Execution controls (future - currently disabled)
        EXECUTION_ENABLE_ATTEMPT = "EXECUTION_ENABLE_ATTEMPT", "Execution Enable Attempted"
        EXECUTION_DISABLE_ATTEMPT = "EXECUTION_DISABLE_ATTEMPT", "Execution Disable Attempted"
        EXECUTION_KILL_ATTEMPT = "EXECUTION_KILL_ATTEMPT", "Kill Switch Attempted"

        # Execution job lifecycle
        EXECUTION_JOB_CREATED = "EXECUTION_JOB_CREATED", "Execution Job Created"
        EXECUTION_JOB_CLAIMED = "EXECUTION_JOB_CLAIMED", "Execution Job Claimed"
        EXECUTION_JOB_COMPLETED = "EXECUTION_JOB_COMPLETED", "Execution Job Completed"
        EXECUTION_JOB_FAILED = "EXECUTION_JOB_FAILED", "Execution Job Failed"

        # Rate limiting
        RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED", "Rate Limit Exceeded"

        # Trade ingestion
        TRADES_INGESTED = "TRADES_INGESTED", "Trades Ingested"
        TRADES_SYNC_QUEUED = "TRADES_SYNC_QUEUED", "Trades Sync Queued"

        # Strategy signal lifecycle
        SIGNAL_EVALUATED = "SIGNAL_EVALUATED", "Signal Evaluated"
        SIGNAL_REJECTED = "SIGNAL_REJECTED", "Signal Rejected"
        SIGNAL_CREATED = "SIGNAL_CREATED", "Signal Created (Job Queued)"

        # Worker authentication
        WORKER_AUTH_SUCCESS = "WORKER_AUTH_SUCCESS", "Worker Auth Success"
        WORKER_AUTH_FAILED = "WORKER_AUTH_FAILED", "Worker Auth Failed"

        # Subscription mutations
        SUBSCRIPTION_CREATED = "SUBSCRIPTION_CREATED", "Subscription Created"
        SUBSCRIPTION_UPDATED = "SUBSCRIPTION_UPDATED", "Subscription Updated"

        # Admin overrides
        ADMIN_OVERRIDE = "ADMIN_OVERRIDE", "Admin Override"

        # Entitlement enforcement
        ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED", "Entitlement Denied"

        # Reconciliation
        RECONCILIATION_RUN_STARTED = "RECONCILIATION_RUN_STARTED", "Reconciliation Run Started"
        RECONCILIATION_RUN_COMPLETED = "RECONCILIATION_RUN_COMPLETED", "Reconciliation Run Completed"
        RECONCILIATION_DISCREPANCY = "RECONCILIATION_DISCREPANCY", "Reconciliation Discrepancy Detected"

        # Terminal node lifecycle
        NODE_CREATED = "NODE_CREATED", "Terminal Node Created"
        NODE_STATUS_CHANGED = "NODE_STATUS_CHANGED", "Terminal Node Status Changed"
        NODE_ACCOUNT_ASSIGNED = "NODE_ACCOUNT_ASSIGNED", "Account Assigned to Node"
        NODE_ACCOUNT_UNASSIGNED = "NODE_ACCOUNT_UNASSIGNED", "Account Unassigned from Node"
        NODE_WRONG_CLAIM = "NODE_WRONG_CLAIM", "Worker Claimed Wrong-Node Job"
        NODE_HEARTBEAT_STALE = "NODE_HEARTBEAT_STALE", "Terminal Node Heartbeat Stale"

        # Backtest worker lifecycle (Packet B — B2)
        BACKTEST_EXECUTION_CLAIMED = "BACKTEST_EXECUTION_CLAIMED", "Backtest Execution Claimed"
        BACKTEST_EXECUTION_STARTED = "BACKTEST_EXECUTION_STARTED", "Backtest Execution Started"
        BACKTEST_EXECUTION_COMPLETED = "BACKTEST_EXECUTION_COMPLETED", "Backtest Execution Completed"
        BACKTEST_EXECUTION_FAILED = "BACKTEST_EXECUTION_FAILED", "Backtest Execution Failed"

        # Backtest API lifecycle (Packet B — B5)
        BACKTEST_JOB_CREATED = "BACKTEST_JOB_CREATED", "Backtest Job Created"
        BACKTEST_STATUS_VIEWED = "BACKTEST_STATUS_VIEWED", "Backtest Status Viewed"
        BACKTEST_RESULTS_VIEWED = "BACKTEST_RESULTS_VIEWED", "Backtest Results Viewed"
        BACKTEST_ARTIFACTS_VIEWED = "BACKTEST_ARTIFACTS_VIEWED", "Backtest Artifacts Viewed"

        # Payment webhook lifecycle
        WEBHOOK_SIGNATURE_FAILED = "WEBHOOK_SIGNATURE_FAILED", "Webhook Signature Failed"
        WEBHOOK_DUPLICATE_REJECTED = "WEBHOOK_DUPLICATE_REJECTED", "Webhook Duplicate Rejected"
        WEBHOOK_STALE_REJECTED = "WEBHOOK_STALE_REJECTED", "Webhook Stale Event Rejected"
        WEBHOOK_SUBSCRIPTION_TRANSITIONED = "WEBHOOK_SUBSCRIPTION_TRANSITIONED", "Webhook Subscription Transitioned"
        WEBHOOK_PROCESSING_FAILED = "WEBHOOK_PROCESSING_FAILED", "Webhook Processing Failed"

    # Use immutable manager to block bulk update/delete at QuerySet level.
    objects = AuditEventManager()

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # User who triggered the event (null for system events or anonymous)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )

    event_type = models.CharField(
        max_length=64,
        choices=EventType.choices,
        db_index=True,
    )

    severity = models.CharField(
        max_length=16,
        choices=Severity.choices,
        default=Severity.INFO,
        db_index=True,
    )

    # Entity being acted upon
    entity_type = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Type of entity (e.g., 'strategy', 'account', 'backtest_config')",
    )

    entity_id = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        db_index=True,
        help_text="ID of the entity (string to support UUIDs and integers)",
    )

    # Request context
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
    )

    user_agent = models.TextField(
        blank=True,
        default="",
    )

    path = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Request path (e.g., '/api/strategies/')",
    )

    method = models.CharField(
        max_length=12,
        blank=True,
        default="",
        help_text="HTTP method (GET, POST, PUT, DELETE, etc.)",
    )

    # Additional context (JSON)
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional event context. Must NOT contain sensitive data.",
    )

    # Timestamp (auto, not editable)
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event_type", "created_at"], name="core_audite_event_t_68fb24_idx"),
            models.Index(fields=["user", "created_at"], name="core_audite_user_id_69cb1f_idx"),
            models.Index(fields=["entity_type", "entity_id"], name="core_audite_entity__e1955d_idx"),
        ]
        # Prevent accidental updates
        verbose_name = "Audit Event"
        verbose_name_plural = "Audit Events"

    def __str__(self):
        user_str = self.user.email if self.user else "anonymous"
        return f"[{self.severity}] {self.event_type} by {user_str} at {self.created_at}"

    def save(self, *args, **kwargs):
        # Only allow inserts, not updates (append-only)
        if self.pk and AuditEvent.objects.filter(pk=self.pk).exists():
            raise ValueError("AuditEvent records are immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("AuditEvent records cannot be deleted.")
