"""broker_public URL routing — PUBLIC (unauthenticated) directory + metrics (DARK; 404 until armed)."""
from django.urls import path

from .views import (PublicBrokerDirectoryView, PublicBrokerProfileView, PublicCaseStudyView,
                    PublicWtmPreviewView)

urlpatterns = [
    path("brokers/", PublicBrokerDirectoryView.as_view(), name="public-broker-directory"),
    path("wtm-preview/", PublicWtmPreviewView.as_view(), name="public-wtm-preview"),
    path("case-study/", PublicCaseStudyView.as_view(), name="public-case-study"),
    path("brokers/<slug:slug>/", PublicBrokerProfileView.as_view(), name="public-broker-profile"),
]
