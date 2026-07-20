from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    StrategyViewSet,
    StrategyAssignmentViewSet,
    StrategyAutoTuneView,   # <-- make sure this exists in views.py
    StrategyChangeLogViewSet,
)
from .views_sizing import AssignmentLegSizingView, AssignmentLegSizingHistoryView

router = DefaultRouter()
router.register("strategies", StrategyViewSet, basename="strategy")
router.register("assignments", StrategyAssignmentViewSet, basename="strategy-assignment")
router.register("changes", StrategyChangeLogViewSet, basename="strategy-change")

urlpatterns = [
    path(
        "strategies/<int:pk>/auto-tune/",
        StrategyAutoTuneView.as_view(),
        name="strategy-auto-tune",
    ),
    # GFX-BETA-PHASE0 Increment 1 — per-assignment lot-size override (account-owner scoped).
    path(
        "assignments/<int:pk>/leg-sizing/",
        AssignmentLegSizingView.as_view(),
        name="assignment-leg-sizing",
    ),
    path(
        "assignments/<int:pk>/leg-sizing/history/",
        AssignmentLegSizingHistoryView.as_view(),
        name="assignment-leg-sizing-history",
    ),
]

urlpatterns += router.urls