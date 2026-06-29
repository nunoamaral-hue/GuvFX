"""
Binding Occupancy Enforcement Service.

Centralizes all occupancy claim and release operations on
TerminalBinding.  No other code should directly mutate
occupied_by_session / occupied_since / status for occupancy
transitions.

Uses select_for_update() to prevent race conditions on
concurrent claim attempts.
"""
import logging

from django.db import transaction
from django.utils import timezone

from mt5.models import InteractionSession, TerminalBinding, TerminalInteractionAudit

logger = logging.getLogger(__name__)


class OccupancyError(Exception):
    """Raised when an occupancy operation fails."""
    pass


@transaction.atomic
def claim_occupancy(
    binding: TerminalBinding,
    session: InteractionSession,
) -> TerminalBinding:
    """
    Claim occupancy of a TerminalBinding for the given session.

    Preconditions (enforced):
    - binding.status == AVAILABLE
    - binding.occupied_by_session is None

    Effects:
    - binding.occupied_by_session = session
    - binding.occupied_since = now
    - binding.status = LAUNCHING

    Returns the updated binding.
    Raises OccupancyError if preconditions are not met.
    """
    # Re-fetch with row lock to prevent races
    locked_binding = (
        TerminalBinding.objects
        .select_for_update()
        .get(pk=binding.pk)
    )

    if locked_binding.status != TerminalBinding.Status.AVAILABLE:
        raise OccupancyError(
            f"Cannot claim binding {binding.pk}: status is "
            f"{locked_binding.status}, expected available."
        )

    if locked_binding.occupied_by_session_id is not None:
        raise OccupancyError(
            f"Cannot claim binding {binding.pk}: already occupied "
            f"by session {locked_binding.occupied_by_session_id}."
        )

    now = timezone.now()
    locked_binding.occupied_by_session = session
    locked_binding.occupied_since = now
    locked_binding.status = TerminalBinding.Status.LAUNCHING
    locked_binding.save(
        update_fields=["occupied_by_session", "occupied_since", "status", "updated_at"]
    )

    _audit_occupancy_change(
        session=session,
        action_type="occupancy_claimed",
        before_status=TerminalBinding.Status.AVAILABLE,
        after_status=TerminalBinding.Status.LAUNCHING,
        timestamp=now,
    )

    logger.info(
        "Occupancy claimed: binding=%s session=%s",
        binding.pk, session.pk,
    )
    return locked_binding


@transaction.atomic
def activate_occupancy(binding: TerminalBinding) -> TerminalBinding:
    """
    Transition a LAUNCHING binding to ACTIVE.

    Called when the MT5 session is confirmed connected.
    """
    locked_binding = (
        TerminalBinding.objects
        .select_for_update()
        .get(pk=binding.pk)
    )

    if locked_binding.status != TerminalBinding.Status.LAUNCHING:
        raise OccupancyError(
            f"Cannot activate binding {binding.pk}: status is "
            f"{locked_binding.status}, expected launching."
        )

    now = timezone.now()
    before = locked_binding.status
    locked_binding.status = TerminalBinding.Status.ACTIVE
    locked_binding.last_heartbeat = now
    locked_binding.save(
        update_fields=["status", "last_heartbeat", "updated_at"]
    )

    if locked_binding.occupied_by_session:
        _audit_occupancy_change(
            session=locked_binding.occupied_by_session,
            action_type="occupancy_activated",
            before_status=before,
            after_status=TerminalBinding.Status.ACTIVE,
            timestamp=now,
        )

    logger.info("Occupancy activated: binding=%s", binding.pk)
    return locked_binding


@transaction.atomic
def release_occupancy(
    binding: TerminalBinding,
    reason: str = "",
) -> TerminalBinding:
    """
    Release occupancy of a TerminalBinding, returning it to AVAILABLE.

    This is the ONLY path to clear occupied_by_session.

    Effects:
    - binding.occupied_by_session = None
    - binding.occupied_since = None
    - binding.status = AVAILABLE
    """
    locked_binding = (
        TerminalBinding.objects
        .select_for_update()
        .get(pk=binding.pk)
    )

    before_status = locked_binding.status
    session = locked_binding.occupied_by_session
    now = timezone.now()

    locked_binding.occupied_by_session = None
    locked_binding.occupied_since = None
    locked_binding.status = TerminalBinding.Status.AVAILABLE
    locked_binding.save(
        update_fields=[
            "occupied_by_session", "occupied_since", "status", "updated_at",
        ]
    )

    if session:
        _audit_occupancy_change(
            session=session,
            action_type="occupancy_released",
            before_status=before_status,
            after_status=TerminalBinding.Status.AVAILABLE,
            timestamp=now,
            metadata={"reason": reason} if reason else {},
        )

    logger.info(
        "Occupancy released: binding=%s reason=%s",
        binding.pk, reason or "(none)",
    )
    return locked_binding


def _audit_occupancy_change(
    session: InteractionSession,
    action_type: str,
    before_status: str,
    after_status: str,
    timestamp=None,
    metadata: dict | None = None,
) -> None:
    """Write a TerminalInteractionAudit entry for an occupancy change."""
    try:
        TerminalInteractionAudit.objects.create(
            interaction_session=session,
            actor_user=session.user,
            action_type=action_type,
            before_state=str(before_status),
            after_state=str(after_status),
            metadata=metadata or {},
            timestamp=timestamp or timezone.now(),
        )
    except Exception:
        logger.exception(
            "Failed to write occupancy audit for session=%s action=%s",
            session.pk, action_type,
        )
