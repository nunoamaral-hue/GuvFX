"""
Security headers middleware for GuvFX API.

Ensures consistent security headers are set on all API responses,
regardless of reverse proxy configuration. Headers are only set
if not already present (to avoid conflicts with upstream proxies).

Headers applied:
- Strict-Transport-Security (HSTS)
- X-Frame-Options
- X-Content-Type-Options
- Referrer-Policy
- Permissions-Policy
- Content-Security-Policy (minimal, for API)
"""
from django.conf import settings


class SecurityHeadersMiddleware:
    """
    Middleware to add security headers to all responses.

    Only sets headers if they are not already present, allowing
    reverse proxies to take precedence if configured.
    """

    # Security headers to apply (header_name: value)
    SECURITY_HEADERS = {
        # HSTS: Force HTTPS for 1 year, include subdomains, preload
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
        # Prevent clickjacking - DENY all framing
        "X-Frame-Options": "DENY",
        # Prevent MIME type sniffing attacks
        "X-Content-Type-Options": "nosniff",
        # Control referrer information (match frontend policy)
        "Referrer-Policy": "strict-origin-when-cross-origin",
        # Restrict browser features/sensors (match frontend policy)
        "Permissions-Policy": (
            "camera=(), microphone=(), geolocation=(), payment=(), "
            "usb=(), magnetometer=(), gyroscope=(), accelerometer=()"
        ),
        # Minimal CSP for API: prevent framing, restrict form targets
        # Kept minimal to avoid interfering with API clients
        "Content-Security-Policy": "frame-ancestors 'none'; default-src 'none'",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Only add security headers in production (DEBUG=False)
        # or if explicitly enabled via setting
        if not getattr(settings, "DEBUG", False) or getattr(
            settings, "SECURITY_HEADERS_ALWAYS", False
        ):
            self._add_security_headers(response)

        return response

    def _add_security_headers(self, response):
        """
        Add security headers to response if not already present.

        This allows reverse proxies (Traefik, nginx) to set headers
        upstream without being overwritten.
        """
        for header, value in self.SECURITY_HEADERS.items():
            # Only set if not already present (case-insensitive check)
            if header not in response:
                response[header] = value
