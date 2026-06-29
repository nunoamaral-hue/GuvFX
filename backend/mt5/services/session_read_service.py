"""
Session Read Service.

Narrow read-only service helpers for:
- retrieving a single InteractionSession scoped to a user
- listing terminal bindings authorized for a user

No adapter calls.  No lifecycle mutation.  No occupancy mutation.
"""
import logging

from django.db.models import QuerySet
from django.utils import timezone

from mt5.models import (
    InteractionSession,
    TerminalBinding,
    UserToTerminalAuthorization,
)

logger = logging.getLogger(__name__)


class SessionNotFound(Exception):
    """Raised when a session is not found or not accessible."""
    pass


def get_user_session_detail(
    user_id: int,
    session_id: int,
) -> InteractionSession:
    """
    Retrieve a single InteractionSession belonging to the given user.

    Includes safe related objects needed for response serialization
    (terminal_binding, terminal_node).

    Args:
        user_id: The requesting user's ID.
        session_id: The InteractionSession PK.

    Returns:
        The InteractionSession instance.

    Raises:
        SessionNotFound: If session does not exist or does not
                         belong to the user.
    """
    try:
        return (
            InteractionSession.objects
            .select_related(
                "terminal_binding",
                "terminal_binding__terminal_node",
            )
            .get(pk=session_id, user_id=user_id)
        )
    except InteractionSession.DoesNotExist:
        raise SessionNotFound(
            f"Session {session_id} not found for user {user_id}."
        )


def list_authorized_terminal_bindings(
    user_id: int,
) -> QuerySet[TerminalBinding]:
    """
    Return terminal bindings visible to the user through active
    UserToTerminalAuthorization records.

    Filters:
    - authorization not revoked
    - authorization not expired

    Returns a QuerySet of TerminalBinding with terminal_node
    select_related, ordered by node + identifier.
    """
    now = timezone.now()

    authorized_binding_ids = (
        UserToTerminalAuthorization.objects
        .filter(
            user_id=user_id,
            revoked_at__isnull=True,
        )
        .exclude(
            expires_at__isnull=False,
            expires_at__lt=now,
        )
        .values_list("terminal_binding_id", flat=True)
        .distinct()
    )

    return (
        TerminalBinding.objects
        .filter(pk__in=authorized_binding_ids)
        .select_related("terminal_node")
        .order_by("terminal_node", "terminal_identifier")
    )
