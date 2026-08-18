"""ADR-0048 — NODE EXECUTION COMMISSIONING (server-derived, deterministic, idempotent).

Execution infrastructure belongs to the ``TerminalNode``, NOT to each customer. This module makes a
hosted automated-execution node fit for service by ensuring its execution PATH exists and is proven,
independent of any customer:

    node exists + configured  →  order bridge configured  →  a DEDICATED node-aware order worker
    registered + authorized for THIS EXACT node  →  worker liveness (optional at commission)  →
    bridge health  →  read-only claimability  →  ``node_execution_operational == True``

A COMMISSIONED node authorises NO customer and places NO order. It is the counterpart to — and must
run before — customer execution authorization (ADR-0047) and the live order-time gate (unchanged).

Contract:
  * DRY-RUN by default; ``apply`` performs the (idempotent) worker registration / node grant only.
  * Creates NO ExecutionJob, sends NO order, arms NO customer strategy, contacts NO host.
  * Refuses to touch a Customer Zero node (``forbidden_execution_node_ids`` — live-derived).
  * A dedicated worker serves exactly ONE node: refuses the shared legacy identity and refuses to
    reuse an identity already authorized for a DIFFERENT node (no cross-node identity reuse).
  * HARD ORDERING (fail-closed): refuses to commission a node that still has un-reconciled stale
    PENDING PLACE_ORDER jobs — those must be reconciled FIRST, so a newly-claimable node can never
    fire historical orders (enforced in code, not only the runbook).
  * The worker secret is taken from the environment, never a CLI argument, never logged.
"""
from __future__ import annotations

import os

from django.db import transaction
from django.utils import timezone

# Env var carrying the dedicated node worker's secret (never a CLI arg; never logged).
NODE_WORKER_SECRET_ENV = "GUVFX_NODE_WORKER_SECRET"
DEFAULT_STALE_OLDER_THAN_SECONDS = 1800


