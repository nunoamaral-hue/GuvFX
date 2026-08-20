from django.contrib import admin

from .models import (
    CustomerNotification,
    CustomerNotificationAttempt,
    CustomerNotificationPreference,
    CustomerNotificationProjectionCursor,
    CustomerStrategyNotificationPreference,
    CustomerNotificationWorkerState,
    CustomerTelegramBinding,
    TelegramConnectionToken,
    WorkspaceReadinessNotificationIntent,
)


class CustomerNotificationReadOnlyAdmin(admin.ModelAdmin):
    """Operational visibility only; admin cannot bypass customer identity/queue invariants."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CustomerTelegramBinding)
class CustomerTelegramBindingAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = ("user", "is_active", "connected_at", "disconnected_at")
    exclude = ("telegram_chat_id", "telegram_user_id")
    readonly_fields = ("connected_at", "created_at", "updated_at")
    search_fields = ("user__email",)


@admin.register(CustomerNotificationPreference)
class CustomerNotificationPreferenceAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = (
        "user", "winning_trades", "losing_trades", "tp_progress", "system_messages",
        "language", "updated_at",
    )


@admin.register(CustomerStrategyNotificationPreference)
class CustomerStrategyNotificationPreferenceAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = ("user", "assignment", "enabled", "pending_enable", "updated_at")


@admin.register(WorkspaceReadinessNotificationIntent)
class WorkspaceReadinessNotificationIntentAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = ("user", "workspace", "milestone", "fulfilled_at", "created_at")


@admin.register(CustomerNotificationProjectionCursor)
class CustomerNotificationProjectionCursorAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = ("source", "last_created_at", "last_object_id", "updated_at")


@admin.register(CustomerNotificationWorkerState)
class CustomerNotificationWorkerStateAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = ("key", "last_cycle_state", "last_heartbeat_at", "updated_at")


@admin.register(CustomerNotification)
class CustomerNotificationAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = ("id", "user", "account", "event_type", "status", "attempts", "created_at")
    list_filter = ("event_type", "status", "language")
    readonly_fields = ("dedupe_key", "payload", "created_at", "updated_at", "delivered_at")


@admin.register(CustomerNotificationAttempt)
class CustomerNotificationAttemptAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = ("notification", "attempt", "result", "provider_message_id", "created_at")
    exclude = ("recipient_chat_id",)
    readonly_fields = (
        "notification", "attempt", "result", "provider_message_id", "error_code", "created_at",
    )

@admin.register(TelegramConnectionToken)
class TelegramConnectionTokenAdmin(CustomerNotificationReadOnlyAdmin):
    list_display = ("user", "expires_at", "consumed_at", "created_at")
    exclude = ("token_digest",)
    readonly_fields = ("expires_at", "consumed_at", "created_at")
