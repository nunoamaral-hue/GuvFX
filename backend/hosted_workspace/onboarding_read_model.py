"""hosted_workspace.onboarding_read_model — ADR-0034 Onboarding customer-journey projection (DARK).

A PURE, secret-free projection that turns the durable workspace/account truth into the customer-facing
onboarding phase + next action + a degrade-closed delivery-readiness signal. It reads authoritative state
only (canonical_state, proj_account_match, execution_node, workspace_confirmed_at) — it NEVER derives
readiness independently and NEVER exposes a credential, a full login (masked), an attach path, a node id
(staff only), or a stack trace. Phase names are stable customer-facing identifiers.
"""
from __future__ import annotations

from hosted_workspace.flags import hosted_delivery_lifecycle_enabled, hosted_mt5_remoteapp_enabled
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
DELIVERY_DELIVERABLE = "DELIVERY_DELIVERABLE"   # BB#1: authority proves it openable (availability, not CONNECTED)
DELIVERY_READY = "DELIVERY_READY"
DELIVERY_EXTERNAL_GATE = "DELIVERY_EXTERNAL_GATE"

_CONNECTED_STATES = (S.CONNECTED, S.EXECUTION_READY, S.EXECUTING)
_DEGRADED_STATES = (S.DISCONNECTED, S.RECOVERING, S.SUSPENDED, S.RETIRED)

# BB#1 HIGH fix (Sponsor 2026-08-16, adversarial review): DELIVERABLE means "the slot is actually openable now",
# so it is projected ONLY once slot preparation has FINISHED — i.e. the certified writer advanced the workspace
# PAST PROVISIONING (to WAITING_FOR_LOGIN or a later connected/degraded state). That advance happens ONLY on a
# ``prepare_hosted_slot`` ``prepared=True``, which itself requires Stage 8 RemoteApp verify AND (flag on) the
# Stage 10 observer. While still at PROVISIONING the RemoteApp may be unpublished and no observer exists, so a
# live "Open MetaTrader" would open a broken/unobservable session — those states must show the "preparing"
# placeholder, never a launch. RETIRED (a teardown reachable straight from PROVISIONING) is never openable. A
# POSITIVE allow-list keeps this fail-closed: an unknown/garbled canonical_state is not deliverable.
_DELIVERABLE_ELIGIBLE_STATES = frozenset({
    S.WAITING_FOR_LOGIN, S.CONNECTED, S.EXECUTION_READY, S.EXECUTING,
    S.DISCONNECTED, S.RECOVERING, S.SUSPENDED,
})


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
    # BB#1 (Sponsor 2026-08-16): break the button⇄CONNECTED circular dependency. Once the delivery AUTHORITY
    # proves the workspace deliverable (RemoteApp published + node transport + PROVISIONED identity + GUAC), it
    # is DELIVERABLE — the frontend surfaces "Open MetaTrader" so the customer can make the very click that
    # CREATES the session the trusted observer later turns into CONNECTED. This is AVAILABILITY, kept DISTINCT
    # from READY(CONNECTED): it never asserts a session is up. Gated → OFF ⇒ byte-identical to before.
    if hosted_delivery_lifecycle_enabled():
        # HIGH-fix gate: only surface DELIVERABLE once prep has FINISHED (canonical past PROVISIONING — see
        # _DELIVERABLE_ELIGIBLE_STATES) so "Open MetaTrader" never points at an unpublished/unobservable slot.
        # CZ-refused (defence in depth): Customer Zero uses the legacy Terminal Access path, never this new
        # DELIVERABLE surface, and every mutation-bearing delivery edge already excludes CZ.
        from hosted_workspace.delivery import workspace_delivery_ready
        from hosted_workspace.tenant_isolation import is_customer_zero_account
        state = str(getattr(workspace, "canonical_state", "") or "")
        if (state in _DELIVERABLE_ELIGIBLE_STATES
                and not is_customer_zero_account(getattr(workspace, "trading_account_id", None))
                and workspace_delivery_ready(workspace)):
            return DELIVERY_DELIVERABLE
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
    # AJ#3 product correction: ONBOARDING is COMPLETE once the workspace is OPERATIONAL — CONNECTED + account
    # matched + confirmed. Execution readiness (AutoTrading → canonical EXECUTION_READY) and arming are STRICTLY
    # HIGHER, separately-owned tiers (eligibility.py: ASSIGNMENT-ELIGIBLE < ARMED < ORDER-AUTHORISED) and must
    # NEVER be an onboarding gate. The retired PHASE_ACCOUNT_BOUND used to wait here for EXECUTION_READY — which
    # depends on obs.trade_allowed (a host-observed MT5 fact the backend can't write), so a confirmed customer
    # could sit on an INDEFINITE "Finishing up" spinner. Assignment eligibility, arming and the order gate all
    # read canonical EXECUTION_READY DIRECTLY and are unaffected by this customer-facing phase.
    return PHASE_WORKSPACE_READY, NEXT_ASSIGN_STRATEGY


def onboarding_journey_projection(workspace, account, *, staff: bool = False) -> dict:
    """Customer-safe onboarding-journey projection. ``strategy_eligible`` is the journey signal that onboarding
    is COMPLETE (phase WORKSPACE_READY = operational workspace: CONNECTED + matched + confirmed) so the customer
    may proceed to choose a strategy — it does NOT require EXECUTION_READY/arming. The authoritative
    assignment-eligibility contract lives in the strategy layer (eligibility.py: ASSIGNMENT-ELIGIBLE < ARMED <
    ORDER-AUTHORISED) and is strictly below arming. Staff receive extra, still-secret-free operator context."""
    phase, next_action = _phase_and_next(workspace, account)
    confirmed = getattr(account, "workspace_confirmed_at", None) is not None
    state = str(getattr(workspace, "canonical_state", "") or "") if workspace is not None else ""
    out = {
        "phase": phase,
        "next_action": next_action,
        "confirmed": confirmed,
        "strategy_eligible": bool(phase == PHASE_WORKSPACE_READY),
        "delivery": delivery_readiness(workspace),
        "active_login_masked": _mask(getattr(workspace, "currently_attached_login", "")) if workspace else "",
        # Additive, server-derived source of truth (Sponsor 2026-08-16): has the customer's EXPECTED broker
        # identity already been recorded via the write-once deferred bind? The bind writes the login to
        # ``trading_account.account_number`` (provisioning.bind_broker_identity), so a non-empty account number
        # IS "identity declared". Read-only projection of EXISTING state — never a secret (the value is a
        # boolean, never the login), never a lifecycle/provisioning change. The onboarding UI uses it as the
        # single source of truth for whether to still show the broker-declaration form vs. the waiting experience,
        # so a page reload is deterministic and never re-shows a form the customer already completed.
        "identity_declared": bool(str(getattr(account, "account_number", "") or "").strip()),
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
