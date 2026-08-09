"""ADR-0034 Workspace Delivery — the delivery read projection (derived, non-secret, DARK).

A query-shaped, SECRET-FREE view of a workspace's RemoteApp delivery state, for the read-only delivery API
and any future operator surface. Like the M3c read model this is a DERIVED PROJECTION (a cache), never
authority: it never gates order execution, and ``remoteapp_ready`` here means only "the window is up",
never "an order may be sent" (that remains ``evaluate_binding`` in the bridge).

Structural safety: built from an explicit ALLOW-LIST. It NEVER emits a credential, the Windows username,
the runtime path, or the signed ``embed_url``. The delivery HOST (node hostname) is operator-only — it
appears only in the ``staff=True`` block, never in the customer-facing projection.
"""
from __future__ import annotations

from hosted_workspace.models import HostedMt5Workspace


def _iso(dt):
    return dt.isoformat() if dt else None


def delivery_state_projection(workspace: HostedMt5Workspace, *, staff: bool = False) -> dict:
    """Return the safe, allow-listed projection of ``workspace``'s delivery state. Customer-safe by default;
    ``staff=True`` adds operator-only (still secret-free) fields — the delivery host and correlation id."""
    projection = {
        "account_id": workspace.trading_account_id,
        "workspace_uuid": str(workspace.workspace_uuid),
        "delivery_state": str(workspace.delivery_state),
        "delivery_reason": workspace.delivery_reason or "",
        # "The RemoteApp window is up" — display only, NOT the order-time authority.
        "remoteapp_ready": bool(workspace.remoteapp_ready),
        # Whether a delivery host is assigned at all (bool only — the hostname itself is operator-only).
        "node_assigned": workspace.workspace_node_id is not None,
        "last_delivery_attempt": _iso(workspace.last_delivery_attempt),
        "last_delivery_success": _iso(workspace.last_delivery_success),
        "updated_at": _iso(workspace.updated_at),
    }
    if staff:
        node = workspace.workspace_node
        projection["operator"] = {
            # Operator-only, never customer-facing, never a credential. ``delivery_host`` is the RDP
            # TRANSPORT endpoint (``node.rdp_host``) guacd dials; ``node_identity`` is the separate logical
            # execution-node name (``node.hostname``). The two are deliberately distinct.
            "delivery_host": (node.rdp_host if node else ""),
            "node_identity": (node.hostname if node else ""),
            "supervision_state": workspace.supervision_state,
            "correlation_id": workspace.last_delivery_correlation_id or "",
        }
    return projection
