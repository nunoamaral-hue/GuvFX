"""broker_public DARK feature flags (settings-override-then-env; default OFF)."""
from __future__ import annotations

import os

from django.conf import settings

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def broker_public_directory_enabled() -> bool:
    """Master gate for the PUBLIC (unauthenticated) broker directory + metrics endpoints. DEFAULT OFF → every public
    endpoint 404s, so the whole public surface ships DARK and is armed deliberately (and never auto-enables general
    onboarding). Read-only aggregate data only; NO member data, NO execution/credential authority. Independent of
    whether aggregates have been computed — an armed-but-empty directory simply returns INSUFFICIENT_DATA."""
    return _flag("BROKER_PUBLIC_DIRECTORY_ENABLED")
