"""
URL configuration for Admin Operations Console.

All routes live under /api/admin/ and are RBAC-gated.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    AdminReconciliationViewSet,
    AdminPaymentEventViewSet,
    AdminWorkerViewSet,
    AdminEntitlementSummaryView,
    AdminEntitlementOverrideViewSet,
    AdminExecutionJobViewSet,
    AdminBetaEstateView,
)

router = DefaultRouter()
router.register("reconciliation/events", AdminReconciliationViewSet, basename="admin-reconciliation")
router.register("payments/events", AdminPaymentEventViewSet, basename="admin-payment-event")
router.register("execution/jobs", AdminExecutionJobViewSet, basename="admin-execution-job")

urlpatterns = [
    # Router-managed viewsets
    path("", include(router.urls)),

    # Workers — manual routes for ViewSet (no model-backed queryset)
    path("workers/", AdminWorkerViewSet.as_view({"get": "list", "post": "create"}), name="admin-workers-list"),
    path("workers/<int:pk>/", AdminWorkerViewSet.as_view({"get": "retrieve"}), name="admin-worker-detail"),
    path("workers/<int:pk>/rotate-secret/", AdminWorkerViewSet.as_view({"post": "rotate_secret"}), name="admin-worker-rotate"),
    path("workers/<int:pk>/revoke/", AdminWorkerViewSet.as_view({"post": "revoke"}), name="admin-worker-revoke"),

    # GFX-BETA-PHASE0 Increment 5 — read-only per-user beta estate (no decrypted credentials)
    path("beta-estate/", AdminBetaEstateView.as_view(), name="admin-beta-estate"),

    # Entitlements — summary + override CRUD
    path("entitlements/<int:user_id>/summary/", AdminEntitlementSummaryView.as_view(), name="admin-entitlement-summary"),
    path("entitlements/overrides/", AdminEntitlementOverrideViewSet.as_view({"get": "list", "post": "create"}), name="admin-overrides-list"),
    path("entitlements/overrides/<int:pk>/", AdminEntitlementOverrideViewSet.as_view({"get": "retrieve"}), name="admin-override-detail"),
    path("entitlements/overrides/<int:pk>/renew/", AdminEntitlementOverrideViewSet.as_view({"post": "renew"}), name="admin-override-renew"),
    path("entitlements/overrides/<int:pk>/cancel/", AdminEntitlementOverrideViewSet.as_view({"post": "cancel"}), name="admin-override-cancel"),
]
