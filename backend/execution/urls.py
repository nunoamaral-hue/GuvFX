"""
URL configuration for execution app.

Includes:
- Execution job management (existing)
- Execution control stubs (MVP - all return 501)
"""
from django.urls import path

from .views import (
    ExecutionEnableView,
    ExecutionDisableView,
    ExecutionKillAllView,
)

urlpatterns = [
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
