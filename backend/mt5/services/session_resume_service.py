"""
Session Resume Service.

Locates and validates resumable InteractionSessions for a user,
then prepares them for reconnection.

A session is resumable if:
- state is "active" (started but not ended)
- the binding is still occupied by that session
- the user has can_resume authorization
- the session has not expired
"""
import logging
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone

from mt5.models import (
    InteractionSession,
    MT5Session,
    TerminalBinding,
    TerminalInteractionAudit,
)
from mt5.services.authorization_validation_service import (
    AuthorizationDenied,
    validate_can_resume,
)

logger = logging.getLogger(__name__)


class ResumeError(Exception):
    """Raised when session resume fails."""
    pass


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
    Validate that a specific session can be resumed.

    Raises ResumeError if the session is not in a resumable state.
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


def resume_session(
    session: InteractionSession,
    user_id: int,
) -> MT5Session:
    """
    Resume an existing InteractionSession.

    Validates resumability, authorization, then creates a new
    MT5Session for the reconnection.

    Args:
        session: The InteractionSession to resume.
        user_id: The user requesting the resume.

    Returns:
        A new MT5Session in "launching" state.

    Raises:
        ResumeError: If session cannot be resumed.
        AuthorizationDenied: If user lacks can_resume.
    """
    # Validate the session is resumable
    validate_resumable(session)

    # Validate authorization
    if session.user_id != user_id:
        raise ResumeError(
            f"User {user_id} does not own session {session.pk}."
        )

    validate_can_resume(user_id, session.terminal_binding)

    now = timezone.now()

    # End any existing MT5Sessions in non-terminal states
    _end_stale_mt5_sessions(session, now)

    # Create new MT5Session for the resume
    mt5_session = MT5Session.objects.create(
        interaction_session=session,
        terminal_binding=session.terminal_binding,
        state="launching",
        launch_issued_at=now,
        launch_descriptor_snapshot=_get_last_descriptor(session),
    )

    # Update activity timestamp
    session.last_activity_at = now
    session.save(update_fields=["last_activity_at", "updated_at"])

    # Audit
    try:
        TerminalInteractionAudit.objects.create(
            interaction_session=session,
            mt5_session=mt5_session,
            actor_user_id=user_id,
            action_type="session_resumed",
            before_state="active",
            after_state="active",
            metadata={"new_mt5_session_id": mt5_session.pk},
            timestamp=now,
        )
    except Exception:
        logger.exception(
            "Failed to audit session_resumed for session=%s", session.pk
        )

    logger.info(
        "Session resumed: session=%s user=%s new_mt5_session=%s",
        session.pk, user_id, mt5_session.pk,
    )
    return mt5_session


def _end_stale_mt5_sessions(
    session: InteractionSession,
    now,
) -> int:
    """
    End any MT5Sessions for this InteractionSession that are still
    in non-terminal states (launching, connected, suspended).

    Returns the number of sessions ended.
    """
    stale = MT5Session.objects.filter(
        interaction_session=session,
        state__in=["launching", "connected", "suspended"],
    )
    count = stale.update(
        state="ended",
        ended_at=now,
        failure_reason="ended_for_resume",
    )
    if count:
        logger.info(
            "Ended %d stale MT5Sessions for session=%s before resume",
            count, session.pk,
        )
    return count


def _get_last_descriptor(session: InteractionSession) -> dict:
    """
    Get the launch descriptor from the most recent MT5Session,
    to reuse for the resume.
    """
    last = (
        MT5Session.objects
        .filter(interaction_session=session)
        .order_by("-created_at")
        .values_list("launch_descriptor_snapshot", flat=True)
        .first()
    )
    return last or {}
