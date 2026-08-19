from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class CustomerTelegramBinding(models.Model):
    """Authoritative Telegram private-chat binding for one GuvFX user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="customer_telegram_binding",
    )
    telegram_chat_id = models.BigIntegerField()
    telegram_user_id = models.BigIntegerField(null=True, blank=True)
    telegram_username = models.CharField(max_length=64, blank=True, default="")
    telegram_first_name = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    connected_at = models.DateTimeField(default=timezone.now)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["telegram_chat_id"], condition=Q(is_active=True),
                name="cust_tg_one_active_chat",
            ),
        ]


class TelegramConnectionToken(models.Model):
    """Short-lived, single-use Telegram deep-link token; only its SHA-256 digest is stored."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="telegram_connection_tokens",
    )
    token_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "expires_at"], name="tg_token_user_exp_idx")]


class CustomerNotificationPreference(models.Model):
    """User-controlled Telegram categories. These flags never alter trading state."""

    class Language(models.TextChoices):
        EN = "en", "English"
        JA = "ja", "Japanese"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="customer_notification_preferences",
    )
    telegram_enabled = models.BooleanField(default=True)
    trade_opened = models.BooleanField(default=True)
    trade_updated = models.BooleanField(default=True)
    trade_closed = models.BooleanField(default=True)
    strategy_changed = models.BooleanField(default=True)
    execution_problem = models.BooleanField(default=True)
    workspace_ready = models.BooleanField(default=True)
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CustomerNotificationProjectionCursor(models.Model):
    """Durable high-water mark for bounded reconciliation of one authoritative source."""

    source = models.CharField(max_length=64, unique=True)
    last_created_at = models.DateTimeField(null=True, blank=True)
    last_object_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CustomerNotificationWorkerState(models.Model):
    """Secret-free heartbeat for the dedicated notification worker."""

    class State(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        DARK = "DARK", "Dark"

    key = models.CharField(max_length=32, primary_key=True, default="delivery", editable=False)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_cycle_state = models.CharField(max_length=8, choices=State.choices, default=State.DARK)
    updated_at = models.DateTimeField(auto_now=True)


class CustomerNotification(models.Model):
    """Durable, user/account-scoped asynchronous customer-notification outbox."""

    class EventType(models.TextChoices):
        CONNECTION_CONFIRMED = "CONNECTION_CONFIRMED", "Telegram connected"
        TRADE_OPENED = "TRADE_OPENED", "Trade opened"
        TRADE_UPDATED = "TRADE_UPDATED", "Trade updated"
        TRADE_CLOSED = "TRADE_CLOSED", "Trade closed"
        STRATEGY_ENABLED = "STRATEGY_ENABLED", "Strategy enabled"
        STRATEGY_DISABLED = "STRATEGY_DISABLED", "Strategy disabled"
        EXECUTION_PROBLEM = "EXECUTION_PROBLEM", "Trading needs attention"
        WORKSPACE_READY = "WORKSPACE_READY", "Workspace ready"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing (never auto-retried)"
        RETRYING = "RETRYING", "Retrying"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed"
        SUPPRESSED = "SUPPRESSED", "Suppressed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="customer_notifications",
    )
    account = models.ForeignKey(
        "trading.TradingAccount", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="customer_notifications",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices, db_index=True)
    source_object_type = models.CharField(max_length=80)
    source_object_id = models.CharField(max_length=80)
    dedupe_key = models.CharField(max_length=200, unique=True)
    payload = models.JSONField(default=dict)
    language = models.CharField(
        max_length=2, choices=CustomerNotificationPreference.Language.choices,
        default=CustomerNotificationPreference.Language.EN,
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="cust_notify_queue_idx"),
            models.Index(fields=["user", "-created_at"], name="cust_notify_user_idx"),
        ]


class CustomerNotificationAttemptQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("CustomerNotificationAttempt records are immutable")

    def delete(self):
        raise ValueError("CustomerNotificationAttempt records are immutable")


class CustomerNotificationAttemptManager(models.Manager):
    def get_queryset(self):
        return CustomerNotificationAttemptQuerySet(self.model, using=self._db)


class CustomerNotificationAttempt(models.Model):
    """Append-only proof of a delivery attempt; retains attribution after disconnect."""

    class Result(models.TextChoices):
        DELIVERED = "DELIVERED", "Delivered"
        RETRY = "RETRY", "Retry scheduled"
        FAILED = "FAILED", "Failed"
        SUPPRESSED = "SUPPRESSED", "Suppressed"

    notification = models.ForeignKey(
        CustomerNotification, on_delete=models.PROTECT, related_name="delivery_attempts",
    )
    attempt = models.PositiveSmallIntegerField()
    result = models.CharField(max_length=12, choices=Result.choices)
    recipient_chat_id = models.BigIntegerField(null=True, blank=True)
    provider_message_id = models.CharField(max_length=64, blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CustomerNotificationAttemptManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "attempt"], name="cust_notify_one_attempt_num",
            ),
        ]
        ordering = ["notification_id", "attempt"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("CustomerNotificationAttempt records are immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("CustomerNotificationAttempt records are immutable")
