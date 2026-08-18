"""ADR-0048 — GuvFX EXECUTION-PATH readiness (concept C), node-operational gate, and the
shared node-awareness rule.

This module answers ONE question the rest of the system never asked before the AJ#7.2.1 /
Node-2 incident: *even if the customer's MT5 runtime is ready and the customer authorised the
strategy, can a PLACE_ORDER job for this account's node actually be CLAIMED and dispatched?*

It exists because four DISTINCT properties were being conflated (see the ADR):

    A. MT5 / runtime readiness      — the tenant's terminal is up, attached, connected, matched,
                                      AutoTrading allowed, observation fresh.  (canonical
                                      ``EXECUTION_READY`` / ``PersistentWorkspaceProvider``)
    B. customer strategy authorization — the customer explicitly enabled the strategy.
                                      (ADR-0047 ``execution_authorized_at`` + the arm bit)
    C. GuvFX execution-path availability — an authorized, non-revoked, order-capable worker
                                      exists for the account's node, the worker is alive, the
                                      node's order bridge is healthy, and the order route
                                      resolves to the correct node.  ← THIS MODULE
    D. order authorization           — the live, per-order, fail-closed bridge gate.
                                      (``evaluate_binding`` on the host — UNCHANGED)

The Node-2 defect was a pure concept-C hole: A, B and D were all fine, yet no worker was
authorized to claim ``guvfx-beta-node-1`` jobs, so real signals produced PLACE_ORDER jobs that
sat PENDING forever with zero fills.

CONTRACT (do not weaken):
  * READ-ONLY / non-authoritative. This module NEVER places, sizes, or authorises an order and
    is NEVER consulted at order time — the live bridge gate (D) remains the sole order-time
    authority. It is an eligibility / observability surface, ANDed *alongside* the existing
    gates, never merged into them.
  * FAIL-CLOSED. Unknown ⇒ not-ready. Any exception ⇒ not-ready with a reason code. A missing
    health row is treated as unobserved (never HEALTHY).
  * DARK-safe. While the hosted pin subsystem is off, hosted order transport resolves to the
    legacy global bridge for every job; this module reports ``EP_EXPECTED_DARK`` for hosted
    accounts rather than over-reporting readiness.
  * Composes, never re-derives: it calls ``resolve_order_transport`` (route), ``resolve_hosted_route``
    (owner-bound single armed route + node agreement) and reuses ``ComponentHealth`` for bridge /
    worker liveness, so it cannot drift from the real claim/dispatch behaviour.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone


# --------------------------------------------------------------------------------------------------
# Shared node-awareness rule — the SINGLE source of truth for "which nodes may this worker claim".
# The claim seam (execution.views.next_job) and this readiness module MUST use the same rule or a
# readiness check will silently diverge from real claim behaviour. Factored out of views.py.
# --------------------------------------------------------------------------------------------------
def worker_authorized_nodes(worker_identity) -> list[str]:
    """Return the node hostnames a worker identity may claim jobs for.

    Mirrors the claim-seam rule exactly: the shared ``legacy-worker`` identity is FORCE-EMPTIED
    (it must never be node-aware, or a shared/legacy bridge could claim a hosted node-bound job);
    a dedicated per-node worker uses its own ``authorized_nodes``. A revoked worker returns [] so
    it can claim nothing.
    """
    from execution.auth import LEGACY_WORKER_ID

    if worker_identity is None:
        return []
    if getattr(worker_identity, "status", None) != "ACTIVE":
        return []
    if getattr(worker_identity, "worker_id", None) == LEGACY_WORKER_ID:
        return []
    perms = getattr(worker_identity, "worker_permissions", None) or {}
    nodes = perms.get("authorized_nodes") or []
    return [str(n) for n in nodes] if isinstance(nodes, (list, tuple)) else []


def _worker_recently_seen(worker_identity, max_age_seconds: int) -> bool:
    """A worker is 'alive' if it claimed a job (stamped ``last_seen``) within ``max_age_seconds``.

    Fail-closed: a worker that has NEVER been seen (``last_seen`` NULL — e.g. registered but never
    started) is NOT considered alive. ``last_seen`` is an additive nullable column stamped in the
    claim seam.
    """
    last = getattr(worker_identity, "last_seen", None)
    if last is None:
        return False
    return (timezone.now() - last) <= timedelta(seconds=max_age_seconds)


def worker_liveness_max_age_seconds() -> int:
    # A node-aware order worker polls the claim endpoint on a short loop; a couple of minutes of
    # silence means it is down. Configurable; conservative default.
    try:
        return int(os.getenv("EXECUTION_WORKER_LIVENESS_MAX_AGE_SECONDS", "180"))
    except (TypeError, ValueError):
        return 180


def eligible_order_claimant(node) -> "ClaimantProbe":
    """READ-ONLY synthetic claimability check (no ExecutionJob is created).

    Answers requirement 3's "a hypothetical node-bound job WOULD have an eligible claimant":
    does at least one ACTIVE, non-legacy, node-aware WorkerIdentity exist whose authorized_nodes
    contains this node's hostname, and (when liveness is required) has it been seen recently?

    This is the proactive dual of the reactive per-caller filter in ``next_job``. It NEVER
    mutates and NEVER enqueues a probe job — the static "an eligible claimant exists" query is the
    synthetic check the packet asks for. (A future dynamic NODE_PROBE round-trip may be layered on
    top; it is intentionally out of scope here to keep the check order-free.)
    """
    from execution.auth import LEGACY_WORKER_ID
    from execution.models import WorkerIdentity

    hostname = getattr(node, "hostname", None)
    if not hostname:
        return ClaimantProbe(ok=False, reason="EP_NODE_NO_HOSTNAME", worker_id=None, alive=False)

    max_age = worker_liveness_max_age_seconds()
    candidate = None
    for wi in WorkerIdentity.objects.filter(status=WorkerIdentity.Status.ACTIVE).exclude(
        worker_id=LEGACY_WORKER_ID
    ):
        if hostname in worker_authorized_nodes(wi):
            candidate = wi
            if _worker_recently_seen(wi, max_age):
                return ClaimantProbe(ok=True, reason="EP_CLAIMANT_OK", worker_id=wi.worker_id, alive=True)
    if candidate is None:
        return ClaimantProbe(ok=False, reason="EP_NO_ELIGIBLE_WORKER", worker_id=None, alive=False)
    # A node-aware worker exists but has not been seen recently → registered-but-not-running / dead.
    return ClaimantProbe(ok=False, reason="EP_WORKER_STALE", worker_id=candidate.worker_id, alive=False)


@dataclass(frozen=True)
class ClaimantProbe:
    ok: bool
    reason: str
    worker_id: str | None
    alive: bool


# --------------------------------------------------------------------------------------------------
# Bridge health via the existing ComponentHealth rollup (never re-probe in this read path).
# --------------------------------------------------------------------------------------------------
def _bridge_health(node) -> str:
    """Return the node's order-bridge health as one of OK / DEGRADED / UNOBSERVED.

    Reuses ``reliability.ComponentHealth`` (EXECUTION_PIPELINE). A missing row is UNOBSERVED
    (never treated as healthy — mirrors operational_health's ``_unobserved``). Fail-open on any
    lookup error to UNOBSERVED so this never raises into the caller.
    """
    try:
        from reliability.models import ComponentHealth, Component, HealthStatus

        # NODE-SCOPED ONLY: a per-node order bridge must have its OWN ComponentHealth row. We do NOT
        # fall back to a global/legacy EXECUTION_PIPELINE row — that would infer THIS node's bridge
        # health from a DIFFERENT bridge and could over-report. No node-scoped row ⇒ UNOBSERVED.
        row = (
            ComponentHealth.objects.filter(
                component=Component.EXECUTION_PIPELINE, terminal_node_id=node.id
            )
            .order_by("-updated_at")
            .first()
        )
        if row is None:
            return "UNOBSERVED"
        return "OK" if row.status == HealthStatus.OK else "DEGRADED"
    except Exception:
        return "UNOBSERVED"


# --------------------------------------------------------------------------------------------------
# The execution-path readiness computation.
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionPathReadiness:
    ready: bool
    reason_code: str
    checks: dict = field(default_factory=dict)
    node_id: int | None = None
    node_hostname: str | None = None


def evaluate_execution_path_readiness(account, *, require_worker_liveness: bool = True) -> ExecutionPathReadiness:
    """Concept-C: can a PLACE_ORDER for this account be claimed + dispatched to its node's bridge?

    READ-ONLY, fail-closed, never an order authority. Returns a frozen result with a reason code
    and a per-check breakdown. This is deliberately account-centric (the customer's node is
    derived from the account, server-side) so a caller can never inject an arbitrary node.
    """
    try:
        return _evaluate(account, require_worker_liveness)
    except Exception as exc:  # fail-closed: any error ⇒ not ready
        return ExecutionPathReadiness(
            ready=False,
            reason_code="EP_INDETERMINATE",
            checks={"error": type(exc).__name__},
        )


def _evaluate(account, require_worker_liveness: bool) -> ExecutionPathReadiness:
    from execution.hosted_pin import is_hosted_workspace_account, pin_subsystem_enabled
    from execution.hosted_routing import resolve_hosted_route
    from execution.models import TerminalNode

    checks: dict = {}
    node = getattr(account, "terminal_node", None)
    node_id = getattr(node, "id", None)
    hostname = getattr(node, "hostname", None)

    hosted = is_hosted_workspace_account(account)
    checks["hosted"] = hosted

    # DARK: while the pin subsystem is off, hosted transport resolves to the legacy global bridge
    # for every job, so a hosted account's node-specific path is not the operative one. Report
    # expected-dark rather than over-report readiness. A non-hosted (legacy/Customer-Zero) account
    # keeps the global path and is out of the hosted execution-path scope entirely.
    if hosted and not pin_subsystem_enabled():
        return ExecutionPathReadiness(
            ready=False, reason_code="EP_EXPECTED_DARK",
            checks={**checks, "pin_subsystem": False},
            node_id=node_id, node_hostname=hostname,
        )
    if not hosted:
        return ExecutionPathReadiness(
            ready=False, reason_code="EP_NOT_HOSTED",
            checks={**checks, "note": "legacy/global path — hosted execution-path readiness N/A"},
            node_id=node_id, node_hostname=hostname,
        )

    # 1. Owner-bound single armed route + workspace/account/node AGREEMENT (server-derived).
    route = resolve_hosted_route(account)
    checks["hosted_route"] = {"ok": route.ok, "reason": getattr(route, "reason_code", None)}
    if not route.ok:
        return ExecutionPathReadiness(
            ready=False, reason_code=f"EP_ROUTE:{getattr(route, 'reason_code', 'unknown')}",
            checks=checks, node_id=node_id, node_hostname=hostname,
        )

    # 2. Node ACTIVE + has a configured order bridge.
    if node is None:
        return ExecutionPathReadiness(ready=False, reason_code="EP_NODE_UNBOUND", checks=checks)
    node_active = node.status == TerminalNode.Status.ACTIVE
    bridge_url = (getattr(node, "order_bridge_base_url", "") or "").strip()
    checks["node_active"] = node_active
    checks["bridge_configured"] = bool(bridge_url)
    if not node_active:
        return ExecutionPathReadiness(
            ready=False, reason_code="EP_NODE_NOT_ACTIVE", checks=checks,
            node_id=node_id, node_hostname=hostname)
    if not bridge_url:
        return ExecutionPathReadiness(
            ready=False, reason_code="EP_BRIDGE_UNCONFIGURED", checks=checks,
            node_id=node_id, node_hostname=hostname)

    # 3. An authorized, non-revoked, node-aware order-capable worker exists (and is alive).
    probe = eligible_order_claimant(node)
    checks["claimant"] = {"ok": probe.ok, "reason": probe.reason, "worker_id": probe.worker_id,
                          "alive": probe.alive}
    if not probe.ok:
        # If liveness is not required (e.g. a commission-time check before the worker's first
        # poll), accept a registered node-aware worker even if not-yet-seen.
        if not (not require_worker_liveness and probe.reason == "EP_WORKER_STALE"):
            return ExecutionPathReadiness(
                ready=False, reason_code=probe.reason, checks=checks,
                node_id=node_id, node_hostname=hostname)

    # 4. Bridge health (observability — never HEALTHY when unobserved). FAIL-CLOSED at runtime:
    # DEGRADED ⇒ not ready; UNOBSERVED ⇒ not ready when liveness is required (an unknown bridge is
    # not a proven-dispatchable bridge — mirrors the "unknown ⇒ not ready" contract). UNOBSERVED is
    # RELAXED only at commission (require_worker_liveness=False), where a freshly-activated bridge may
    # not have a health rollup yet.
    bridge = _bridge_health(node)
    checks["bridge_health"] = bridge
    if bridge == "DEGRADED":
        return ExecutionPathReadiness(
            ready=False, reason_code="EP_BRIDGE_UNHEALTHY", checks=checks,
            node_id=node_id, node_hostname=hostname)
    if bridge == "UNOBSERVED" and require_worker_liveness:
        return ExecutionPathReadiness(
            ready=False, reason_code="EP_BRIDGE_UNOBSERVED", checks=checks,
            node_id=node_id, node_hostname=hostname)

    return ExecutionPathReadiness(
        ready=True, reason_code="EP_READY", checks=checks,
        node_id=node_id, node_hostname=hostname)


@dataclass(frozen=True)
class NodeOperational:
    operational: bool
    reason_code: str
    checks: dict = field(default_factory=dict)


def node_execution_operational(node, *, require_worker_liveness: bool = False) -> NodeOperational:
    """Commission-time gate: is this TerminalNode fit to be marked execution-operational?

    A node must NOT be considered operational (allocatable / armable) merely because its
    operator-declared ``status`` is ACTIVE. It also needs: a configured order bridge, and an
    authorized non-revoked node-aware order-capable worker registered for it. Bridge/worker
    liveness is checked when ``require_worker_liveness`` is True (runtime), and relaxed at initial
    commission (the worker may not have polled yet).

    READ-ONLY, fail-closed. This never mutates the node; a caller uses it to REFUSE to advance a
    node into service until it returns operational.
    """
    try:
        from execution.models import TerminalNode

        checks: dict = {}
        if node is None:
            return NodeOperational(False, "NODE_NULL", checks)
        checks["status_active"] = node.status == TerminalNode.Status.ACTIVE
        if not checks["status_active"]:
            return NodeOperational(False, "NODE_STATUS_NOT_ACTIVE", checks)
        bridge_url = (getattr(node, "order_bridge_base_url", "") or "").strip()
        checks["bridge_configured"] = bool(bridge_url)
        if not bridge_url:
            return NodeOperational(False, "NODE_BRIDGE_UNCONFIGURED", checks)
        probe = eligible_order_claimant(node)
        checks["claimant"] = {"ok": probe.ok, "reason": probe.reason, "worker_id": probe.worker_id}
        # At commission we accept a registered-but-not-yet-seen worker (EP_WORKER_STALE) unless
        # liveness is explicitly required; we NEVER accept "no eligible worker at all".
        if probe.reason == "EP_NO_ELIGIBLE_WORKER" or probe.reason == "EP_NODE_NO_HOSTNAME":
            return NodeOperational(False, "NODE_NO_ELIGIBLE_WORKER", checks)
        if require_worker_liveness and not probe.ok:
            return NodeOperational(False, "NODE_WORKER_NOT_LIVE", checks)
        return NodeOperational(True, "NODE_OPERATIONAL", checks)
    except Exception as exc:
        return NodeOperational(False, "NODE_INDETERMINATE", {"error": type(exc).__name__})


# --------------------------------------------------------------------------------------------------
# Operational-health scan (read-only) — the fail-closed conditions that must be VISIBLE so the
# system can never silently present itself as fully executable (AJ#7.2.1 root-cause class).
# --------------------------------------------------------------------------------------------------
def stuck_pending_threshold_seconds() -> int:
    try:
        return int(os.getenv("EXECUTION_STUCK_PENDING_SECONDS", "600"))
    except (TypeError, ValueError):
        return 600


def scan_execution_path_health() -> list[dict]:
    """Return a list of fail-closed execution-path health findings (read-only; never mutates).

    Covers the AJ#7.2.1 gap conditions:
      * ``NODE_NO_ELIGIBLE_WORKER``   — an ACTIVE node has no authorized order-capable worker.
      * ``NODE_WORKER_STALE``         — its node-aware worker exists but has not been seen recently.
      * ``NODE_BRIDGE_UNHEALTHY``     — the node's order bridge health is DEGRADED.
      * ``NODE_PENDING_NO_CLAIMANT``  — PENDING PLACE_ORDER jobs exist for a node with no eligible
                                        claimant (the exact Node-2 silent failure).
      * ``JOB_STUCK_PENDING``         — a PLACE_ORDER job has been PENDING beyond the threshold.

    A caller (ops dashboard / monitor chain / alert sink) surfaces these; this function itself only
    observes. Severity is advisory. Fail-open per node (one node's error never hides another's).
    """
    findings: list[dict] = []
    try:
        from django.utils import timezone as _tz

        from execution.models import ExecutionJob, TerminalNode

        active_nodes = list(TerminalNode.objects.filter(status=TerminalNode.Status.ACTIVE))
        now = _tz.now()
        stuck_cut = now - timedelta(seconds=stuck_pending_threshold_seconds())

        # Per-node pending PLACE_ORDER counts (single grouped query).
        pending_by_node: dict = {}
        for row in (
            ExecutionJob.objects.filter(
                status=ExecutionJob.Status.PENDING, job_type=ExecutionJob.JobType.PLACE_ORDER
            ).values_list("terminal_node_id", flat=True)
        ):
            pending_by_node[row] = pending_by_node.get(row, 0) + 1

        for node in active_nodes:
            try:
                probe = eligible_order_claimant(node)
                bridge_url = (getattr(node, "order_bridge_base_url", "") or "").strip()
                pending = pending_by_node.get(node.id, 0)
                base = {"node_id": node.id, "node_hostname": node.hostname, "pending_orders": pending}
                if not bridge_url:
                    findings.append({**base, "code": "NODE_BRIDGE_UNCONFIGURED", "severity": "warning"})
                if probe.reason == "EP_NO_ELIGIBLE_WORKER":
                    findings.append({**base, "code": "NODE_NO_ELIGIBLE_WORKER",
                                     "severity": "critical" if pending else "warning"})
                elif probe.reason == "EP_WORKER_STALE":
                    findings.append({**base, "code": "NODE_WORKER_STALE", "worker_id": probe.worker_id,
                                     "severity": "critical" if pending else "warning"})
                if pending and not probe.ok:
                    # The exact Node-2 condition: real orders queued, nothing can claim them.
                    findings.append({**base, "code": "NODE_PENDING_NO_CLAIMANT",
                                     "reason": probe.reason, "severity": "critical"})
                if _bridge_health(node) == "DEGRADED":
                    findings.append({**base, "code": "NODE_BRIDGE_UNHEALTHY", "severity": "critical"})
            except Exception as exc:  # fail-open per node
                findings.append({"node_id": getattr(node, "id", None),
                                 "code": "NODE_SCAN_ERROR", "error": type(exc).__name__,
                                 "severity": "warning"})

        # Individually stuck PENDING PLACE_ORDER jobs (any node), beyond the threshold.
        stuck = (
            ExecutionJob.objects.filter(
                status=ExecutionJob.Status.PENDING,
                job_type=ExecutionJob.JobType.PLACE_ORDER,
                created_at__lte=stuck_cut,
            ).values_list("id", "account_id", "terminal_node_id")[:200]
        )
        for job_id, acct_id, node_id in stuck:
            findings.append({"code": "JOB_STUCK_PENDING", "job_id": job_id, "account_id": acct_id,
                             "node_id": node_id, "severity": "critical"})
    except Exception as exc:
        findings.append({"code": "SCAN_INDETERMINATE", "error": type(exc).__name__, "severity": "warning"})
    return findings
