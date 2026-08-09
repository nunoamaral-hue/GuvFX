"""hosted_workspace.onboarding_ops — ADR-0034 Onboarding staff/ops visibility (DARK, read-only).

A secret-free operator projection over the onboarding fleet: one row per Hosted Workspace with its journey
phase, canonical state, node, confirmation, delivery-readiness and assignment-eligibility state. Built ENTIRELY
from the same customer-facing projections (with the staff lens), so operators never see more secrets than the
customer — only more context (owner id, node id, workspace uuid). No credential, no full login, no path.
The owner id is the account's user (``trading_account.user`` — the single ownership source; no separate FK).
"""
from __future__ import annotations

from hosted_workspace.eligibility import strategy_assignment_eligibility
from hosted_workspace.onboarding_read_model import onboarding_journey_projection


def onboarding_fleet_projection(workspaces) -> list[dict]:
    """Project an iterable of ``HostedMt5Workspace`` to secret-free operator rows. Pure/read-only."""
    rows = []
    for ws in workspaces:
        acct = getattr(ws, "trading_account", None)
        j = onboarding_journey_projection(ws, acct, staff=True)
        elig = strategy_assignment_eligibility(acct)
        staff = j.get("_staff", {})
        rows.append({
            "account_id": getattr(acct, "id", None),
            "owner_id": getattr(acct, "user_id", None),
            "phase": j["phase"],
            "next_action": j["next_action"],
            "confirmed": j["confirmed"],
            "delivery": j["delivery"],
            "assignment_state": elig["state"],
            "armed": elig["armed"],
            "canonical_state": staff.get("canonical_state"),
            "canonical_reason": staff.get("canonical_reason"),
            "execution_node_id": staff.get("execution_node_id"),
            "workspace_uuid": staff.get("workspace_uuid"),
        })
    return rows
