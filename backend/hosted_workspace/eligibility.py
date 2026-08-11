"""hosted_workspace.eligibility — ADR-0034 Onboarding strategy-assignment eligibility (DARK, read-only).

The Provider-B answer to "may a strategy be ASSIGNED to this hosted workspace yet?" expressed as an explicit,
secret-free {state, checklist, next_action} projection with a HARD three-tier separation that this module
exists to make un-blurrable:

    ASSIGNMENT-ELIGIBLE   <   ARMED   <   ORDER-AUTHORISED
    (a strategy may be         (execution_enabled +      (decided ONLY by the live bridge
     bound to the workspace)    canonical EXECUTION_READY) gate immediately before order_send)

Assignment eligibility is the LOWEST tier: it requires the customer to have a confirmed, connected, matched
workspace, but NOT that execution is armed. Arming (``execution_enabled``) is a strictly higher, separate
gate owned by the Execution Engine; order authorisation is higher still and is NEVER asserted here — this
projection reports it as an external live gate. Pure/read-only: it changes nothing and authorises nothing.
"""
from __future__ import annotations

from hosted_workspace.entitlement import has_hosted_workspace_capability
from hosted_workspace.flags import hosted_persistent_mt5_enabled
from hosted_workspace.onboarding_read_model import (
    NEXT_ASSIGN_STRATEGY,
    NEXT_CONFIRM_ACCOUNT,
    NEXT_OPEN_MT5_AND_LOGIN,
    NEXT_REQUEST_WORKSPACE,
    NEXT_WAIT,
)
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

# Stable checklist keys (order = evaluation order; the first unmet one drives next_action).
CHECK_SUBSYSTEM_ENABLED = "SUBSYSTEM_ENABLED"
CHECK_ENTITLED = "ENTITLED"
CHECK_WORKSPACE_PRESENT = "WORKSPACE_PRESENT"
CHECK_NODE_ALLOCATED = "NODE_ALLOCATED"
CHECK_BROKER_CONNECTED = "BROKER_CONNECTED"
CHECK_ACCOUNT_MATCHED = "ACCOUNT_MATCHED"
CHECK_ACCOUNT_CONFIRMED = "ACCOUNT_CONFIRMED"

# Summary states.
STATE_NOT_ELIGIBLE = "NOT_ELIGIBLE"
STATE_ASSIGNMENT_ELIGIBLE = "ASSIGNMENT_ELIGIBLE"
STATE_ARMED = "ARMED"

# The order-authorisation tier is NEVER decided here — it is the live bridge gate's authority.
ORDER_AUTHORISATION_EXTERNAL = "external_live_gate"

# checklist-key → the next action a customer/operator takes to satisfy it.
_NEXT_FOR = {
    CHECK_WORKSPACE_PRESENT: NEXT_REQUEST_WORKSPACE,
    CHECK_NODE_ALLOCATED: NEXT_WAIT,
    CHECK_BROKER_CONNECTED: NEXT_OPEN_MT5_AND_LOGIN,
    CHECK_ACCOUNT_MATCHED: NEXT_OPEN_MT5_AND_LOGIN,
    CHECK_ACCOUNT_CONFIRMED: NEXT_CONFIRM_ACCOUNT,
}


def strategy_assignment_eligibility(account, *, user=None) -> dict:
    """Return the Provider-B assignment-eligibility projection for ``account`` (its hosted workspace). PURE and
    read-only. ``assignment_eligible`` is TRUE only when every checklist item holds; ``armed`` is a strictly
    higher, separately-owned fact reported for transparency (never a substitute for the live gate); the
    order-authorisation tier is reported as an external live gate and never asserted True here. ``user`` may be
    supplied explicitly for the no-account (no-workspace-yet) case so the ENTITLED check is still meaningful."""
    ws = getattr(account, "hosted_workspace", None)
    if user is None:
        user = getattr(account, "user", None)
    confirmed = getattr(account, "workspace_confirmed_at", None) is not None
    subsystem_on = hosted_persistent_mt5_enabled()
    # ADR-0034 amendment: capability is commercial entitlement OR Hosted Beta programme membership (independent
    # of the commercial plan). Same predicate as hosted_workspace_admission, so eligibility stays consistent.
    entitled = has_hosted_workspace_capability(user)

    checks = [
        (CHECK_SUBSYSTEM_ENABLED, subsystem_on),
        (CHECK_ENTITLED, entitled),
        (CHECK_WORKSPACE_PRESENT, ws is not None),
        (CHECK_NODE_ALLOCATED, ws is not None and getattr(ws, "execution_node_id", None) is not None),
        (CHECK_BROKER_CONNECTED, ws is not None and getattr(ws, "proj_connected", None) is True),
        (CHECK_ACCOUNT_MATCHED, ws is not None and getattr(ws, "proj_account_match", None) is True),
        (CHECK_ACCOUNT_CONFIRMED, confirmed),
    ]
    checklist = [{"key": k, "ok": bool(ok)} for k, ok in checks]
    assignment_eligible = all(ok for _, ok in checks)

    # ARMED is strictly ABOVE assignment eligibility — the per-workspace explicit arm AND canonical readiness.
    # Reported read-only; this projection never arms and never authorises an order.
    armed = bool(
        assignment_eligible and ws is not None
        and getattr(ws, "execution_enabled", False) is True
        and str(getattr(ws, "canonical_state", "")) == S.EXECUTION_READY)

    if armed:
        state, next_action = STATE_ARMED, "none"
    elif assignment_eligible:
        state, next_action = STATE_ASSIGNMENT_ELIGIBLE, NEXT_ASSIGN_STRATEGY
    else:
        state = STATE_NOT_ELIGIBLE
        first_unmet = next((k for k, ok in checks if not ok), None)
        next_action = _NEXT_FOR.get(first_unmet, NEXT_WAIT)

    return {
        "state": state,
        "assignment_eligible": assignment_eligible,
        "armed": armed,                                  # tier 2 — read-only transparency, not authority
        "order_authorisation": ORDER_AUTHORISATION_EXTERNAL,  # tier 3 — always the live bridge gate
        "checklist": checklist,
        "next_action": next_action,
    }
