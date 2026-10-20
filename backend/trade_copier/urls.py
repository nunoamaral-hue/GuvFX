"""trade_copier.urls — read-only Copy Trading API (PR C). Mounted under /api/copy-trading/."""
from django.urls import path

from trade_copier.views import (
    CopyTradingMastersView,
    CopyTradingOverviewView,
    CopyTradingStatusView,
)

urlpatterns = [
    path("status/", CopyTradingStatusView.as_view(), name="copy-trading-status"),
    path("masters/", CopyTradingMastersView.as_view(), name="copy-trading-masters"),
    path("accounts/<int:account_id>/overview/", CopyTradingOverviewView.as_view(),
         name="copy-trading-overview"),
]
