"""
Session Resume Service.

Locates and validates resumable InteractionSessions for a user,
returning validated domain context only.

A session is resumable if:
- state is "active" (started but not ended)
- the binding is still occupied by that session
- the user has can_resume authorization
- the session has not expired

This service does NOT create MT5Sessions, end existing sessions,
or perform any lifecycle mutation.  Adapter reconnection logic
is out of scope for Phase 2.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone

from mt5.models import (
    InteractionSession,
    MT5Session,
    TerminalBinding,
    UserToTerminalAuthorization,
)
from mt5.services.authorization_validation_service import (
    AuthorizationDenied,
    validate_can_resume,
)

logger = logging.getLogger(__name__)


class ResumeError(Exception):
    """Raised when session resume validation fails."""
    pass


@dataclass
class ResumableContext:
    """Validated resumable session context returned by resolve_resumable."""

    interaction_session: InteractionSession
    terminal_binding: TerminalBinding
    authorization: UserToTerminalAuthorization
    latest_mt5_session: MT5Session | None


def find_resumable_sessions(user_id: int) -> QuerySet[InteractionSession]:
    """
    Return all InteractionSessions for a user that are potentially
    resumable (state == "active", not expired, not ended).
    """
    now = timezone.now()
    return (
        InteractionSession.objects
        .filter(
            user_id=user_id,
            state="active",
            ended_at__isnull=True,
        )
        .exclude(
            expires_at__isnull=False,
            expires_at__lt=now,
        )
        .select_related("terminal_binding", "authorization")
        .order_by("-started_at")
    )


def validate_resumable(session: InteractionSession) -> None:
    """
    Validate that a specific session is in a resumable state.

    Raises ResumeError if the session is not resumable.
    """
    if session.state != "active":
        raise ResumeError(
            f"Session {session.pk} is not active (state: {session.state})."
        )

    if session.ended_at is not None:
        raise ResumeError(
            f"Session {session.pk} has already ended."
        )

    # Check expiry
    now = timezone.now()
    if session.expires_at and session.expires_at < now:
        raise ResumeError(
            f"Session {session.pk} has expired "
            f"(expired at {session.expires_at})."
        )

    # Check binding still occupied by this session
    binding = session.terminal_binding
    if binding.occupied_by_session_id != session.pk:
        raise ResumeError(
            f"Session {session.pk}: binding {binding.pk} is no longer "
            f"occupied by this session."
        )


def resolve_resumable(
    session: InteractionSession,
    user_id: int,
) -> ResumableContext:
    """
    Validate and resolve the full resumable context for a session.

    Checks:
    - session ownership
    - session resumability (state, expiry, occupancy)
    - user has can_resume authorization on the binding

    Returns:
        ResumableContext with validated domain objects.

    Raises:
        ResumeError: If session cannot be resumed.
        AuthorizationDenied: If user lacks can_resume.
    """
    # Validate ownership
    if session.user_id != user_id:
        raise ResumeError(
            f"User {user_id} does not own session {session.pk}."
        )

    # Validate resumability
    validate_resumable(session)

    # Validate authorization
    authorization = validate_can_resume(user_id, session.terminal_binding)

    # Locate the latest MT5Session (read-only context)
    latest_mt5 = (
        MT5Session.objects
        .filter(interaction_session=session)
        .order_by("-created_at")
        .first()
    )

    logger.info(
        "Resumable context resolved: session=%s user=%s binding=%s",
        session.pk, user_id, session.terminal_binding_id,
    )

    return ResumableContext(
        interaction_session=session,
        terminal_binding=session.terminal_binding,
        authorization=authorization,
        latest_mt5_session=latest_mt5,
    )
