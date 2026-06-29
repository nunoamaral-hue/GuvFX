from django.http import JsonResponse
from django.middleware.csrf import get_token
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from core.audit import log_auth_login, log_auth_logout, log_auth_failed
from core.throttling import GuvFXCSRFRateThrottle, GuvFXAuthRateThrottle

COOKIE_ACCESS = "guvfx_access"
COOKIE_REFRESH = "guvfx_refresh"

def _cookie_kwargs():
    return dict(
        httponly=True,
        secure=True,
        samesite="Lax",
        domain=".guvfx.com",
        path="/",
    )

@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([GuvFXAuthRateThrottle])
def cookie_login(request):
    email = request.data.get("email", request.data.get("username", ""))

    serializer = TokenObtainPairSerializer(data=request.data)
    try:
        serializer.is_valid(raise_exception=True)
    except (InvalidToken, TokenError) as e:
        log_auth_failed(request, email, reason=str(e))
        raise
    except Exception as e:
        log_auth_failed(request, email, reason="validation_error")
        raise

    access = serializer.validated_data["access"]
    refresh = serializer.validated_data["refresh"]

    # Log successful login
    user = serializer.user
    log_auth_login(request, user.id, user.email)

    resp = JsonResponse({"ok": True})
    ck = _cookie_kwargs()
    resp.set_cookie(COOKIE_ACCESS, access, max_age=60 * 15, **ck)             # 15 min
    resp.set_cookie(COOKIE_REFRESH, refresh, max_age=60 * 60 * 24 * 7, **ck)  # 7 days
    return resp

@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([GuvFXAuthRateThrottle])
def cookie_refresh(request):
    refresh = request.COOKIES.get(COOKIE_REFRESH, "")
    serializer = TokenRefreshSerializer(data={"refresh": refresh})
    serializer.is_valid(raise_exception=True)

    access = serializer.validated_data["access"]
    resp = JsonResponse({"ok": True})
    ck = _cookie_kwargs()
    resp.set_cookie(COOKIE_ACCESS, access, max_age=60 * 15, **ck)
    return resp

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cookie_logout(request):
    # Log logout before clearing cookies
    log_auth_logout(request)

    resp = JsonResponse({"ok": True})
    ck = _cookie_kwargs()
    resp.set_cookie(COOKIE_ACCESS, "", max_age=0, **ck)
    resp.set_cookie(COOKIE_REFRESH, "", max_age=0, **ck)
    return resp

# --- CSRF helper for cookie auth (frontend calls this first) ---
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response


@method_decorator(ensure_csrf_cookie, name="get")
class CookieCSRFView(APIView):
    """
    GET /api/auth/cookie/csrf/

    Returns CSRF token and sets csrftoken cookie.
    Frontend should call this before cookie-login/refresh.

    Rate limited to 60/min per IP to prevent abuse.
    """
    permission_classes = [AllowAny]
    throttle_classes = [GuvFXCSRFRateThrottle]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


# Legacy function-based view for backward compatibility with urls.py
cookie_csrf = CookieCSRFView.as_view()
