"""ADR-0034 Execution Engine — Hosted Workspace (Provider B) routing + arming (DARK, demo-only).

Decision C (isolation topology, Phase 1): every execution operation for a Provider-B account must resolve to
EXACTLY ONE authorised, owner-bound Hosted Workspace → one MT5 process → one active broker account → one
route. Never a shared/NULL target, never another user's workspace, never "whichever MT5 is attached".

Decision D (layered arming): execution is permitted only when the full AND of the backend conditions holds
(global flag ∧ execution feature flag ∧ provider == persistent_workspace ∧ workspace.execution_enabled ∧
canonical state permits ∧ demo-only) AND the live order-time gates (guarded attach ∧ live identity ∧
connected ∧ trade_allowed ∧ health/pause) — the latter enforced by the certified bridge, NOT here.

This module is pure routing/authorisation resolution:
- It reads durable bindings server-side; it NEVER trusts client-supplied identity as authority.
- It performs NO order, attach, login, or launch.
- Persisted state is routing/readiness CONTEXT only — never order authority (the live bridge gate is).
- Fail-closed on every ambiguity; DARK (no-op-shaped rejection while the subsystem/execution flags are off,
  via the readiness gate it delegates to).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ── execution result / routing reason codes (ADR-0034 Execution Engine PART 14) — stable, secret-free ──
ER_ROUTE_OK = "execution_route_ok"
ER_ACCOUNT_MISSING = "workspace_account_missing"
ER_WORKSPACE_NOT_FOUND = "workspace_not_found"
ER_WORKSPACE_OWNER_MISMATCH = "workspace_owner_mismatch"
ER_WORKSPACE_ROUTE_AMBIGUOUS = "workspace_route_ambiguous"
ER_BINDING_MISMATCH = "binding_mismatch"
ER_NOT_ARMED = "workspace_execution_not_armed"
ER_ROUTE_MISSING = "workspace_route_missing"        # a hosted job with a NULL/shared node — Decision C
ER_WORKER_NOT_ENTITLED = "worker_not_entitled"      # a legacy/shared worker tried to claim a hosted job


@dataclass(frozen=True)
class RouteDecision:
    """The resolved, owner-bound route for one Provider-B execution operation. ``ok`` gates the operation;
    ``expected_login``/``expected_server`` are the SERVER-DERIVED identity the bridge re-verifies live before
    the mutation. Secret-free (identifiers only)."""
    ok: bool
    reason_code: str
    workspace_uuid: str = ""
    expected_login: str = ""
    expected_server: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason_code": self.reason_code, "workspace_uuid": self.workspace_uuid,
                "expected_login": self.expected_login, "expected_server": self.expected_server}


def hosted_execution_armed(account) -> bool:
    """The layered backend arming decision (Decision D conditions 1-5 + 11) for a Provider-B account.
    Delegates to the single readiness authority (which already ANDs the global + execution flags, provider
    selection, demo-only, per-workspace ``execution_enabled``, and the canonical projection). Returns False
    fail-closed for any non-Provider-B account or any failing condition. NEVER the order authority — the live
    bridge gate re-verifies conditions 6-10 immediately before every mutation."""
    if account is None or getattr(account, "pk", None) is None:
        return False
    from execution.hosted_pin import is_hosted_workspace_account
    if not is_hosted_workspace_account(account):
        return False
    from execution.readiness import PersistentWorkspaceProvider
    return PersistentWorkspaceProvider().evaluate(account).eligible


def resolve_hosted_route(account) -> RouteDecision:
    """Resolve ``account`` to its ONE authorised, owner-bound Hosted Workspace route (Decision C), fail-closed.

    Enforces: the account is persisted → it owns exactly one ``HostedMt5Workspace`` (OneToOne) → that
    workspace's owner is the account's owner (defence-in-depth against a mis-set binding) → execution is
    armed → derives the expected login/server server-side. On any failure returns a specific, secret-free
    reason and NO identity. This is the server-side binding authority: a wrong/ambiguous/cross-user route can
    never reach an MT5 mutation.
    """
    if account is None or getattr(account, "pk", None) is None:
        return RouteDecision(False, ER_ACCOUNT_MISSING)
    ws = getattr(account, "hosted_workspace", None)
    if ws is None:
        return RouteDecision(False, ER_WORKSPACE_NOT_FOUND)
    # Owner binding — the OneToOne makes ws.trading_account == account by construction; assert it anyway so
    # a mis-set FK (or a future model change) can never let account A route to account B's workspace.
    if getattr(ws, "trading_account_id", None) != account.pk:
        return RouteDecision(False, ER_WORKSPACE_OWNER_MISMATCH)
    account_owner = getattr(account, "user_id", None)
    ws_owner = _workspace_owner_id(ws, account)
    if account_owner is None or ws_owner is None or account_owner != ws_owner:
        return RouteDecision(False, ER_WORKSPACE_OWNER_MISMATCH)
    # Layered arm (Decision D) — delegated to the single readiness authority.
    if not hosted_execution_armed(account):
        return RouteDecision(False, ER_NOT_ARMED)
    # Server-derived identity pin (never client-supplied). Reuses the certified G3 derivation.
    from execution.hosted_pin import identity_pin_for
    pin = identity_pin_for(account)
    login = str(pin.get("expected_login") or "")
    server = str(pin.get("expected_server") or "")
    if not login:  # a route with no bound login can never be safely pinned → fail closed
        return RouteDecision(False, ER_BINDING_MISMATCH)
    return RouteDecision(True, ER_ROUTE_OK, workspace_uuid=str(getattr(ws, "workspace_uuid", "") or ""),
                         expected_login=login, expected_server=server)


def authorize_hosted_claim(job, *, worker_is_node_aware: bool) -> RouteDecision:
    """G4 claim-seam entitlement (Decision C) — the extra gate at the authoritative claim boundary.

    A NON-hosted job passes through unchanged (``ER_ROUTE_OK``) so legacy dispatch is untouched. For a Hosted
    Workspace (Provider B) job it proves, at claim time and fail-closed, that the job resolves to ONE
    owner-bound, armed workspace (``resolve_hosted_route``) AND has a durable NON-NULL node route (no shared /
    NULL route) AND is being claimed by a node-aware, non-legacy/non-shared worker (no shared-worker
    entitlement). One workspace → one process → one route → one authorised worker.

    DARK/zero-overhead: while the subsystem is dark ``is_hosted_workspace_account`` short-circuits on the flag
    before touching the account, so legacy claims are byte-for-byte unchanged.
    """
    account = getattr(job, "account", None)
    from execution.hosted_pin import is_hosted_workspace_account
    if not is_hosted_workspace_account(account):
        return RouteDecision(True, ER_ROUTE_OK)  # not a hosted job — existing behaviour, untouched
    route = resolve_hosted_route(account)         # owner-bound + armed + server-derived identity
    if not route.ok:
        return route
    if getattr(job, "terminal_node_id", None) is None:   # Decision C — no shared/NULL route for a hosted job
        return RouteDecision(False, ER_ROUTE_MISSING)
    if not worker_is_node_aware:                          # Decision C — no legacy/shared-worker entitlement
        return RouteDecision(False, ER_WORKER_NOT_ENTITLED)
    return route


def _workspace_owner_id(ws, account) -> Optional[int]:
    """The owning user id of the workspace, resolved without an extra query when possible (the OneToOne
    back-reference means ws.trading_account is `account` itself once bound)."""
    if getattr(ws, "trading_account_id", None) == getattr(account, "pk", None):
        return getattr(account, "user_id", None)
    ta = getattr(ws, "trading_account", None)
    return getattr(ta, "user_id", None) if ta is not None else None
