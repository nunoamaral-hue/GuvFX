"""hosted_workspace.onboarding_read_model — ADR-0034 Onboarding customer-journey projection (DARK).

A PURE, secret-free projection that turns the durable workspace/account truth into the customer-facing
onboarding phase + next action + a degrade-closed delivery-readiness signal. It reads authoritative state
only (canonical_state, proj_account_match, execution_node, workspace_confirmed_at) — it NEVER derives
readiness independently and NEVER exposes a credential, a full login (masked), an attach path, a node id
(staff only), or a stack trace. Phase names are stable customer-facing identifiers.
"""
from __future__ import annotations

from hosted_workspace.flags import hosted_mt5_remoteapp_enabled
from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason

# Customer-facing journey phases (ADR-0034 Onboarding PART G), stable identifiers.
PHASE_NO_WORKSPACE = "NO_WORKSPACE"
PHASE_WORKSPACE_REQUESTED = "WORKSPACE_REQUESTED"
PHASE_WORKSPACE_PREPARING = "WORKSPACE_PREPARING"
PHASE_AWAITING_BROKER_LOGIN = "AWAITING_BROKER_LOGIN"
PHASE_BROKER_CONNECTED = "BROKER_CONNECTED"
PHASE_ACCOUNT_CONFIRMATION_REQUIRED = "ACCOUNT_CONFIRMATION_REQUIRED"
PHASE_ACCOUNT_BOUND = "ACCOUNT_BOUND"
PHASE_WORKSPACE_READY = "WORKSPACE_READY"
PHASE_WORKSPACE_UNAVAILABLE = "WORKSPACE_UNAVAILABLE"

# Next-action hints (what the customer should do now). Stable identifiers, not copy.
NEXT_REQUEST_WORKSPACE = "request_workspace"
NEXT_WAIT = "wait"
NEXT_OPEN_MT5_AND_LOGIN = "open_mt5_and_log_in"
NEXT_CONFIRM_ACCOUNT = "confirm_broker_account"
NEXT_ASSIGN_STRATEGY = "assign_strategy"
NEXT_CONTACT_SUPPORT = "contact_support"

# Delivery-readiness (degrade-closed). #316 (delivery_state/workspace_node) is not on this branch, so this
# projects conservatively: OFF ⇒ NOT_AVAILABLE; ON ⇒ EXTERNAL_GATE (RDS host activation is an un-begun
# Sponsor/host change). When #316 lands, read HostedMt5Workspace.delivery_state here instead.
DELIVERY_NOT_AVAILABLE = "DELIVERY_NOT_AVAILABLE"
DELIVERY_PREPARING = "DELIVERY_PREPARING"
DELIVERY_READY = "DELIVERY_READY"
DELIVERY_EXTERNAL_GATE = "DELIVERY_EXTERNAL_GATE"

_CONNECTED_STATES = (S.CONNECTED, S.EXECUTION_READY, S.EXECUTING)
_DEGRADED_STATES = (S.DISCONNECTED, S.RECOVERING, S.SUSPENDED, S.RETIRED)


def _mask(login: str) -> str:
    login = str(login or "")
    return ("***" + login[-3:]) if login else ""


def delivery_readiness(workspace) -> str:
    """Degrade-closed delivery-readiness projection, reconciled with the merged Workspace Delivery (#316).
    It now reads the REAL ``HostedMt5Workspace.delivery_state`` (owned by the delivery single writer) but
    never fabricates readiness: only a genuinely CONNECTED RemoteApp is READY. Because the actual RemoteApp
    host (RDS) is a Sponsor/host gate that is NOT installed, an undelivered-but-flagged workspace reports
    EXTERNAL_GATE (host-pending), not READY — the honest DARK state. Delivery is NEVER execution: this signal
    is read-model only and can never authorise an order."""
    if workspace is None or not hosted_mt5_remoteapp_enabled():
        return DELIVERY_NOT_AVAILABLE
    ds = str(getattr(workspace, "delivery_state", "") or "")
    if ds == "CONNECTED":
        return DELIVERY_READY               # RemoteApp actually connected to the persistent session
    if ds in ("AUTHORIZED", "DISCONNECTED"):
        return DELIVERY_PREPARING           # descriptor minted / reconnectable — delivery in progress
    # NONE / FAILED / unknown: not delivered. The real RemoteApp needs the host (RDS) — a Sponsor/host gate —
    # so a flagged-but-undelivered workspace is EXTERNAL_GATE (host-pending), never READY.
    return DELIVERY_EXTERNAL_GATE


