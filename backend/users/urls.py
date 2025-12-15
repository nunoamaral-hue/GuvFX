from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import RegisterView, MeView, EmailTokenObtainPairView, ChangePasswordView

urlpatterns = [
    # Registration
    path("register/", RegisterView.as_view(), name="auth-register"),

    # JWT login / refresh (email-based)
    path("token/", EmailTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),

    # Current user
    path("me/", MeView.as_view(), name="auth-me"),

    # Change password
    path("change-password/", ChangePasswordView.as_view(), name="auth-change-password"),
]