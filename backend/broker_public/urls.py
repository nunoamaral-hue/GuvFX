"""broker_public URL routing — PUBLIC (unauthenticated) directory + metrics (DARK; 404 until armed)."""
from django.urls import path

from .views import (PublicBrokerDirectoryView, PublicBrokerProfileView,
                    PublicBrokerWithdrawalIntelligenceView, PublicCaseStudyView, PublicWtmPreviewView)

urlpatterns = [
    path("brokers/", PublicBrokerDirectoryView.as_view(), name="public-broker-directory"),
    path("wtm-preview/", PublicWtmPreviewView.as_view(), name="public-wtm-preview"),
    path("case-study/", PublicCaseStudyView.as_view(), name="public-case-study"),
    # More-specific route first so a two-segment broker path is never swallowed by the single-segment profile route.
    path("brokers/<slug:slug>/withdrawal-intelligence/", PublicBrokerWithdrawalIntelligenceView.as_view(),
         name="public-broker-withdrawal-intelligence"),
    path("brokers/<slug:slug>/", PublicBrokerProfileView.as_view(), name="public-broker-profile"),
]
