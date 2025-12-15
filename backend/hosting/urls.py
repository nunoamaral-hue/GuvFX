from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    HostingProviderViewSet,
    VpsPlanViewSet,
    VpsInstanceViewSet,
    Mt5InstanceViewSet,
    MyHostingView,
    HostingRequestViewSet,
    MyConsolesView,
)

router = DefaultRouter()
router.register(r"providers", HostingProviderViewSet, basename="hosting-provider")
router.register(r"plans", VpsPlanViewSet, basename="hosting-plan")
router.register(r"vps", VpsInstanceViewSet, basename="hosting-vps")
router.register(r"mt5", Mt5InstanceViewSet, basename="hosting-mt5")
router.register(r"requests", HostingRequestViewSet, basename="hosting-request")

urlpatterns = [
    path("me/", MyHostingView.as_view(), name="hosting-me"),
    path("my-consoles/", MyConsolesView.as_view(), name="hosting-my-consoles"),
    path("", include(router.urls)),
]
