from django.contrib.auth import get_user_model
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .serializers import (
    RegisterSerializer,
    UserSerializer,
    EmailTokenObtainPairSerializer,
    ChangePasswordSerializer,
)

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    """
    POST /api/auth/register/
    """
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class MeView(APIView):
    """
    GET /api/auth/me/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        data = dict(UserSerializer(request.user).data)
        # Phase 9 — per-user customer-facing UX capability (additive). Empty allowlist by default ⇒ False for
        # every user, so the frontend defaults to the legacy experience; granted only for support@ in the POC.
        # Fails closed (False) so a lookup error never exposes the unfinished multi-account UX.
        try:
            from trading.account_entitlement import user_broker_ux_enabled
            data["broker_accounts_ux"] = user_broker_ux_enabled(request.user)
        except Exception:  # noqa: BLE001
            data["broker_accounts_ux"] = False
        return Response(data)


class EmailTokenObtainPairView(TokenObtainPairView):
    """
    POST /api/auth/token/
    Accepts: {"email": "...", "password": "..."}
    """
    serializer_class = EmailTokenObtainPairSerializer


class ChangePasswordView(APIView):
    """
    POST /api/auth/change-password/
    Body: {"old_password": "...", "new_password": "..."}
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Password updated successfully."})