def commission_execution_node(
    *, node_hostname: str, worker_id: str, apply: bool = False,
    require_liveness: bool = False, bridge_url: str = "",
    stale_older_than_seconds: int = DEFAULT_STALE_OLDER_THAN_SECONDS,
) -> dict:
    """Commission ``node_hostname``'s execution path via a dedicated ``worker_id``. Read-only unless
    ``apply``. Deterministic + idempotent + fail-closed; identical for Node 2, 3, 4, … (no
    account-specific code)."""
    from execution.auth import LEGACY_WORKER_ID
    from execution.models import ExecutionJob, TerminalNode, WorkerIdentity
    from execution.node_execution import node_execution_operational
    from hosted_workspace.tenant_isolation import forbidden_execution_node_ids

    report: dict = {
        "node_hostname": node_hostname, "worker_id": worker_id, "apply": apply,
        "applied": False, "operational": False, "reason": "", "checks": {},
    }

    node = TerminalNode.objects.filter(hostname=node_hostname).first()
    if node is None:
        report["reason"] = "NODE_NOT_FOUND"
        return report
    report["node_id"] = node.id

    # Never commission a Customer Zero node — CZ uses the legacy global path, not a per-node worker.
    if node.id in forbidden_execution_node_ids():
        report["reason"] = "NODE_IS_CUSTOMER_ZERO"
        return report

    if not worker_id or worker_id == LEGACY_WORKER_ID:
        report["reason"] = "WORKER_IS_LEGACY_OR_EMPTY"
        return report

    # A dedicated worker serves exactly ONE node — refuse an identity already authorized for another.
    existing = WorkerIdentity.objects.filter(worker_id=worker_id).first()
    if existing is not None:
        other_nodes = [n for n in ((existing.worker_permissions or {}).get("authorized_nodes") or [])
                       if n and n != node_hostname]
        if other_nodes:
            report["reason"] = "WORKER_AUTHORIZED_FOR_OTHER_NODE"
            report["checks"]["existing_authorized_nodes"] = other_nodes
            return report

    # HARD ORDERING: refuse to make the node claimable while un-reconciled stale PENDING orders exist.
    cutoff = timezone.now() - timezone.timedelta(seconds=stale_older_than_seconds)
    stale = ExecutionJob.objects.filter(
        terminal_node_id=node.id, status=ExecutionJob.Status.PENDING,
        job_type=ExecutionJob.JobType.PLACE_ORDER, created_at__lte=cutoff,
    ).count()
    report["checks"]["stale_pending_orders"] = stale
    if stale:
        report["reason"] = "STALE_ORDERS_PRESENT"   # reconcile first (reconcile_stale_preactivation_orders)
        return report

    # Optionally persist a node bridge URL (server-side config only; NO host contact). Never overwrite
    # a DIFFERENT existing endpoint (fail-closed, mirrors slot_preparation's guard).
    if bridge_url:
        current = (node.order_bridge_base_url or "").strip()
        if current and current != bridge_url.strip():
            report["reason"] = "BRIDGE_URL_CONFLICT"
            report["checks"]["current_bridge_url"] = current
            return report

    if not apply:
        # Dry-run: report the operational verdict the node WOULD have. If the worker is not yet
        # registered, reflect that (node not operational until commissioned).
        op = node_execution_operational(node, require_worker_liveness=require_liveness)
        report["operational"] = op.operational
        report["reason"] = op.reason_code
        report["checks"].update(op.checks)
        return report

    # ---- APPLY: register/reuse the dedicated worker + authorize THIS node (idempotent). ----
    # Validate every precondition that can REFUSE the apply BEFORE mutating anything, so a refused
    # apply (e.g. missing secret) leaves the node and its bridge untouched (no partial commissioning).
    secret = os.environ.get(NODE_WORKER_SECRET_ENV)
    if existing is None and not secret:
        report["reason"] = "SECRET_REQUIRED"   # a NEW identity needs its secret (env only)
        return report

    # Atomic: the bridge-URL write and the worker create/update commit together or not at all, so a
    # failure mid-way can never leave the node with a configured bridge but no worker (no partial
    # commissioning), and the create-branch's unique-worker_id constraint still fails a concurrent
    # double-run closed (loser raises IntegrityError, rolls back, no duplicate row / no over-grant).
    with transaction.atomic():
        if bridge_url and (node.order_bridge_base_url or "").strip() != bridge_url.strip():
            TerminalNode.objects.filter(pk=node.pk,
                                        order_bridge_base_url__in=("", bridge_url.strip())).update(
                order_bridge_base_url=bridge_url.strip())
            node.refresh_from_db()

        if existing is None:
            wi = WorkerIdentity.objects.create(
                worker_id=worker_id, worker_secret_hash=WorkerIdentity.hash_secret(secret),
                status=WorkerIdentity.Status.ACTIVE,
                worker_permissions={"authorized_nodes": [node_hostname]})
            _credential_event("CREATED", worker_id)
        else:
            wi = existing
            perms = dict(wi.worker_permissions or {})
            nodes = list(perms.get("authorized_nodes") or [])
            if node_hostname not in nodes:
                nodes.append(node_hostname)
            perms["authorized_nodes"] = nodes
            fields = ["worker_permissions", "status"]
            wi.worker_permissions = perms
            wi.status = WorkerIdentity.Status.ACTIVE   # re-activate a revoked identity on re-commission
            if secret:  # optional rotation
                new_hash = WorkerIdentity.hash_secret(secret)
                if new_hash != wi.worker_secret_hash:
                    wi.worker_secret_hash = new_hash
                    fields.append("worker_secret_hash")
                    _credential_event("ROTATED", worker_id)
            wi.save(update_fields=fields)

    report["applied"] = True
    op = node_execution_operational(node, require_worker_liveness=require_liveness)
    report["operational"] = op.operational
    report["reason"] = op.reason_code
    report["checks"].update(op.checks)
    return report


def _credential_event(event: str, worker_id: str) -> None:
    try:
        from core.audit import log_credential_event

        log_credential_event(event, entity_type="WorkerIdentity", entity_id=worker_id,
                             actor="commission_execution_node")
    except Exception:
        pass
