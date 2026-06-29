from django.urls import path
from rest_framework.routers import DefaultRouter
from .views_brokers import BrokerServerViewSet
from .views import TradingAccountViewSet, TradeViewSet, SyncNowView, SetIngestCutoverView
from .views_account_add import AddAccountWithMt5LoginView

router = DefaultRouter()
router.register("accounts", TradingAccountViewSet, basename="trading-account")
router.register("trades", TradeViewSet, basename="trade")
router.register(r"broker-servers", BrokerServerViewSet, basename="broker-servers")

urlpatterns = [
    # IMPORTANT: this must come BEFORE router.urls, otherwise the router treats it as <pk>
    path('accounts/add-with-mt5-login/', AddAccountWithMt5LoginView.as_view(), name='accounts-add-with-mt5-login'),
    path('sync-now/', SyncNowView.as_view(), name='trading-sync-now'),
    path('set-ingest-cutover/', SetIngestCutoverView.as_view(), name='trading-set-ingest-cutover'),
] + router.urls
