"""ADR-0034 / M3a — the Workspace Manager decision engine (pure, deterministic, DARK).

The Workspace Manager is the ONE authoritative component that derives canonical Workspace state. Everything
else is an observation *producer*; nothing else invents lifecycle semantics. This engine is pure and
side-effect free: it does NOT talk to Windows or MT5, attach, persist, cache, emit telemetry, or execute.
It answers exactly one question — *given the latest observation, what should the workspace state become?* —
and returns a decision object describing the answer (including which telemetry event SHOULD occur; emission
belongs to a later increment).

Two hard invariants:
- **EXECUTION_READY is derived only when ALL of {attached, connected, active-account match, observation
  fresh, trade allowed} hold** — otherwise fail closed (`_all_execution_conditions`).
- **Every state change is validated against the M2 canonical state machine** (`evaluate_workspace_transition`);
  an illegal or unknown transition fails closed to *no transition* (hold the previous state, flag the anomaly).
"""
from dataclasses import dataclass
from typing import Optional

from hosted_workspace.state_machine import (
    WorkspaceLifecycleState as S,
    WorkspaceReason,
    evaluate_workspace_transition,
)
from hosted_workspace.telemetry import WorkspaceEvent


@dataclass(frozen=True)
class WorkspaceObservation:
    """The manager's input — health signals only. `account_match`/`fresh` are pre-evaluated by the
    observation producer so the manager stays clock-free and deterministic (`observed_at` is carried for
    audit, never used in the decision). `previous_state`/`previous_reason` are the canonical string values."""
    process_running: bool
    ipc_available: bool
    connected: bool
    account_match: bool
    trade_allowed: bool
    fresh: bool
    previous_state: str
    previous_reason: str = WorkspaceReason.NONE
    observed_at: Optional[int] = None


@dataclass(frozen=True)
class WorkspaceDecision:
    """The manager's output — a description, never an action."""
    next_state: str
    reason: str
    transition_required: bool
    telemetry_event: Optional[str]
    execution_ready: bool
    recovery_required: bool


# Canonical state -> the WorkspaceEvent that SHOULD be emitted on entering it (or None). EXECUTING is
# order-driven, not observation-derived, so it never appears here; RETIRED has no entry event.
_EVENT_FOR_STATE = {
    S.PROVISIONING: WorkspaceEvent.CREATED,
    S.WAITING_FOR_LOGIN: WorkspaceEvent.WAITING_FOR_LOGIN,
    S.CONNECTED: WorkspaceEvent.CONNECTED,
    S.EXECUTION_READY: WorkspaceEvent.EXECUTION_READY,
    S.DISCONNECTED: WorkspaceEvent.DISCONNECTED,
    S.RECOVERING: WorkspaceEvent.RECOVERING,
    S.SUSPENDED: WorkspaceEvent.EXECUTION_PAUSED,
}


def _all_execution_conditions(obs):
    """The EXECUTION_READY gate — the safety-critical conjunction. True ONLY when every condition holds."""
    return (obs.process_running and obs.ipc_available and obs.connected
            and obs.account_match and obs.fresh and obs.trade_allowed)


def _target_for(obs):
    """Deterministic observation -> (target canonical state, reason). Never yields EXECUTION_READY unless
    `_all_execution_conditions` holds. Uses the previous state only to disambiguate the still-in-login vs
    link-lost cases; the transition is graph-validated afterwards."""
    attached = obs.process_running and obs.ipc_available
    if _all_execution_conditions(obs):
        return S.EXECUTION_READY, WorkspaceReason.NONE
    if attached and obs.connected:
        if not obs.account_match:
            return S.SUSPENDED, WorkspaceReason.ACCOUNT_MISMATCH
        if not obs.fresh:
            return S.CONNECTED, WorkspaceReason.STALE_OBSERVATION
        return S.CONNECTED, WorkspaceReason.NONE  # connected+matched+fresh but trading halted: not ready
    if attached and not obs.connected:
        if obs.previous_state in (S.PROVISIONING, S.WAITING_FOR_LOGIN):
            return S.WAITING_FOR_LOGIN, WorkspaceReason.NONE
        return S.DISCONNECTED, WorkspaceReason.BROKER_AUTH_FAILURE
    # not attached
    if obs.previous_state == S.PROVISIONING:
        return S.WAITING_FOR_LOGIN, WorkspaceReason.NONE
    if obs.previous_state == S.DISCONNECTED:
        return S.RECOVERING, WorkspaceReason.NONE
    if obs.previous_state in (S.WAITING_FOR_LOGIN, S.RECOVERING, S.SUSPENDED, S.RETIRED):
        return obs.previous_state, obs.previous_reason  # hold (legal same-state); no forced illegal move
    return S.DISCONNECTED, (WorkspaceReason.IPC_FAILURE if obs.process_running else WorkspaceReason.NONE)


def derive_workspace_decision(obs):
    """Pure decision: given `obs`, what should the workspace state become? Returns a WorkspaceDecision and
    performs NO action. Fail-closed: an illegal/unknown transition holds the previous state; EXECUTION_READY
    additionally requires a legal predecessor (CONNECTED/EXECUTING) per the M2 graph, so the conditions gate
    AND the transition gate must both pass."""
    target, reason = _target_for(obs)
    prev = obs.previous_state

    if prev == target:
        transition_required = False
    else:
        allowed, _ = evaluate_workspace_transition(prev, target)
        if allowed:
            transition_required = True
        else:
            # Illegal or unknown transition -> fail closed: hold previous state, flag the anomaly.
            target, reason = prev, WorkspaceReason.ERROR
            transition_required = False

    execution_ready = (target == S.EXECUTION_READY)
    recovery_required = target in (S.DISCONNECTED, S.RECOVERING)
    telemetry_event = None
    if transition_required:
        event = _EVENT_FOR_STATE.get(target)
        telemetry_event = str(event) if event is not None else None

    return WorkspaceDecision(
        next_state=str(target),
        reason=str(reason),
        transition_required=transition_required,
        telemetry_event=telemetry_event,
        execution_ready=execution_ready,
        recovery_required=recovery_required,
    )
