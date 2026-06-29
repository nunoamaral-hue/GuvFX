"""
URL configuration for Packet A — Terminal Interaction API.

All routes live under /api/mt5-interaction/.
"""
from django.urls import path

from .views_interaction import (
    SessionLaunchView,
    SessionDetailView,
    SessionResumeView,
    SessionTerminateView,
    TerminalBindingListView,
    ActiveSessionView,
)

urlpatterns = [
    # Session lifecycle
    path(
        "sessions/",
        SessionLaunchView.as_view(),
        name="mt5-interaction-session-launch",
    ),
    # PX-7A: current resumable session for the user (read-only re-discovery).
    # Declared before <int:pk> ("active" is not an int, but explicit is safer).
    path(
        "sessions/active/",
        ActiveSessionView.as_view(),
        name="mt5-interaction-session-active",
    ),
    path(
        "sessions/<int:pk>/",
        SessionDetailView.as_view(),
        name="mt5-interaction-session-detail",
    ),
    path(
        "sessions/<int:pk>/resume/",
        SessionResumeView.as_view(),
        name="mt5-interaction-session-resume",
    ),
    path(
        "sessions/<int:pk>/terminate/",
        SessionTerminateView.as_view(),
        name="mt5-interaction-session-terminate",
    ),

    # Terminal bindings
    path(
        "terminal-bindings/",
        TerminalBindingListView.as_view(),
        name="mt5-interaction-binding-list",
    ),
]
