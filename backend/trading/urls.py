from rest_framework.routers import DefaultRouter
from .views_brokers import BrokerServerViewSet
from .views import TradingAccountViewSet, TradeViewSet

router = DefaultRouter()
router.register("accounts", TradingAccountViewSet, basename="trading-account")
router.register("trades", TradeViewSet, basename="trade")
router.register(r"broker-servers", BrokerServerViewSet, basename="broker-servers")

urlpatterns = router.urls