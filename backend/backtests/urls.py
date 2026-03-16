from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BacktestConfigViewSet,
    BacktestJobArtifactsView,
    BacktestJobResultsView,
    BacktestJobRunView,
    BacktestJobStatusView,
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
    # Packet B — B5: Canonical backtest API endpoints
    path(
        "jobs/run/",
        BacktestJobRunView.as_view(),
        name="backtest-job-run",
    ),
    path(
        "jobs/<int:job_id>/status/",
        BacktestJobStatusView.as_view(),
        name="backtest-job-status",
    ),
    path(
        "jobs/<int:job_id>/results/",
        BacktestJobResultsView.as_view(),
        name="backtest-job-results",
    ),
    path(
        "jobs/<int:job_id>/artifacts/",
        BacktestJobArtifactsView.as_view(),
        name="backtest-job-artifacts",
    ),
    # Legacy endpoints
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