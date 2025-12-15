from django.urls import path

from .views import StrategyInsightsView

urlpatterns = [
    path(
        "strategy-insights/",
        StrategyInsightsView.as_view(),
        name="strategy-insights",
    ),
]
