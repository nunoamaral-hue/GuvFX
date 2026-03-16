"""
Session Launch Orchestration Service.

Orchestrates the full launch preparation sequence:
1. Resolve binding
2. Validate authorization (can_launch)
3. Claim occupancy
4. Create InteractionSession + MT5Session records
5. Audit the launch

Does NOT communicate with external adapters — that is the
adapter layer's responsibility (not in scope for Phase 2).
"""
import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from mt5.models import (
    InteractionSession,
    MT5Session,
    TerminalBinding,
    TerminalInteractionAudit,
)
from mt5.services.authorization_validation_service import validate_can_launch
from mt5.services.binding_occupancy_enforcement_service import claim_occupancy
from mt5.services.binding_resolution_service import (
    resolve_binding_by_id,
    validate_binding_launchable,
)

logger = logging.getLogger(__name__)


class LaunchError(Exception):
    """Raised when session launch preparation fails."""
    pass


@dataclass
class LaunchResult:
    """Result of a successful launch orchestration."""

    interaction_session: InteractionSession
    mt5_session: MT5Session
    binding: TerminalBinding


@transaction.atomic
def orchestrate_launch(
    user_id: int,
    binding_id: int,
    launch_descriptor: dict | None = None,
    session_expires_at=None,
) -> LaunchResult:
    """
    Orchestrate the full session launch preparation.

    Args:
        user_id: The user requesting the launch.
        binding_id: The TerminalBinding to launch on.
        launch_descriptor: Optional adapter-specific launch parameters.
        session_expires_at: Optional expiry for the interaction session.

    Returns:
        LaunchResult with created session objects.

    Raises:
        LaunchError: If any step of the orchestration fails.
        BindingResolutionError: If binding cannot be resolved.
        AuthorizationDenied: If user lacks can_launch.
        OccupancyError: If binding cannot be claimed.
    """
    now = timezone.now()

    # Step 1: Resolve and validate binding
    binding = resolve_binding_by_id(binding_id, user_id)
    validate_binding_launchable(binding)

    # Step 2: Validate authorization
    authorization = validate_can_launch(user_id, binding)

    # Step 3: Create InteractionSession
    interaction_session = InteractionSession.objects.create(
        user_id=user_id,
        terminal_binding=binding,
        authorization=authorization,
        state="requested",
        requested_at=now,
    )

    # Step 4: Authorize the session
    interaction_session.state = "authorized"
    interaction_session.authorized_at = now
    interaction_session.expires_at = session_expires_at
    interaction_session.last_activity_at = now
    interaction_session.save(
        update_fields=[
            "state", "authorized_at", "expires_at",
            "last_activity_at", "updated_at",
        ]
    )

    # Step 5: Claim occupancy (transitions binding to LAUNCHING)
    binding = claim_occupancy(binding, interaction_session)

    # Step 6: Create MT5Session
    mt5_session = MT5Session.objects.create(
        interaction_session=interaction_session,
        terminal_binding=binding,
        state="launching",
        launch_issued_at=now,
        launch_descriptor_snapshot=launch_descriptor or {},
    )

    # Step 7: Audit
    _audit_launch(interaction_session, mt5_session, now)

    logger.info(
        "Launch orchestrated: user=%s binding=%s session=%s mt5_session=%s",
        user_id, binding_id, interaction_session.pk, mt5_session.pk,
    )

    return LaunchResult(
        interaction_session=interaction_session,
        mt5_session=mt5_session,
        binding=binding,
    )


def confirm_launch_connected(mt5_session: MT5Session) -> MT5Session:
    """
    Confirm that the MT5 adapter has connected successfully.

    Called by the adapter layer when the connection is established.
    Transitions:
    - MT5Session.state → "connected"
    - InteractionSession.state → "active"
    - TerminalBinding.status → ACTIVE (via occupancy service)
    """
    from mt5.services.binding_occupancy_enforcement_service import activate_occupancy

    now = timezone.now()

    # Update MT5Session
    mt5_session.state = "connected"
    mt5_session.connected_at = now
    mt5_session.last_heartbeat_at = now
    mt5_session.save(
        update_fields=[
            "state", "connected_at", "last_heartbeat_at", "updated_at",
        ]
    )

    # Update InteractionSession
    interaction = mt5_session.interaction_session
    before_state = interaction.state
    interaction.state = "active"
    interaction.started_at = now
    interaction.last_activity_at = now
    interaction.save(
        update_fields=["state", "started_at", "last_activity_at", "updated_at"]
    )

    # Activate occupancy on binding
    activate_occupancy(mt5_session.terminal_binding)

    # Audit
    try:
        TerminalInteractionAudit.objects.create(
            interaction_session=interaction,
            mt5_session=mt5_session,
            actor_user=interaction.user,
            action_type="session_started",
            before_state=before_state,
            after_state="active",
            metadata={
                "adapter_type": mt5_session.adapter_type,
                "adapter_session_id": mt5_session.adapter_session_id,
            },
            timestamp=now,
        )
    except Exception:
        logger.exception(
            "Failed to audit session_started for session=%s", interaction.pk
        )

    logger.info(
        "Launch confirmed connected: mt5_session=%s interaction=%s",
        mt5_session.pk, interaction.pk,
    )
    return mt5_session


def mark_launch_failed(
    mt5_session: MT5Session,
    failure_reason: str = "",
) -> MT5Session:
    """
    Mark a launching MT5Session as failed and release the binding.

    Called when the adapter fails to connect.
    """
    from mt5.services.binding_occupancy_enforcement_service import release_occupancy

    now = timezone.now()

    # Update MT5Session
    mt5_session.state = "failed"
    mt5_session.ended_at = now
    mt5_session.failure_reason = failure_reason
    mt5_session.save(
        update_fields=["state", "ended_at", "failure_reason", "updated_at"]
    )

    # End the InteractionSession
    interaction = mt5_session.interaction_session
    before_state = interaction.state
    interaction.state = "ended"
    interaction.ended_at = now
    interaction.terminated_reason = f"Launch failed: {failure_reason}"
    interaction.save(
        update_fields=["state", "ended_at", "terminated_reason", "updated_at"]
    )

    # Release occupancy
    release_occupancy(
        mt5_session.terminal_binding,
        reason=f"launch_failed: {failure_reason}",
    )

    # Audit
    try:
        TerminalInteractionAudit.objects.create(
            interaction_session=interaction,
            mt5_session=mt5_session,
            actor_user=interaction.user,
            action_type="launch_failed",
            before_state=before_state,
            after_state="ended",
            metadata={"failure_reason": failure_reason},
            timestamp=now,
        )
    except Exception:
        logger.exception(
            "Failed to audit launch_failed for session=%s", interaction.pk
        )

    logger.info(
        "Launch failed: mt5_session=%s reason=%s",
        mt5_session.pk, failure_reason,
    )
    return mt5_session


def _audit_launch(
    session: InteractionSession,
    mt5_session: MT5Session,
    timestamp,
) -> None:
    """Audit the launch orchestration."""
    try:
        TerminalInteractionAudit.objects.create(
            interaction_session=session,
            mt5_session=mt5_session,
            actor_user=session.user,
            action_type="session_launch_orchestrated",
            before_state="",
            after_state="authorized",
            metadata={
                "binding_id": session.terminal_binding_id,
                "authorization_id": session.authorization_id,
            },
            timestamp=timestamp,
        )
    except Exception:
        logger.exception(
            "Failed to audit session_launch_orchestrated for session=%s",
            session.pk,
        )
