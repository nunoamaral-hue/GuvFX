"""
Binding Resolution Service.

Resolves and validates TerminalBinding instances for a given user
and account context.  Determines which bindings are available,
valid, and eligible for session launch or resume.
"""
import logging
from typing import Optional

from django.db.models import QuerySet

from mt5.models import TerminalBinding, UserToTerminalAuthorization

logger = logging.getLogger(__name__)


class BindingResolutionError(Exception):
    """Raised when binding resolution fails."""
    pass


def resolve_available_bindings(
    user_id: int,
    mt5_account_login: str,
    environment_type: str = "",
) -> QuerySet[TerminalBinding]:
    """
    Return all TerminalBindings that are available and authorized
    for the given user + MT5 account login.

    Filters:
    - binding.status == AVAILABLE
    - binding.mt5_account_login matches
    - active (non-revoked, non-expired) authorization exists for user
    - environment_type matches (if provided)
    """
    bindings = TerminalBinding.objects.filter(
        mt5_account_login=mt5_account_login,
        status=TerminalBinding.Status.AVAILABLE,
        authorizations__user_id=user_id,
        authorizations__revoked_at__isnull=True,
    )

    if environment_type:
        bindings = bindings.filter(environment_type=environment_type)

    # Exclude expired authorizations
    from django.utils import timezone

    now = timezone.now()
    bindings = bindings.exclude(
        authorizations__expires_at__isnull=False,
        authorizations__expires_at__lt=now,
    )

    return bindings.distinct().select_related("terminal_node")


def resolve_binding_by_id(
    binding_id: int,
    user_id: int,
) -> TerminalBinding:
    """
    Resolve a specific TerminalBinding by ID, validating that the
    user has an active authorization for it.

    Raises BindingResolutionError if not found or unauthorized.
    """
    try:
        binding = TerminalBinding.objects.select_related(
            "terminal_node",
        ).get(pk=binding_id)
    except TerminalBinding.DoesNotExist:
        raise BindingResolutionError(
            f"TerminalBinding {binding_id} does not exist."
        )

    if not _has_active_authorization(user_id, binding):
        raise BindingResolutionError(
            f"User {user_id} has no active authorization for "
            f"TerminalBinding {binding_id}."
        )

    return binding


def validate_binding_launchable(binding: TerminalBinding) -> None:
    """
    Validate that a binding is in a state that allows session launch.

    A binding is launchable if:
    - status is AVAILABLE
    - occupied_by_session is None

    Raises BindingResolutionError otherwise.
    """
    if binding.status != TerminalBinding.Status.AVAILABLE:
        raise BindingResolutionError(
            f"TerminalBinding {binding.pk} is not available "
            f"(current status: {binding.status})."
        )

    if binding.occupied_by_session_id is not None:
        raise BindingResolutionError(
            f"TerminalBinding {binding.pk} is already occupied "
            f"by session {binding.occupied_by_session_id}."
        )


def _has_active_authorization(
    user_id: int,
    binding: TerminalBinding,
) -> bool:
    """Check if user has at least one active authorization for the binding."""
    from django.utils import timezone

    now = timezone.now()
    return UserToTerminalAuthorization.objects.filter(
        user_id=user_id,
        terminal_binding=binding,
        revoked_at__isnull=True,
    ).exclude(
        expires_at__isnull=False,
        expires_at__lt=now,
    ).exists()
