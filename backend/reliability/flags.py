"""Reliability DARK feature flags (settings-override-then-env; default OFF).

Mirrors the broker_intelligence flag idiom: a flag is ON only when a settings attribute or the
environment variable is explicitly truthy, so an unset flag fails closed (the gated surface 404s).
"""

import os

from django.conf import settings

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def wayond_metrics_operator_enabled() -> bool:
    """DARK gate for the staff-only Wayond truthful-metrics operator endpoint. Default OFF -> 404."""
    return _flag("WAYOND_METRICS_OPERATOR_ENABLED")
