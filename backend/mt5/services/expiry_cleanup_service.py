"""
Expiry Cleanup Service.

Finds and terminates expired InteractionSessions via the
canonical terminate path (session_terminate_service).

Designed to be called periodically (e.g., from a management
command or scheduled task).  Does NOT introduce its own
termination logic — delegates entirely to terminate_session().
"""
import logging
from dataclasses import dataclass

from django.db.models import QuerySet
from django.utils import timezone

from mt5.models import InteractionSession
from mt5.services.session_terminate_service import (
    force_terminate_session,
)

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Result of a cleanup run."""

    expired_found: int
    terminated_ok: int
    terminated_failed: int
    errors: list[str]


def find_expired_sessions() -> QuerySet[InteractionSession]:
    """
    Return all InteractionSessions that have expired but are not
    yet ended.

    Criteria:
    - expires_at is set and in the past
    - state is NOT "ended"
    - ended_at is NULL
    """
    now = timezone.now()
    return (
        InteractionSession.objects
        .filter(
            expires_at__isnull=False,
            expires_at__lt=now,
            ended_at__isnull=True,
        )
        .exclude(state="ended")
        .select_related("terminal_binding")
    )


def cleanup_expired_sessions(batch_size: int = 100) -> CleanupResult:
    """
    Find and terminate all expired sessions.

    Uses force_terminate_session to handle sessions that may be
    in unexpected states.

    Args:
        batch_size: Maximum number of sessions to process per run.

    Returns:
        CleanupResult with counts and any errors.
    """
    expired = find_expired_sessions()[:batch_size]
    expired_list = list(expired)

    result = CleanupResult(
        expired_found=len(expired_list),
        terminated_ok=0,
        terminated_failed=0,
        errors=[],
    )

    for session in expired_list:
        try:
            force_terminate_session(
                session=session,
                reason="session_expired",
                actor_user_id=None,  # system-initiated
            )
            result.terminated_ok += 1
        except Exception as e:
            result.terminated_failed += 1
            error_msg = (
                f"Failed to terminate expired session {session.pk}: {e}"
            )
            result.errors.append(error_msg)
            logger.error(error_msg)

    if result.expired_found > 0:
        logger.info(
            "Expiry cleanup complete: found=%d ok=%d failed=%d",
            result.expired_found,
            result.terminated_ok,
            result.terminated_failed,
        )

    return result
