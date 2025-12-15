from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BacktestConfigViewSet,
    BacktestRunViewSet,
    ProcessPendingBacktestsView,
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
]

urlpatterns += router.urls