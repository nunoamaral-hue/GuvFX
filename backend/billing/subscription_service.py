"""
Subscription lifecycle transition service.

Single service-layer path for all subscription state mutations triggered
by webhook events.  Webhook handlers must never mutate subscription models
directly — all transitions flow through ``apply_subscription_transition``.

Entitlements are **not** stored here.  They remain computed from persisted
billing state via ``billing.entitlements.resolve_entitlements``.

State normalization mapping (webhook → canonical ``PlanStatus``):
    webhook "trial"     → PlanStatus.TRIAL_ACTIVE
    webhook "active"    → PlanStatus.ACTIVE
    webhook "past_due"  → PlanStatus.PAST_DUE
    webhook "cancelled" → PlanStatus.CANCELLED
    webhook "suspended" → PlanStatus.EXPIRED   (closest safe collapse)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.contrib.auth import get_user_model
from django.utils import timezone

from billing.models import UserSubscriptionState

logger = logging.getLogger(__name__)

User = get_user_model()

# ---------------------------------------------------------------------------
# Normalization map: webhook incoming status → canonical PlanStatus value
# ---------------------------------------------------------------------------

_WEBHOOK_STATUS_MAP: dict[str, str] = {
    "trial": UserSubscriptionState.PlanStatus.TRIAL_ACTIVE,
    "active": UserSubscriptionState.PlanStatus.ACTIVE,
    "past_due": UserSubscriptionState.PlanStatus.PAST_DUE,
    "cancelled": UserSubscriptionState.PlanStatus.CANCELLED,
    "suspended": UserSubscriptionState.PlanStatus.EXPIRED,
}

# Statuses that should set viewer_mode = True (no active entitlements).
_VIEWER_STATUSES = frozenset({
    UserSubscriptionState.PlanStatus.EXPIRED,
    UserSubscriptionState.PlanStatus.CANCELLED,
    UserSubscriptionState.PlanStatus.VIEWER_ONLY,
})


# ---------------------------------------------------------------------------
# Transition result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TransitionResult:
    """Outcome of a subscription transition attempt."""
    success: bool
    previous_status: str
    new_status: str
    error: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_webhook_status(webhook_status: str) -> Optional[str]:
    """
    Map a webhook-provided status string to the canonical
    ``PlanStatus`` value.

    Returns ``None`` if the status is unrecognized.
    """
    return _WEBHOOK_STATUS_MAP.get(webhook_status)


def apply_subscription_transition(
    user: User,
    target_status: str,
    plan: Optional[str] = None,
) -> TransitionResult:
    """
    Apply a subscription lifecycle transition for *user*.

    Args:
        user: The user whose subscription is being transitioned.
        target_status: The **canonical** ``PlanStatus`` value to transition
            to (already normalized from webhook status).
        plan: Optional plan slug to set (e.g. ``"standard"``).  If ``None``
            the current plan is preserved.

    Returns:
        ``TransitionResult`` indicating success/failure and the
        before/after statuses.

    This function is the **only** authorised path for webhook-driven
    subscription mutations.  It:
    1. Loads (or creates) the ``UserSubscriptionState``.
    2. Validates that the target_status is a known canonical value.
    3. Persists the transition (plan_status, viewer_mode, timestamps).
    4. Does **not** store entitlements — they are computed on demand.
    """
    # Validate target_status is a known canonical value.
    valid_statuses = {c[0] for c in UserSubscriptionState.PlanStatus.choices}
    if target_status not in valid_statuses:
        return TransitionResult(
            success=False,
            previous_status="",
            new_status=target_status,
            error=f"Unknown canonical status: {target_status!r}",
        )

    state, _created = UserSubscriptionState.objects.get_or_create(
        user=user,
        defaults={
            "plan_status": UserSubscriptionState.PlanStatus.VIEWER_ONLY,
            "viewer_mode": True,
        },
    )

    previous_status = state.plan_status

    # Apply transition
    state.plan_status = target_status
    if plan is not None:
        state.current_plan = plan

    # Derive viewer_mode from the new status.
    state.viewer_mode = target_status in _VIEWER_STATUSES

    # Update billing timestamp for transition ordering authority.
    state.last_plan_change_at = timezone.now()

    # Track first payment if transitioning to an active paid status.
    if target_status == UserSubscriptionState.PlanStatus.ACTIVE:
        state.has_ever_paid = True
        state.last_payment_at = timezone.now()

    try:
        state.save()
    except Exception as exc:
        logger.exception(
            "Subscription transition failed: user=%s target=%s",
            user.id,
            target_status,
        )
        return TransitionResult(
            success=False,
            previous_status=previous_status,
            new_status=target_status,
            error=str(exc),
        )

    logger.info(
        "Subscription transitioned: user=%s %s → %s",
        user.id,
        previous_status,
        target_status,
    )

    return TransitionResult(
        success=True,
        previous_status=previous_status,
        new_status=target_status,
    )
