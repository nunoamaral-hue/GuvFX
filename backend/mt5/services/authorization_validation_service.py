"""
Authorization Validation Service.

Validates UserToTerminalAuthorization records for specific
capabilities (launch, resume, manual trade, chart interaction).
Provides a single entry point for capability checks used by
other services.
"""
import logging
from typing import Optional

from django.utils import timezone

from mt5.models import TerminalBinding, UserToTerminalAuthorization

logger = logging.getLogger(__name__)


class AuthorizationDenied(Exception):
    """Raised when a user lacks required authorization."""

    def __init__(self, message: str, user_id: int = 0, binding_id: int = 0):
        self.user_id = user_id
        self.binding_id = binding_id
        super().__init__(message)


def get_active_authorization(
    user_id: int,
    binding: TerminalBinding,
) -> Optional[UserToTerminalAuthorization]:
    """
    Return the active (non-revoked, non-expired) authorization for
    a user + binding pair, or None if none exists.

    If multiple active authorizations exist (shouldn't normally happen),
    returns the most recently created one.
    """
    now = timezone.now()
    return (
        UserToTerminalAuthorization.objects.filter(
            user_id=user_id,
            terminal_binding=binding,
            revoked_at__isnull=True,
        )
        .exclude(
            expires_at__isnull=False,
            expires_at__lt=now,
        )
        .order_by("-created_at")
        .first()
    )


def validate_can_launch(
    user_id: int,
    binding: TerminalBinding,
) -> UserToTerminalAuthorization:
    """
    Validate that the user can launch a session on the given binding.

    Returns the authorization record on success.
    Raises AuthorizationDenied if the user lacks can_launch.
    """
    auth = get_active_authorization(user_id, binding)
    if auth is None:
        raise AuthorizationDenied(
            f"No active authorization for user {user_id} "
            f"on binding {binding.pk}.",
            user_id=user_id,
            binding_id=binding.pk,
        )
    if not auth.can_launch:
        raise AuthorizationDenied(
            f"User {user_id} does not have can_launch permission "
            f"on binding {binding.pk}.",
            user_id=user_id,
            binding_id=binding.pk,
        )
    return auth


def validate_can_resume(
    user_id: int,
    binding: TerminalBinding,
) -> UserToTerminalAuthorization:
    """
    Validate that the user can resume a session on the given binding.

    Returns the authorization record on success.
    Raises AuthorizationDenied if the user lacks can_resume.
    """
    auth = get_active_authorization(user_id, binding)
    if auth is None:
        raise AuthorizationDenied(
            f"No active authorization for user {user_id} "
            f"on binding {binding.pk}.",
            user_id=user_id,
            binding_id=binding.pk,
        )
    if not auth.can_resume:
        raise AuthorizationDenied(
            f"User {user_id} does not have can_resume permission "
            f"on binding {binding.pk}.",
            user_id=user_id,
            binding_id=binding.pk,
        )
    return auth


def validate_can_manual_trade(
    user_id: int,
    binding: TerminalBinding,
) -> UserToTerminalAuthorization:
    """
    Validate manual trade capability.

    Returns the authorization record on success.
    Raises AuthorizationDenied if the user lacks can_manual_trade.
    """
    auth = get_active_authorization(user_id, binding)
    if auth is None:
        raise AuthorizationDenied(
            f"No active authorization for user {user_id} "
            f"on binding {binding.pk}.",
            user_id=user_id,
            binding_id=binding.pk,
        )
    if not auth.can_manual_trade:
        raise AuthorizationDenied(
            f"User {user_id} does not have can_manual_trade permission "
            f"on binding {binding.pk}.",
            user_id=user_id,
            binding_id=binding.pk,
        )
    return auth


def validate_can_chart_interact(
    user_id: int,
    binding: TerminalBinding,
) -> UserToTerminalAuthorization:
    """
    Validate chart interaction capability.

    Returns the authorization record on success.
    Raises AuthorizationDenied if the user lacks can_chart_interact.
    """
    auth = get_active_authorization(user_id, binding)
    if auth is None:
        raise AuthorizationDenied(
            f"No active authorization for user {user_id} "
            f"on binding {binding.pk}.",
            user_id=user_id,
            binding_id=binding.pk,
        )
    if not auth.can_chart_interact:
        raise AuthorizationDenied(
            f"User {user_id} does not have can_chart_interact permission "
            f"on binding {binding.pk}.",
            user_id=user_id,
            binding_id=binding.pk,
        )
    return auth
