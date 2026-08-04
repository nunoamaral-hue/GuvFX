from django.urls import path
from .views import health_check, VersionView

urlpatterns = [
    path("health/", health_check, name="health-check"),
    # IPR Area G — staff-only build provenance + live arming-flag snapshot (deploy-parity oracle).
    path("version/", VersionView.as_view(), name="version"),
]