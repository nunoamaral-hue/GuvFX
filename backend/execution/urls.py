"""
URL configuration for execution app.

Includes:
- Demo trade execution (safety-first, demo accounts only)
- Execution control stubs (MVP - all return 501)
- Terminal node heartbeat + admin CRUD

Note: ExecutionJobViewSet is registered in guvfx_backend/urls.py via router.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    CreateDemoTradeJobView,
    ExecutionEnableView,
    ExecutionDisableView,
    ExecutionKillAllView,
    TerminalNodeHeartbeatView,
    TerminalNodeViewSet,
)

node_router = DefaultRouter()
node_router.register("nodes", TerminalNodeViewSet, basename="terminal-node")

urlpatterns = [
    # Demo trade endpoint (safety-first execution)
    path(
        "demo-trade/",
        CreateDemoTradeJobView.as_view(),
        name="execution-demo-trade",
    ),

    # Execution control stubs (501 Not Implemented in MVP)
    path(
        "enable/<int:account_id>/",
        ExecutionEnableView.as_view(),
        name="execution-enable",
    ),
    path(
        "disable/<int:account_id>/",
        ExecutionDisableView.as_view(),
        name="execution-disable",
    ),
    path(
        "kill-all/",
        ExecutionKillAllView.as_view(),
        name="execution-kill-all",
    ),

    # Terminal node heartbeat (worker-authenticated)
    path(
        "nodes/<str:hostname>/heartbeat/",
        TerminalNodeHeartbeatView.as_view(),
        name="terminal-node-heartbeat",
    ),

    # Terminal node admin CRUD (staff-only)
    path("", include(node_router.urls)),
]
