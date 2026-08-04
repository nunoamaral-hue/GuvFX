"""WP5.1 — operational-events API routes (ADR-0032). One owner-scoped, read-only endpoint."""
from django.urls import path

from .views import OperationalAccountEventsView

urlpatterns = [
    path("account-events/", OperationalAccountEventsView.as_view(), name="operations-account-events"),
]
