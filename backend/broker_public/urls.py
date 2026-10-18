"""broker_public URL routing — PUBLIC (unauthenticated) directory + metrics (DARK; 404 until armed)."""
from django.urls import path

from .views import PublicBrokerDirectoryView, PublicBrokerProfileView

urlpatterns = [
    path("brokers/", PublicBrokerDirectoryView.as_view(), name="public-broker-directory"),
    path("brokers/<slug:slug>/", PublicBrokerProfileView.as_view(), name="public-broker-profile"),
]
