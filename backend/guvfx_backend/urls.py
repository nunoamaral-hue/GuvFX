from django.contrib import admin
from django.urls import path, include
from users.auth_cookie_views import cookie_login, cookie_refresh, cookie_logout, cookie_csrf
from rest_framework.routers import DefaultRouter
from .health import health
from execution.views import (
    ExecutionJobViewSet,
    WorkerAccountCredentialsView,
    CreateOpenTradeJobView,
)

# Windows Agent views (MVP)
from backtests.views import AIBacktestRecommendationsView
from strategies.views import WindowsStrategyAssignView

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
    path("api/execution/", include("execution.urls")),  # Execution control stubs
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
    path("api/billing/", include("billing.urls")),
    path("api/admin/", include("admin_ops.urls")),
    path("api/mt5/", include("mt5.urls")),
    path("api/mt5-interaction/", include("mt5.urls_interaction")),
    path("health/", health),
    # Windows Agent MVP endpoints (direct wiring)
    path(
        "api/ai/backtest-recommendations/",
        AIBacktestRecommendationsView.as_view(),
        name="ai-backtest-recommendations",
    ),
    path(
        "api/strategies/windows/assign/",
        WindowsStrategyAssignView.as_view(),
        name="windows-strategy-assign",
    ),
]

# Cookie-based JWT auth (HttpOnly)
urlpatterns += [
    path("api/auth/cookie/login/", cookie_login),
    path("api/auth/cookie/csrf/", cookie_csrf),
    path("api/auth/cookie/refresh/", cookie_refresh),
    path("api/auth/cookie/logout/", cookie_logout),
]