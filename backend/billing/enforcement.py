"""
Server-side entitlement enforcement.

Provides a single ``require_entitlement`` helper that resolves the user's
entitlements and raises ``PermissionDenied`` when a specific capability
is not granted.

Usage::

    from billing.enforcement import require_entitlement

    # In a DRF view:
    require_entitlement(request.user, "can_run_backtests")

    # In a management command / engine (no request object):
    require_entitlement(user, "can_deploy_automation")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rest_framework.exceptions import PermissionDenied

from .entitlements import resolve_entitlements
from .models import UserSubscriptionState

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger(__name__)

# Capabilities that can be enforced via ``require_entitlement``.
_ENFORCEABLE_CAPABILITIES = frozenset(
    {
        "can_run_backtests",
        "can_assign_strategies",
        "can_deploy_automation",
    }
)


def require_entitlement(user: AbstractBaseUser, capability: str) -> None:
    """
    Resolve entitlements for *user* and raise ``PermissionDenied`` if
    *capability* is not granted.

    Raises:
        PermissionDenied: structured error with ``error``, ``reason_code``,
            and ``message`` keys.
        ValueError: if *capability* is not a recognised enforceable gate.
    """
    if capability not in _ENFORCEABLE_CAPABILITIES:
        raise ValueError(
            f"Unknown enforceable capability: {capability!r}. "
            f"Valid values: {sorted(_ENFORCEABLE_CAPABILITIES)}"
        )

    # Fetch subscription state (may be None for users without a row).
    try:
        state = UserSubscriptionState.objects.get(user=user)
    except UserSubscriptionState.DoesNotExist:
        state = None

    entitlements = resolve_entitlements(state)

    if not getattr(entitlements, capability, False):
        logger.info(
            "Entitlement denied: user=%s capability=%s plan=%s status=%s mode=%s",
            getattr(user, "pk", "?"),
            capability,
            entitlements.source_plan,
            entitlements.source_plan_status,
            entitlements.resolved_access_mode,
        )
        raise PermissionDenied(
            {
                "error": "ENTITLEMENT_RESTRICTED",
                "reason_code": "PLAN_LIMIT",
                "capability": capability,
                "message": "Your current plan does not allow this action.",
            }
        )
