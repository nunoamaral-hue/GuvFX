"""
Core models for GuvFX platform.

Includes audit logging infrastructure for security compliance.
"""
import uuid
from django.db import models
from django.conf import settings


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
        BACKTEST_PROCESSED = "BACKTEST_PROCESSED", "Backtests Processed"

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

        # Rate limiting
        RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED", "Rate Limit Exceeded"

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
            models.Index(fields=["event_type", "created_at"]),
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["entity_type", "entity_id"]),
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
