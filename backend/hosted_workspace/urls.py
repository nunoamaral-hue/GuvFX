"""ADR-0034 / M3c — Hosted Workspace read-only API routes (DARK).

Mounted under ``/api/hosted-workspace/`` (see ``guvfx_backend/urls.py``). Every route 404s while
``hosted_persistent_mt5_enabled()`` is OFF (enforced in the view, before any DB read).
"""
from django.urls import path

from hosted_workspace.delivery_views import HostedWorkspaceDeliveryStateView
from hosted_workspace.views import HostedWorkspaceStateView

app_name = "hosted_workspace"

urlpatterns = [
    path("workspace-state/", HostedWorkspaceStateView.as_view(), name="workspace-state"),
    path("delivery-state/", HostedWorkspaceDeliveryStateView.as_view(), name="delivery-state"),
]
