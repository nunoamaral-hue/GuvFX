from __future__ import annotations

from typing import Optional, Tuple

from django.contrib.auth.models import AnonymousUser
from django.utils.translation import gettext_lazy as _
from rest_framework.request import Request
from rest_framework_simplejwt.authentication import JWTAuthentication


class CookieJWTAuthentication(JWTAuthentication):
    """
    Reads SimpleJWT access token from HttpOnly cookie (guvfx_access).
    Falls back to normal Authorization: Bearer header if present.
    """

    access_cookie_name = "guvfx_access"

    def authenticate(self, request: Request) -> Optional[Tuple[object, object]]:
        # If an Authorization header exists, use default behavior
        header = self.get_header(request)
        if header is not None:
            return super().authenticate(request)

        raw_token = request.COOKIES.get(self.access_cookie_name)
        if not raw_token:
            return None

        validated_token = self.get_validated_token(raw_token)
        return self.get_user(validated_token), validated_token
