"""
URL configuration for execution app.

Includes:
- Demo trade execution (safety-first, demo accounts only)
- Execution control stubs (MVP - all return 501)

Note: ExecutionJobViewSet is registered in guvfx_backend/urls.py via router.
"""
from django.urls import path

from .views import (
    CreateDemoTradeJobView,
    ExecutionEnableView,
    ExecutionDisableView,
    ExecutionKillAllView,
)

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
]
