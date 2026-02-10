"""
Cookie-based JWT authentication with CSRF enforcement.

This module provides JWT authentication via HttpOnly cookies (guvfx_access)
with mandatory CSRF protection for unsafe HTTP methods (POST, PUT, PATCH, DELETE).

Security model:
- Cookie-authenticated requests MUST include valid X-CSRFToken header for unsafe methods
- Header-authenticated requests (Authorization: Bearer ...) bypass CSRF (stateless)
- CSRF failure returns 403 Forbidden
"""
import secrets

from django.conf import settings
from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication


# CSRF token length (Django uses 32 bytes = 64 hex chars for the secret)
CSRF_SECRET_LENGTH = 32
CSRF_TOKEN_LENGTH = 2 * CSRF_SECRET_LENGTH  # 64 chars when hex-encoded


def _sanitize_token(token: str) -> str:
    """
    Sanitize a CSRF token: strip whitespace, validate length and charset.
    Returns the sanitized token or raises ValueError if invalid.
    """
    if not isinstance(token, str):
        raise ValueError("CSRF token must be a string")

    token = token.strip()

    if len(token) == 0:
        raise ValueError("CSRF token is empty")

    # Django CSRF tokens are alphanumeric (hex or base64-like)
    # Accept alphanumeric chars only for security
    if not token.isalnum():
        raise ValueError("CSRF token contains invalid characters")

    return token


def _constant_time_compare(val1: str, val2: str) -> bool:
    """
    Constant-time string comparison to prevent timing attacks.
    Uses secrets.compare_digest for security.
    """
    if not isinstance(val1, str) or not isinstance(val2, str):
        return False
    return secrets.compare_digest(val1, val2)


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

        This performs a direct CSRF token check using constant-time comparison,
        ensuring the check always happens for cookie-authenticated requests.
        """
        # Safe methods don't need CSRF
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return

        # Get CSRF cookie name from settings (default: 'csrftoken')
        cookie_name = getattr(settings, "CSRF_COOKIE_NAME", "csrftoken")

        # Get CSRF cookie
        csrf_cookie = request.COOKIES.get(cookie_name)
        if not csrf_cookie:
            raise exceptions.PermissionDenied(
                "CSRF validation failed: CSRF cookie not set"
            )

        # Sanitize the cookie token
        try:
            csrf_cookie_token = _sanitize_token(csrf_cookie)
        except ValueError as e:
            raise exceptions.PermissionDenied(
                f"CSRF validation failed: invalid CSRF cookie ({e})"
            )

        # Get the CSRF token from header (X-CSRFToken)
        request_csrf_token = request.META.get("HTTP_X_CSRFTOKEN", "")

        # If no header token, check POST data as fallback (Django's behavior)
        if not request_csrf_token:
            # For JSON requests, request.POST won't have it, but we try anyway
            request_csrf_token = request.POST.get("csrfmiddlewaretoken", "")

        if not request_csrf_token:
            raise exceptions.PermissionDenied(
                "CSRF validation failed: CSRF token missing from request headers"
            )

        # Sanitize the request token
        try:
            request_csrf_token = _sanitize_token(request_csrf_token)
        except ValueError as e:
            raise exceptions.PermissionDenied(
                f"CSRF validation failed: invalid CSRF token ({e})"
            )

        # Compare tokens using constant-time comparison
        if not _constant_time_compare(request_csrf_token, csrf_cookie_token):
            raise exceptions.PermissionDenied(
                "CSRF validation failed: CSRF token mismatch"
            )
