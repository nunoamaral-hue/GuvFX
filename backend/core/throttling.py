"""
Rate limiting classes for GuvFX API endpoints.

Provides configurable throttling based on user authentication status
and IP address. Works with or without Redis (falls back to LocMemCache).
"""
from rest_framework.throttling import SimpleRateThrottle

from core.audit import log_event


class GuvFXUserRateThrottle(SimpleRateThrottle):
    """
    Rate limit per authenticated user.

    Limits: 100 requests per minute for authenticated users.
    Falls back to IP-based throttling for anonymous users.
    """

    scope = "user"
    rate = "100/min"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }

    def throttle_failure(self):
        """Called when throttle limit is exceeded."""
        return False

    def allow_request(self, request, view):
        """Check if request should be allowed."""
        allowed = super().allow_request(request, view)
        if not allowed:
            # Log rate limit exceeded
            self._log_rate_limit_exceeded(request)
        return allowed

    def _log_rate_limit_exceeded(self, request):
        """Log rate limit event to audit log."""
        try:
            log_event(
                request,
                event_type="RATE_LIMIT_EXCEEDED",
                severity="WARN",
                entity_type="user",
                entity_id=str(request.user.pk) if request.user and request.user.is_authenticated else None,
                metadata={
                    "scope": self.scope,
                    "rate": self.rate,
                },
            )
        except Exception:
            # Fail-open: don't block request if logging fails
            pass


class GuvFXPublicAnonRateThrottle(SimpleRateThrottle):
    """
    Dedicated per-IP rate limit for the UNAUTHENTICATED public broker-directory surface (broker_public).

    These endpoints set ``authentication_classes = []`` + ``AllowAny``, so the per-user throttle never binds; this
    gives them their own conservative anonymous ceiling (configured via the ``public`` scope in
    ``DEFAULT_THROTTLE_RATES``) that is stricter than the global IP ceiling. It is keyed purely on the client ident
    (never a user) and audits a rate-limit breach (fail-open on logging).
    """

    scope = "public"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}

    def allow_request(self, request, view):
        allowed = super().allow_request(request, view)
        if not allowed:
            try:
                log_event(request, event_type="RATE_LIMIT_EXCEEDED", severity="WARN", entity_type="public",
                          entity_id=None, metadata={"scope": self.scope, "rate": self.rate})
            except Exception:
                pass  # fail-open: never block a request because audit logging failed
        return allowed


class GuvFXIPRateThrottle(SimpleRateThrottle):
    """
    Rate limit per IP address.

    Limits: 1000 requests per minute per IP.
    This is a higher limit meant to catch abuse while allowing
    legitimate traffic from shared IPs (offices, VPNs, etc.).
    """

    scope = "ip"
    rate = "1000/min"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }

    def allow_request(self, request, view):
        """Check if request should be allowed."""
        allowed = super().allow_request(request, view)
        if not allowed:
            # Log rate limit exceeded
            try:
                log_event(
                    request,
                    event_type="RATE_LIMIT_EXCEEDED",
                    severity="WARN",
                    entity_type="ip",
                    metadata={
                        "scope": self.scope,
                        "rate": self.rate,
                        "ip": self.get_ident(request),
                    },
                )
            except Exception:
                pass
        return allowed


class GuvFXCSRFRateThrottle(SimpleRateThrottle):
    """
    Lower rate limit for CSRF token endpoint.

    Limits: 60 requests per minute per IP.
    """

    scope = "csrf"
    rate = "60/min"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class GuvFXAuthRateThrottle(SimpleRateThrottle):
    """
    Rate limit for authentication endpoints (login, refresh).

    Limits: 20 requests per minute per IP.
    This is stricter than general endpoints to prevent brute-force attacks.
    """

    scope = "auth"
    rate = "20/min"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }

    def allow_request(self, request, view):
        """Check if request should be allowed."""
        allowed = super().allow_request(request, view)
        if not allowed:
            # Log auth rate limit exceeded (potential brute-force)
            try:
                log_event(
                    request,
                    event_type="AUTH_RATE_LIMIT_EXCEEDED",
                    severity="WARN",
                    entity_type="ip",
                    metadata={
                        "scope": self.scope,
                        "rate": self.rate,
                        "ip": self.get_ident(request),
                        "path": request.path,
                    },
                )
            except Exception:
                pass
        return allowed
