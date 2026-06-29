from django.urls import path

from .views import (
    TradingHealthView, HealthMatrixView, AlertListView, AlertAcknowledgeView,
    RecommendationListView, HeartbeatIngestView,
    RecoveryAttemptListView, RecoveryStatusView, CircuitResetView,
)

urlpatterns = [
    path("trading-health/", TradingHealthView.as_view(), name="reliability-trading-health"),
    path("health/", HealthMatrixView.as_view(), name="reliability-health"),
    path("alerts/", AlertListView.as_view(), name="reliability-alerts"),
    path("alerts/<int:pk>/acknowledge/", AlertAcknowledgeView.as_view(), name="reliability-alert-ack"),
    path("recommendations/", RecommendationListView.as_view(), name="reliability-recommendations"),
    path("heartbeat/", HeartbeatIngestView.as_view(), name="reliability-heartbeat"),
    # RX-2G recovery (shadow-only)
    path("recovery-attempts/", RecoveryAttemptListView.as_view(), name="reliability-recovery-attempts"),
    path("recovery-status/", RecoveryStatusView.as_view(), name="reliability-recovery-status"),
    path("circuit/reset/", CircuitResetView.as_view(), name="reliability-circuit-reset"),
]
