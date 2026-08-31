"""DARK gate for the approval-consumption path. Same idiom as hosted_workspace.flags._flag."""
from __future__ import annotations

import os

from django.conf import settings

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def approvals_enabled() -> bool:
    """Master DARK flag. When OFF (default), ``is_artefact_approved`` returns False for EVERYTHING regardless
    of any stored APPROVED row — so a consumer (provisioning) fails closed to its safe fallback. Arming this is
    a separate, explicit Programme step; registering/deciding approvals is unaffected (they just have no effect
    until armed)."""
    return _flag("APPROVALS_ENABLED")
