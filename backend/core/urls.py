from django.urls import path
from .views import health_check, OperationalReadinessView, VersionView

urlpatterns = [
    path("health/", health_check, name="health-check"),
    # IPR Area G — staff-only build provenance + live arming-flag snapshot (deploy-parity oracle).
    path("version/", VersionView.as_view(), name="version"),
    # ADR-0035 — staff-only, DARK-by-default Operational Readiness console (health + preflight + rollback).
    path("operational-readiness/", OperationalReadinessView.as_view(), name="operational-readiness"),
]