def _phase_and_next(workspace, account):
    if workspace is None:
        return PHASE_NO_WORKSPACE, NEXT_REQUEST_WORKSPACE
    state = str(getattr(workspace, "canonical_state", "") or "")
    reason = str(getattr(workspace, "canonical_reason", "") or "")
    confirmed = getattr(account, "workspace_confirmed_at", None) is not None
    matched = getattr(workspace, "proj_account_match", None) is True
    bound = getattr(workspace, "execution_node_id", None) is not None

    # An active-account MISMATCH is the certified writer's SUSPENDED/ACCOUNT_MISMATCH (the manager never
    # yields CONNECTED-but-unmatched). It is a recoverable customer action — the wrong broker account is
    # logged in — so guide them to switch it, NOT to contact support (which the generic degraded bucket
    # would wrongly say). Handled BEFORE the degraded check because SUSPENDED is in _DEGRADED_STATES.
    if state == S.SUSPENDED and reason == WorkspaceReason.ACCOUNT_MISMATCH:
        return PHASE_BROKER_CONNECTED, NEXT_OPEN_MT5_AND_LOGIN
    if state in _DEGRADED_STATES:
        return PHASE_WORKSPACE_UNAVAILABLE, NEXT_CONTACT_SUPPORT
    if not bound:
        # workspace exists but no node yet (request accepted, allocation pending)
        return (PHASE_WORKSPACE_PREPARING if state == S.PROVISIONING else PHASE_WORKSPACE_REQUESTED), NEXT_WAIT
    if state in (S.PROVISIONING, S.WAITING_FOR_LOGIN):
        return PHASE_AWAITING_BROKER_LOGIN, NEXT_OPEN_MT5_AND_LOGIN
    # connected family
    if not matched:
        return PHASE_BROKER_CONNECTED, NEXT_OPEN_MT5_AND_LOGIN     # connected but active account not matched
    if not confirmed:
        return PHASE_ACCOUNT_CONFIRMATION_REQUIRED, NEXT_CONFIRM_ACCOUNT
    if state in (S.EXECUTION_READY, S.EXECUTING):
        return PHASE_WORKSPACE_READY, NEXT_ASSIGN_STRATEGY
    return PHASE_ACCOUNT_BOUND, NEXT_WAIT


def onboarding_journey_projection(workspace, account, *, staff: bool = False) -> dict:
    """Customer-safe onboarding-journey projection. ``strategy_eligible`` here is the journey signal
    (confirmed ∧ canonical EXECUTION_READY); the authoritative assignment-eligibility contract lives in the
    strategy layer and is strictly below arming. Staff receive extra, still-secret-free operator context."""
    phase, next_action = _phase_and_next(workspace, account)
    confirmed = getattr(account, "workspace_confirmed_at", None) is not None
    state = str(getattr(workspace, "canonical_state", "") or "") if workspace is not None else ""
    out = {
        "phase": phase,
        "next_action": next_action,
        "confirmed": confirmed,
        "strategy_eligible": bool(confirmed and state in (S.EXECUTION_READY, S.EXECUTING)),
        "delivery": delivery_readiness(workspace),
        "active_login_masked": _mask(getattr(workspace, "currently_attached_login", "")) if workspace else "",
    }
    if staff and workspace is not None:
        out["_staff"] = {
            "account_id": getattr(account, "id", None),
            "owner_id": getattr(account, "user_id", None),   # ownership = trading_account.user (single source)
            "canonical_state": state,
            "canonical_reason": str(getattr(workspace, "canonical_reason", "") or ""),
            "execution_node_id": getattr(workspace, "execution_node_id", None),
            "proj_account_match": getattr(workspace, "proj_account_match", None),
            "proj_connected": getattr(workspace, "proj_connected", None),
            "workspace_uuid": str(getattr(workspace, "workspace_uuid", "") or ""),
        }
    return out
