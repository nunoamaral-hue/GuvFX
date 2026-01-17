from django.urls import path

from .views import AccountPerformanceView, StrategyBacktestSummaryView
from .views_trade_history import TradeHistoryView, StrategyMetricsView, StrategyHasTradesView

urlpatterns = [
    path("account-performance/", AccountPerformanceView.as_view(), name="account-performance"),
    path("strategy-backtests/", StrategyBacktestSummaryView.as_view(), name="strategy-backtests"),
    path("trade-history/", TradeHistoryView.as_view(), name="trade-history"),
    path("strategy-metrics/", StrategyMetricsView.as_view(), name="strategy-metrics"),
    path("strategy-has-trades/", StrategyHasTradesView.as_view(), name="strategy-has-trades"),
]