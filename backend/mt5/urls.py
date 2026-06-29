from django.urls import path
from .views import (
    ValidateMt5View,
    Mt5StatusView,
    Mt5DesktopLinkView,
    Mt5LaunchApplyView,
    Mt5PoolStatusView,
    Mt5ReleaseView,
)

urlpatterns = [
    path("validate/", ValidateMt5View.as_view()),
    path("status/", Mt5StatusView.as_view()),
    path("desktop-link/", Mt5DesktopLinkView.as_view()),
    path("launch-apply/", Mt5LaunchApplyView.as_view()),
    path("pool-status/", Mt5PoolStatusView.as_view(), name="mt5-pool-status"),
    path("release/", Mt5ReleaseView.as_view(), name="mt5-release"),
]
