"""ADR-0034 / M3c — the Workspace read projection (derived, non-secret, DARK).

A query-shaped, SECRET-FREE view of a workspace's canonical M3c state, for the read-only observability API
and any future operator surface. Like the ADR-0032 operational read model, this is a DERIVED PROJECTION
(a cache), never authority: the order-time gate stays ``evaluate_binding`` in the bridge.

Structural safety: the projection is built from an explicit ALLOW-LIST of fields. It NEVER emits a
credential, a full broker login, an attach path, or any host/operator diagnostic. The active login, if
surfaced at all (staff view), is MASKED (parity with ``HostedMt5Workspace.contract``). ``execution_ready``
here is the canonical display flag — it is NOT the authority to place an order.
"""
from __future__ import annotations

from hosted_workspace.models import HostedMt5Workspace

# Ceiling on the staff provenance list so the projection stays O(1)-ish regardless of transition volume.
_MAX_TRANSITIONS = 10


def _iso(dt):
    return dt.isoformat() if dt else None


def workspace_state_projection(workspace: HostedMt5Workspace, *, staff: bool = False) -> dict:
    """Return the safe, allow-listed projection of ``workspace``'s canonical state. Customer-safe by
    default; ``staff=True`` adds operator-only, still-secret-free provenance (versions, correlation id,
    supervision, masked login, recent transitions)."""
    projection = {
        "account_id": workspace.trading_account_id,
        "workspace_uuid": str(workspace.workspace_uuid),
        "canonical_state": str(workspace.canonical_state),
        "canonical_reason": str(workspace.canonical_reason),
        # Display-only readiness derived from canonical state — NOT the order-time authority.
        "execution_ready": workspace.canonical_execution_ready,
        "health": {
            "process_running": workspace.proj_process_running,
            "ipc_available": workspace.proj_ipc_available,
            "connected": workspace.proj_connected,
            "account_match": workspace.proj_account_match,
            "trade_allowed": workspace.proj_trade_allowed,
            "execution_ready": workspace.proj_execution_ready,
        },
        "last_observed_at": _iso(workspace.last_observed_at),
        "last_decision_at": _iso(workspace.last_decision_at),
        "last_transition_at": _iso(workspace.last_transition_at),
        "updated_at": _iso(workspace.updated_at),
    }
    if staff:
        login = workspace.currently_attached_login or ""
        projection["operator"] = {
            "observation_version": int(workspace.observation_version or 0),
            "decision_version": int(workspace.decision_version or 0),
            "correlation_id": workspace.last_correlation_id or "",
            "supervision_state": workspace.supervision_state,
            "remoteapp_ready": workspace.remoteapp_ready,
            # MASKED login only (never the full broker identifier); no server/path/credential.
            "active_login_masked": ("***" + login[-3:]) if login else "",
            "recent_transitions": _recent_transitions(workspace),
        }
    return projection


def _recent_transitions(workspace: HostedMt5Workspace) -> list:
    """Bounded, secret-free provenance tail for the staff view (canonical enum values + identifiers only)."""
    rows = workspace.transitions.all()[:_MAX_TRANSITIONS]
    return [
        {
            "from_state": t.from_state,
            "to_state": t.to_state,
            "reason": t.reason,
            "observation_version": int(t.observation_version),
            "decision_version": int(t.decision_version),
            "state_changed": t.state_changed,
            "execution_ready_changed": t.execution_ready_changed,
            "telemetry_event": t.telemetry_event,
            "correlation_id": t.correlation_id,
            "created_at": _iso(t.created_at),
        }
        for t in rows
    ]
