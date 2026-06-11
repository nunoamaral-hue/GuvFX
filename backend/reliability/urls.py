from django.urls import path

from .views import (
    TradingHealthView, HealthMatrixView, AlertListView, AlertAcknowledgeView, RecommendationListView,
)

urlpatterns = [
    path("trading-health/", TradingHealthView.as_view(), name="reliability-trading-health"),
    path("health/", HealthMatrixView.as_view(), name="reliability-health"),
    path("alerts/", AlertListView.as_view(), name="reliability-alerts"),
    path("alerts/<int:pk>/acknowledge/", AlertAcknowledgeView.as_view(), name="reliability-alert-ack"),
    path("recommendations/", RecommendationListView.as_view(), name="reliability-recommendations"),
]
