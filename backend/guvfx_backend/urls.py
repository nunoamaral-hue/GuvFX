from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from execution.views import (
    ExecutionJobViewSet,
    WorkerAccountCredentialsView,
    CreateOpenTradeJobView,
)

router = DefaultRouter()
router.register("execution/jobs", ExecutionJobViewSet, basename="execution-job")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
    path("api/auth/", include("users.urls")),
    path("api/trading/", include("trading.urls")),
    path("api/strategies/", include("strategies.urls")),
    path("api/backtests/", include("backtests.urls")),
    path("api/analytics/", include("analytics.urls")),
    path("api/ai/", include("ai_helper.urls")),
    path(
        "api/execution/open-trade/",
        CreateOpenTradeJobView.as_view(),
        name="execution-open-trade",
    ),
    path(
        "api/execution/accounts/<int:account_id>/credentials/",
        WorkerAccountCredentialsView.as_view(),
        name="execution-account-credentials",
    ),
    path("api/", include(router.urls)),
    path("api/hosting/", include("hosting.urls")),
]
