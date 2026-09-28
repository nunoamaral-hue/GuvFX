from django.urls import path

from .views import AccountPerformanceView, StrategyBacktestSummaryView
from .views_trade_history import TradeHistoryView, StrategyMetricsView, StrategyHasTradesView, DailyPnlView
from .views_portfolio import PortfolioSummaryView, PortfolioOpenTradesView, PortfolioEquityCurveView

urlpatterns = [
    path("account-performance/", AccountPerformanceView.as_view(), name="account-performance"),
    path("strategy-backtests/", StrategyBacktestSummaryView.as_view(), name="strategy-backtests"),
    path("trade-history/", TradeHistoryView.as_view(), name="trade-history"),
    path("strategy-metrics/", StrategyMetricsView.as_view(), name="strategy-metrics"),
    path("strategy-has-trades/", StrategyHasTradesView.as_view(), name="strategy-has-trades"),
    path("daily-pnl/", DailyPnlView.as_view(), name="daily-pnl"),
    path("portfolio/summary/", PortfolioSummaryView.as_view(), name="portfolio-summary"),
    path("portfolio/open-trades/", PortfolioOpenTradesView.as_view(), name="portfolio-open-trades"),
    path("portfolio/equity-curve/", PortfolioEquityCurveView.as_view(), name="portfolio-equity-curve"),
]