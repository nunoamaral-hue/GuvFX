"""ADR-0034 / M3c — Hosted Workspace read-only API routes (DARK).

Mounted under ``/api/hosted-workspace/`` (see ``guvfx_backend/urls.py``). Every route 404s while
``hosted_persistent_mt5_enabled()`` is OFF (enforced in the view, before any DB read).
"""
from django.urls import path

from hosted_workspace.delivery_views import (
    HostedWorkspaceDeliveryConnectView,
    HostedWorkspaceDeliveryStateView,
)
from hosted_workspace.onboarding_views import (
    OnboardingAuthorizeExecutionView,
    OnboardingBindView,
    OnboardingConfirmView,
    OnboardingJourneyView,
    OnboardingOpsView,
    OnboardingRequestView,
)
from hosted_workspace.views import HostedWorkspaceStateView

app_name = "hosted_workspace"

urlpatterns = [
    path("workspace-state/", HostedWorkspaceStateView.as_view(), name="workspace-state"),
    # ADR-0034 Workspace Delivery — DARK RemoteApp delivery-state read model.
    path("delivery-state/", HostedWorkspaceDeliveryStateView.as_view(), name="delivery-state"),
    # ADR-0034 Workspace Delivery — DARK owner-scoped RemoteApp connect (mints the signed descriptor).
    path("delivery-connect/", HostedWorkspaceDeliveryConnectView.as_view(), name="delivery-connect"),
    # ADR-0034 Onboarding — DARK customer journey API (404-invisible unless master + onboarding flags ON).
    path("onboarding/journey/", OnboardingJourneyView.as_view(), name="onboarding-journey"),
    path("onboarding/request/", OnboardingRequestView.as_view(), name="onboarding-request"),
    path("onboarding/bind/", OnboardingBindView.as_view(), name="onboarding-bind"),
    path("onboarding/confirm/", OnboardingConfirmView.as_view(), name="onboarding-confirm"),
    # ADR-0047 — the customer's EXPLICIT "Enable automated trading" authorization (the only path that arms).
    path("onboarding/authorize-execution/", OnboardingAuthorizeExecutionView.as_view(),
         name="onboarding-authorize-execution"),
    path("onboarding/ops/", OnboardingOpsView.as_view(), name="onboarding-ops"),
]
