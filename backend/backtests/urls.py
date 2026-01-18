from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BacktestConfigViewSet,
    BacktestRunViewSet,
    ProcessPendingBacktestsView,
    WindowsBacktestRunView,
    WindowsBacktestStatusView,
    WindowsBacktestResultView,
)

router = DefaultRouter()
router.register("configs", BacktestConfigViewSet, basename="backtest-config")
router.register("runs", BacktestRunViewSet, basename="backtest-run")

urlpatterns = [
    path(
        "process-pending/",
        ProcessPendingBacktestsView.as_view(),
        name="backtests-process-pending",
    ),
    # Windows Agent backtest endpoints
    path(
        "windows/run/",
        WindowsBacktestRunView.as_view(),
        name="windows-backtest-run",
    ),
    path(
        "windows/status/",
        WindowsBacktestStatusView.as_view(),
        name="windows-backtest-status",
    ),
    path(
        "windows/result/",
        WindowsBacktestResultView.as_view(),
        name="windows-backtest-result",
    ),
]

urlpatterns += router.urls