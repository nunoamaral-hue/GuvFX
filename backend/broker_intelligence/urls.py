"""broker_intelligence URL routing (read-only member API)."""
from django.urls import path

from .views import WithdrawalMetricsView

urlpatterns = [
    path("withdrawals/metrics/", WithdrawalMetricsView.as_view(), name="broker-intelligence-withdrawal-metrics"),
]
