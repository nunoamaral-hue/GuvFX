from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    StrategyViewSet,
    StrategyAssignmentViewSet,
    StrategyAutoTuneView,   # <-- make sure this exists in views.py
    StrategyChangeLogViewSet,
)

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
]

urlpatterns += router.urls