"""
Cookie-based JWT authentication with CSRF enforcement.

This module provides JWT authentication via HttpOnly cookies (guvfx_access)
with mandatory CSRF protection for unsafe HTTP methods (POST, PUT, PATCH, DELETE).

Security model:
- Cookie-authenticated requests MUST include valid X-CSRFToken header for unsafe methods
- Header-authenticated requests (Authorization: Bearer ...) bypass CSRF (stateless)
- CSRF failure returns 403 Forbidden
"""
from django.conf import settings
from django.middleware.csrf import (
    REASON_BAD_TOKEN,
    REASON_NO_CSRF_COOKIE,
    _does_token_match,
    _sanitize_token,
)
from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication


class CookieJWTAuthentication(JWTAuthentication):
    """
    JWT authentication that reads tokens from HttpOnly cookies.

    When authenticating via cookie:
    - Unsafe methods (POST, PUT, PATCH, DELETE) require valid CSRF token
    - X-CSRFToken header must match csrftoken cookie

    When authenticating via Authorization header:
    - No CSRF check (stateless token flow)
    """

    def authenticate(self, request):
        # Check for Authorization header first (stateless JWT - no CSRF needed)
        header = self.get_header(request)
        if header is not None:
            return super().authenticate(request)

        # Cookie-based authentication
        raw = request.COOKIES.get("guvfx_access")
        if not raw:
            return None

        # Validate the JWT
        validated = self.get_validated_token(raw)
        user = self.get_user(validated)

        # Enforce CSRF for unsafe methods when using cookie auth
        self._enforce_csrf(request)

        return (user, validated)

    def _enforce_csrf(self, request):
        """
        Enforce CSRF protection for unsafe HTTP methods.

        Raises PermissionDenied (403) if:
        - Method is unsafe (POST, PUT, PATCH, DELETE)
        - CSRF token is missing or invalid

        This performs a direct CSRF token check rather than relying on
        Django's middleware, ensuring the check always happens for
        cookie-authenticated requests.
        """
        # Safe methods don't need CSRF
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return

        # Get CSRF cookie
        csrf_cookie = request.COOKIES.get(settings.CSRF_COOKIE_NAME)
        if not csrf_cookie:
            raise exceptions.PermissionDenied(
                f"CSRF validation failed: {REASON_NO_CSRF_COOKIE}"
            )

        # Sanitize the cookie token
        try:
            csrf_token = _sanitize_token(csrf_cookie)
        except Exception:
            raise exceptions.PermissionDenied(
                f"CSRF validation failed: {REASON_BAD_TOKEN}"
            )

        # Get the CSRF token from header (X-CSRFToken)
        request_csrf_token = request.META.get("HTTP_X_CSRFTOKEN", "")

        # If no header token, check POST data as fallback (Django's behavior)
        if not request_csrf_token:
            request_csrf_token = request.POST.get("csrfmiddlewaretoken", "")

        if not request_csrf_token:
            raise exceptions.PermissionDenied(
                "CSRF validation failed: CSRF token missing from request"
            )

        # Sanitize the request token
        try:
            request_csrf_token = _sanitize_token(request_csrf_token)
        except Exception:
            raise exceptions.PermissionDenied(
                f"CSRF validation failed: {REASON_BAD_TOKEN}"
            )

        # Compare tokens
        if not _does_token_match(request_csrf_token, csrf_token):
            raise exceptions.PermissionDenied(
                f"CSRF validation failed: {REASON_BAD_TOKEN}"
            )
