from django.urls import path
from .demo_endpoints import account_add_verify, launch
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

    path("account/add-verify/", account_add_verify, name="mt5_account_add_verify"),
    path("launch/", launch, name="mt5_launch"),
]
