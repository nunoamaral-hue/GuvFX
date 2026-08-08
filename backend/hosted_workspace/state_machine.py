"""ADR-0034 / M2a — the single authoritative Workspace lifecycle state machine (DARK foundation).

The canonical states + legal transitions from ADR-0034 §3. This is the one model every hosted-workspace
subsystem must eventually consume, instead of inventing its own lifecycle semantics. This increment ships
the model + a pure, fail-closed transition-legality decision + a legacy→canonical mapping — additive and
inert (no model field change, no migration, no consumer wired). Conditions such as account-mismatch or
IPC failure are **reason codes** attached to a canonical state, not competing top-level states (ADR-0034 §4).
"""
from django.db import models


class WorkspaceLifecycleState(models.TextChoices):
    """Canonical top-level Workspace lifecycle states (ADR-0034 §3). Exactly nine; every subsystem consumes
    these rather than an independent interpretation."""
    PROVISIONING = "PROVISIONING", "Provisioning"
    WAITING_FOR_LOGIN = "WAITING_FOR_LOGIN", "Waiting for login"
    CONNECTED = "CONNECTED", "Connected"
    EXECUTION_READY = "EXECUTION_READY", "Execution ready"
    EXECUTING = "EXECUTING", "Executing"
    DISCONNECTED = "DISCONNECTED", "Disconnected"
    RECOVERING = "RECOVERING", "Recovering"
    SUSPENDED = "SUSPENDED", "Suspended"
    RETIRED = "RETIRED", "Retired"


class WorkspaceReason(models.TextChoices):
    """Reason / sub-state codes (ADR-0034 §4) — attached to a canonical state, never a lifecycle state of
    their own."""
    NONE = "NONE", "None"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH", "Active account mismatch"
    IPC_FAILURE = "IPC_FAILURE", "IPC failure"
    BROKER_AUTH_FAILURE = "BROKER_AUTH_FAILURE", "Broker authentication failure"
    DEGRADED = "DEGRADED", "Degraded"
    STALE_OBSERVATION = "STALE_OBSERVATION", "Stale observation"
    OPERATOR_PAUSE = "OPERATOR_PAUSE", "Operator pause"
    ERROR = "ERROR", "Error"


# Legal transitions — the ADR-0034 §3 state diagram, verbatim (from -> allowed set). RETIRED is terminal.
# Same-state (from == to) is treated as an idempotent no-op by evaluate_workspace_transition (a re-observation
# confirming the current state is not a transition). NOTE: this is the approved §3 graph as drawn; extending
# it (e.g. WaitingForLogin -> Disconnected) is an ADR refinement, not taken here.
WORKSPACE_TRANSITIONS = {
    WorkspaceLifecycleState.PROVISIONING: {
        WorkspaceLifecycleState.WAITING_FOR_LOGIN, WorkspaceLifecycleState.RETIRED},
    WorkspaceLifecycleState.WAITING_FOR_LOGIN: {WorkspaceLifecycleState.CONNECTED},
    WorkspaceLifecycleState.CONNECTED: {
        WorkspaceLifecycleState.EXECUTION_READY, WorkspaceLifecycleState.DISCONNECTED,
        WorkspaceLifecycleState.SUSPENDED},
    WorkspaceLifecycleState.EXECUTION_READY: {
        WorkspaceLifecycleState.EXECUTING, WorkspaceLifecycleState.CONNECTED,
        WorkspaceLifecycleState.DISCONNECTED, WorkspaceLifecycleState.SUSPENDED},
    WorkspaceLifecycleState.EXECUTING: {WorkspaceLifecycleState.EXECUTION_READY},
    WorkspaceLifecycleState.DISCONNECTED: {
        WorkspaceLifecycleState.RECOVERING, WorkspaceLifecycleState.RETIRED},
    WorkspaceLifecycleState.RECOVERING: {WorkspaceLifecycleState.CONNECTED},
    WorkspaceLifecycleState.SUSPENDED: {
        WorkspaceLifecycleState.CONNECTED, WorkspaceLifecycleState.RETIRED},
    WorkspaceLifecycleState.RETIRED: set(),
}


def evaluate_workspace_transition(from_state, to_state):
    """Pure, fail-closed: is ``from_state -> to_state`` a legal Workspace lifecycle transition (ADR-0034 §3)?

    Returns ``(allowed: bool, reason: str)``. An unknown state on either side is rejected (``unknown_state``);
    a same-state move is an idempotent no-op (``idempotent``); otherwise legality is the §3 graph
    (``ok`` / ``illegal_transition``). Accepts either the TextChoices members or their string values (the
    members are ``str`` subclasses, so set/dict membership matches both).
    """
    if from_state not in WORKSPACE_TRANSITIONS or to_state not in WORKSPACE_TRANSITIONS:
        return False, "unknown_state"
    if from_state == to_state:
        return True, "idempotent"
    if to_state in WORKSPACE_TRANSITIONS[from_state]:
        return True, "ok"
    return False, "illegal_transition"


# Legacy hosted_workspace.WorkspaceState value -> (canonical state, reason). Fail-closed default (below)
# never maps an unrecognised value to a live/execution state.
_LEGACY_TO_CANONICAL = {
    "NOT_PROVISIONED": (WorkspaceLifecycleState.PROVISIONING, WorkspaceReason.NONE),
    "PROVISIONING": (WorkspaceLifecycleState.PROVISIONING, WorkspaceReason.NONE),
    "AWAITING_USER_LOGIN": (WorkspaceLifecycleState.WAITING_FOR_LOGIN, WorkspaceReason.NONE),
    "CONNECTED": (WorkspaceLifecycleState.CONNECTED, WorkspaceReason.NONE),
    "ACTIVE_ACCOUNT_MISMATCH": (WorkspaceLifecycleState.SUSPENDED, WorkspaceReason.ACCOUNT_MISMATCH),
    "DISCONNECTED": (WorkspaceLifecycleState.DISCONNECTED, WorkspaceReason.NONE),
    "DEGRADED": (WorkspaceLifecycleState.RECOVERING, WorkspaceReason.DEGRADED),
    "STOPPED": (WorkspaceLifecycleState.SUSPENDED, WorkspaceReason.OPERATOR_PAUSE),
    "ERROR": (WorkspaceLifecycleState.SUSPENDED, WorkspaceReason.ERROR),
}


def to_canonical(legacy_state):
    """Map a legacy ``hosted_workspace.models.WorkspaceState`` value to ``(canonical state, reason)``.
    FAIL-CLOSED: an unknown legacy value maps to ``(SUSPENDED, ERROR)`` — never silently to an execution
    state (EXECUTION_READY / EXECUTING)."""
    return _LEGACY_TO_CANONICAL.get(
        str(legacy_state), (WorkspaceLifecycleState.SUSPENDED, WorkspaceReason.ERROR))
