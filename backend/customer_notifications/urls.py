from django.urls import path

from .views import (
    CustomerNotificationHealthView,
    TelegramConnectView,
    TelegramDisconnectView,
    TelegramPreferencesView,
    TelegramSettingsView,
    TelegramWebhookView,
    StrategyNotificationPreferenceView,
    WorkspaceReadinessNotificationView,
)

urlpatterns = [
    path("telegram/", TelegramSettingsView.as_view(), name="customer-telegram-settings"),
    path("telegram/connect/", TelegramConnectView.as_view(), name="customer-telegram-connect"),
    path("telegram/disconnect/", TelegramDisconnectView.as_view(), name="customer-telegram-disconnect"),
    path("telegram/preferences/", TelegramPreferencesView.as_view(), name="customer-telegram-preferences"),
    path(
        "telegram/strategy-preferences/<int:assignment_id>/",
        StrategyNotificationPreferenceView.as_view(),
        name="customer-telegram-strategy-preference",
    ),
    path(
        "telegram/workspace-readiness/",
        WorkspaceReadinessNotificationView.as_view(),
        name="customer-telegram-workspace-readiness",
    ),
    path("telegram/webhook/", TelegramWebhookView.as_view(), name="customer-telegram-webhook"),
    path("health/", CustomerNotificationHealthView.as_view(), name="customer-notification-health"),
]
