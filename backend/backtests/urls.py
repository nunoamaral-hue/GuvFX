from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BacktestConfigViewSet,
    BacktestJobArtifactsView,
    BacktestJobResultsView,
    BacktestJobRunView,
    BacktestJobStatusView,
    BacktestPromoteView,
    BacktestRunViewSet,
    ProcessPendingBacktestsView,
    PromotionCandidateReviewView,
    ExecutionCandidateStageView,
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
        "run/",
        BacktestJobRunView.as_view(),
        name="backtest-job-run",
    ),
    path(
        "status/<int:job_id>/",
        BacktestJobStatusView.as_view(),
        name="backtest-status",
    ),
    path(
        "results/<int:job_id>/",
        BacktestJobResultsView.as_view(),
        name="backtest-results",
    ),
    path(
        "artifacts/<int:job_id>/",
        BacktestJobArtifactsView.as_view(),
        name="backtest-artifacts",
    ),
    # Packet B — B7: Promotion candidate endpoint
    path(
        "<int:execution_id>/promote/",
        BacktestPromoteView.as_view(),
        name="backtest-promote",
    ),
    # Packet C1: Promotion candidate review endpoint
    path(
        "candidates/<int:candidate_id>/review/",
        PromotionCandidateReviewView.as_view(),
        name="backtest-promotion-review",
    ),
    # Packet C2: Execution candidate staging endpoint
    path(
        "candidates/<int:candidate_id>/stage/",
        ExecutionCandidateStageView.as_view(),
        name="execution-candidate-stage",
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