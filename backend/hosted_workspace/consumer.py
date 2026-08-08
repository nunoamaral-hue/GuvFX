"""ADR-0034 / M3c — the Workspace persistence CONSUMER (safe orchestration, DARK).

The thin orchestration seam between an observation and the authoritative writer. Given a freshly-produced
``WorkspaceObservation`` (the M3b agent/producer output — health signals only), it:

  1. reads the current authoritative canonical state (so the manager decides against the real premise),
  2. asks the M3a Manager for the decision (the SOLE state-deriver — nothing here invents lifecycle logic),
  3. hands the decision to the SINGLE authoritative writer (``persist_workspace_decision``).

It does NOT attach, launch, login, place/size/approve any order, poll a host, or emit telemetry itself. It
is DARK: gated behind the master ``hosted_persistent_mt5_enabled()`` flag and — critically — WIRED BY NO
PRODUCTION CALLER in this increment. When the flag is OFF it is a no-op returning ``None`` (mirrors
``operational_events.record_event``'s dark contract).

On the small unlocked read-then-derive window here: the writer re-validates the transition's LEGALITY against
the row it LOCKS, so a raced decision that would be ILLEGAL from the true current state is rejected
(REJECTED_ILLEGAL, stored state held), and the observation-version guard rejects an out-of-order one
(REJECTED_STALE). A raced decision whose target happens to remain LEGAL from the true state may still apply;
because canonical state is display-only (never the order gate), that at worst records a neighbouring-state
transition which the next observation corrects — it can never corrupt the order path.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Optional

from hosted_workspace.flags import hosted_persistent_mt5_enabled
from hosted_workspace.manager import WorkspaceObservation, derive_workspace_decision
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.persistence import PersistResult, persist_workspace_decision

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.consumer"


def ingest_observation(
    workspace: HostedMt5Workspace,
    observation: WorkspaceObservation,
    *,
    observation_version: int,
    correlation_id: str = "",
    source: str = SOURCE,
) -> Optional[PersistResult]:
    """Derive the decision for ``observation`` against the workspace's current authoritative state and
    persist it. Returns the writer's ``PersistResult``, or ``None`` when the subsystem is DARK
    (``hosted_persistent_mt5_enabled()`` OFF) — a pure no-op that touches nothing.

    ``observation`` supplies health signals only; its ``previous_state``/``previous_reason`` are OVERRIDDEN
    with the persisted canonical state, so the manager can never decide against a caller-supplied premise.
    """
    if not hosted_persistent_mt5_enabled():
        return None  # DARK: subsystem disabled -> no derivation, no write, no telemetry.

    # Refresh the authoritative premise from the row (the passed instance may be a stale in-memory copy).
    stored = None
    if workspace.pk is not None:
        stored = (HostedMt5Workspace.objects
                  .filter(pk=workspace.pk)
                  .values("canonical_state", "canonical_reason").first())
    if stored is not None:
        prev_state = str(stored["canonical_state"])
        prev_reason = str(stored["canonical_reason"])
    else:
        prev_state = str(workspace.canonical_state)
        prev_reason = str(workspace.canonical_reason)

    obs = dataclasses.replace(observation, previous_state=prev_state, previous_reason=prev_reason)
    decision = derive_workspace_decision(obs)
    return persist_workspace_decision(
        workspace, obs, decision, observation_version=observation_version,
        correlation_id=correlation_id, source=source)
