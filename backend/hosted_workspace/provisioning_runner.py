"""hosted_workspace.provisioning_runner — Beta Readiness Stream 2 (G2): the autonomous node-allocation driver.

A THIN, idempotent, retry-safe driver that finds every Hosted Workspace still at canonical ``PROVISIONING``
and drives it through the certified ``provisioning.allocate_workspace_node`` (which atomically reserves node
capacity, binds ``execution_node`` + ``workspace_node``, and advances ``PROVISIONING → WAITING_FOR_LOGIN``).
It closes the G2 gap: ``allocate_workspace_node`` existed but had no caller after ``request_hosted_workspace``,
so a self-requested workspace never progressed without an operator.

Properties (Workstream A): idempotent (allocate returns ``already_bound`` unchanged), retry-safe (a workspace
left stuck at PROVISIONING — bound but advance-failed, or waiting on capacity/rdp_host — is re-driven every
cycle), no duplicate allocation (capacity reserved under ``select_for_update``), deterministic (ordered scan),
fail-closed (``no_node_capacity`` / ``node_not_deliverable`` leave the workspace untouched). DARK: a no-op
returning an empty summary while ``hosted_persistent_mt5_enabled()`` is OFF. It performs NO host or broker
action and NEVER arms execution — it only records durable bindings and drives canonical state.
"""
from __future__ import annotations

import logging

from hosted_workspace.flags import hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.provisioning_runner"


def run_workspace_provisioning(*, actor: str = SOURCE) -> dict:
    """Allocate a node for every workspace still at PROVISIONING (idempotent). Returns a secret-free summary.
    Never raises into the caller (fail-open per workspace: one workspace's failure does not stop the cycle)."""
    if not hosted_persistent_mt5_enabled():
        return {"enabled": False, "candidates": 0, "allocated": 0, "already": 0,
                "no_capacity": 0, "not_deliverable": 0, "errors": 0}

    from hosted_workspace.provisioning import (
        allocate_workspace_node, ALLOC_OK, ALLOC_ALREADY, ALLOC_NO_CAPACITY, ALLOC_NODE_NOT_DELIVERABLE,
    )

    candidates = allocated = already = no_capacity = not_deliverable = errors = 0
    # Candidates = workspaces still at PROVISIONING (allocate is idempotent + advances a bound-but-stuck one,
    # so we do NOT pre-filter on execution_node — a bind that succeeded but whose advance failed is re-driven).
    qs = HostedMt5Workspace.objects.filter(canonical_state=str(S.PROVISIONING)).iterator()
    for ws in qs:
        candidates += 1
        try:
            res = allocate_workspace_node(ws, actor=actor)
        except Exception:  # noqa: BLE001 — one workspace's failure must not stop the cycle
            errors += 1
            logger.exception("workspace provisioning failed for workspace=%s", getattr(ws, "pk", None))
            continue
        if not res.ok:
            if res.reason == ALLOC_NO_CAPACITY:
                no_capacity += 1
            elif res.reason == ALLOC_NODE_NOT_DELIVERABLE:
                not_deliverable += 1
            else:
                errors += 1
        elif res.reason == ALLOC_ALREADY:
            already += 1
        elif res.reason == ALLOC_OK:
            allocated += 1
    return {"enabled": True, "candidates": candidates, "allocated": allocated, "already": already,
            "no_capacity": no_capacity, "not_deliverable": not_deliverable, "errors": errors}
