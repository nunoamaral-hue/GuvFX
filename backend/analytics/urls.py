from django.urls import path

from .views import AccountPerformanceView, StrategyBacktestSummaryView
from .views_trade_history import TradeHistoryView, StrategyMetricsView

urlpatterns = [
    path("account-performance/", AccountPerformanceView.as_view(), name="account-performance"),
    path("strategy-backtests/", StrategyBacktestSummaryView.as_view(), name="strategy-backtests"),
]