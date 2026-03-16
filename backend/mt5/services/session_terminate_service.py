"""
Session Terminate Service.

Terminates an InteractionSession and its active MT5Sessions,
then releases occupancy on the TerminalBinding.

This is the canonical termination path — used by user-initiated
disconnect, admin force-terminate, and expiry cleanup.
"""
import logging

from django.db import transaction
from django.utils import timezone

from mt5.models import (
    InteractionSession,
    MT5Session,
    TerminalInteractionAudit,
)
from mt5.services.binding_occupancy_enforcement_service import release_occupancy

logger = logging.getLogger(__name__)


class TerminateError(Exception):
    """Raised when session termination fails."""
    pass


@transaction.atomic
def terminate_session(
    session: InteractionSession,
    reason: str = "",
    actor_user_id: int | None = None,
) -> InteractionSession:
    """
    Terminate an InteractionSession.

    Steps:
    1. End all non-terminal MT5Sessions
    2. Set InteractionSession state to "ended"
    3. Release binding occupancy

    Args:
        session: The InteractionSession to terminate.
        reason: Human-readable reason for termination.
        actor_user_id: The user who initiated termination
                       (None for system-initiated, e.g. expiry).

    Returns:
        The updated InteractionSession.

    Raises:
        TerminateError if session is already ended.
    """
    if session.state == "ended" and session.ended_at is not None:
        raise TerminateError(
            f"Session {session.pk} is already terminated."
        )

    now = timezone.now()
    before_state = session.state

    # Step 1: End all non-terminal MT5Sessions
    ended_count = _end_active_mt5_sessions(session, now, reason)

    # Step 2: Update InteractionSession
    session.state = "ended"
    session.ended_at = now
    session.terminated_reason = reason
    session.last_activity_at = now
    session.save(
        update_fields=[
            "state", "ended_at", "terminated_reason",
            "last_activity_at", "updated_at",
        ]
    )

    # Step 3: Release binding occupancy (only if binding is occupied by us)
    binding = session.terminal_binding
    if binding.occupied_by_session_id == session.pk:
        release_occupancy(binding, reason=reason)

    # Audit
    _audit_termination(
        session=session,
        before_state=before_state,
        reason=reason,
        actor_user_id=actor_user_id,
        ended_mt5_count=ended_count,
        timestamp=now,
    )

    logger.info(
        "Session terminated: session=%s reason=%s actor=%s mt5_ended=%d",
        session.pk, reason or "(none)",
        actor_user_id or "system", ended_count,
    )
    return session


def force_terminate_session(
    session: InteractionSession,
    reason: str = "admin_force_terminate",
    actor_user_id: int | None = None,
) -> InteractionSession:
    """
    Force-terminate a session regardless of current state.

    Same as terminate_session but skips the already-ended check.
    Used for admin force-terminate and cleanup of stuck sessions.
    """
    now = timezone.now()
    before_state = session.state

    # End all non-terminal MT5Sessions
    ended_count = _end_active_mt5_sessions(session, now, reason)

    # Update InteractionSession
    session.state = "ended"
    session.ended_at = now
    session.terminated_reason = reason
    session.last_activity_at = now
    session.save(
        update_fields=[
            "state", "ended_at", "terminated_reason",
            "last_activity_at", "updated_at",
        ]
    )

    # Release binding occupancy
    binding = session.terminal_binding
    if binding.occupied_by_session_id == session.pk:
        release_occupancy(binding, reason=reason)

    # Audit
    _audit_termination(
        session=session,
        before_state=before_state,
        reason=reason,
        actor_user_id=actor_user_id,
        ended_mt5_count=ended_count,
        timestamp=now,
        force=True,
    )

    logger.info(
        "Session force-terminated: session=%s reason=%s actor=%s",
        session.pk, reason, actor_user_id or "system",
    )
    return session


def _end_active_mt5_sessions(
    session: InteractionSession,
    now,
    reason: str,
) -> int:
    """
    End all MT5Sessions in non-terminal states for this
    InteractionSession.

    Returns count of sessions ended.
    """
    active_mt5 = MT5Session.objects.filter(
        interaction_session=session,
        state__in=["launching", "connected", "suspended"],
    )
    count = active_mt5.update(
        state="ended",
        ended_at=now,
        failure_reason=reason,
    )
    return count


def _audit_termination(
    session: InteractionSession,
    before_state: str,
    reason: str,
    actor_user_id: int | None,
    ended_mt5_count: int,
    timestamp,
    force: bool = False,
) -> None:
    """Write audit entry for session termination."""
    try:
        action_type = "session_force_terminated" if force else "session_terminated"
        TerminalInteractionAudit.objects.create(
            interaction_session=session,
            actor_user_id=actor_user_id or session.user_id,
            action_type=action_type,
            before_state=before_state,
            after_state="ended",
            metadata={
                "reason": reason,
                "ended_mt5_sessions": ended_mt5_count,
                "actor_user_id": actor_user_id,
            },
            timestamp=timestamp,
        )
    except Exception:
        logger.exception(
            "Failed to audit termination for session=%s", session.pk
        )
