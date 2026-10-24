"""trade_copier.urls — read-only Copy Trading API (PR C). Mounted under /api/copy-trading/."""
from django.urls import path

from trade_copier.consumer_api import (
    PartialCloseAuthorizeSendView,
    PartialCloseClaimView,
    PartialCloseCompleteView,
)
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
    # ADR-0056 — dedicated DEMO partial-close consumer (modern worker auth only; DARK, no worker granted
    # the permission outside tests; serves ONLY PARTIAL_CLOSE_TRADE).
    path("partial-close/claim/", PartialCloseClaimView.as_view(), name="copy-trading-pclose-claim"),
    path("partial-close/authorize-send/", PartialCloseAuthorizeSendView.as_view(),
         name="copy-trading-pclose-authorize-send"),
    path("partial-close/complete/", PartialCloseCompleteView.as_view(), name="copy-trading-pclose-complete"),
]
