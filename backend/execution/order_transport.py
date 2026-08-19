"""ADR-0034 Execution Engine — per-node ORDER-TRANSPORT selection seam (Closed-Beta co-residency).

**Why this exists.** Before the ADR-0044 host co-residency amendment a hosted beta tenant ran on its OWN
physical host, so its order bridge and Customer Zero's legacy bridge were naturally different processes.
Co-residency put both on ONE box that shares ONE global order-bridge URL (``AGENT_ORDER_BASE``). A HOSTED
(Provider-B) order MUST reach a bridge running ``MT5_REQUIRE_IDENTITY_PIN=1`` (the per-job pin binds the
order to the tenant's OWN terminal) — but that same global bridge is Customer Zero's LEGACY bridge, which
must never be pin-forced. This module makes the order destination FOLLOW THE JOB'S AUTHORITATIVE EXECUTION
NODE:

  * a HOSTED (Provider-B) job -> its node's ``order_bridge_base_url`` (a dedicated pin-enforcing bridge),
    and FAILS CLOSED if that node has no explicit endpoint — it is NEVER routed to the global bridge;
  * a LEGACY (non-hosted / Provider-A / Customer Zero) job -> the existing global ``AGENT_ORDER_BASE``,
    byte-for-byte unchanged.

**It keys the decision on the SAME canonical classifier the identity-pin injection and the claim-entitlement
gate use** (``execution.hosted_pin.is_hosted_workspace_account``) — so a job routes to a per-node bridge iff
it is a hosted job whose pin that bridge will enforce, NEVER on the mere presence of a node binding. This is
essential for Customer-Zero safety: CZ's OWN jobs are node-bound to CZ's node
(``ExecutionJob.terminal_node`` is "Snapshotted from account.terminal_node at job creation"), yet they must
still use the global bridge — which they do, because CZ classifies non-hosted.

Fail-closed on every ambiguity. **DARK:** while the hosted subsystem flag is off, ``pin_subsystem_enabled()``
is False, EVERY job classifies non-hosted -> global, so the dispatch path is byte-for-byte the pre-seam
behaviour. The module performs NO order, attach, or host action; it selects a URL only. Secret-free.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── order-transport resolution reason codes (stable, secret-free) ──
OT_LEGACY_GLOBAL = "order_transport_legacy_global"                  # non-hosted / dark -> global AGENT_ORDER_BASE
OT_NODE_OK = "order_transport_node_ok"                              # hosted -> resolved node endpoint
OT_NODE_UNBOUND = "order_transport_node_unbound"                    # hosted job with no node snapshot
OT_NODE_MISMATCH = "order_transport_node_mismatch"                  # hosted job node != account node
OT_ENDPOINT_UNCONFIGURED = "order_transport_endpoint_unconfigured"  # hosted node has no order-bridge URL
OT_RESOLVE_ERROR = "order_transport_resolve_error"                  # any error while resolving a hosted route
# ── P0-B1 per-tenant transport reason codes ──
OT_ENDPOINT_NOT_READY = "order_transport_endpoint_not_ready"        # per-tenant endpoint exists but not READY
OT_ENDPOINT_ACCOUNT_MISMATCH = "order_transport_endpoint_account_mismatch"  # endpoint owner != job account


@dataclass(frozen=True)
class OrderTransport:
    """Resolved order-bridge destination for ONE execution job.

    ``ok`` gates dispatch. When ``ok`` and ``hosted``, ``base_url`` is the per-node pin-enforcing bridge;
    when ``ok`` and not ``hosted`` it is the global legacy bridge. When NOT ``ok`` the caller MUST refuse
    the order — it is never dispatched to any bridge (in particular, a failed HOSTED resolution never
    yields the global bridge)."""
    ok: bool
    reason_code: str
    base_url: str = ""
    hosted: bool = False

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason_code": self.reason_code,
                "base_url": self.base_url, "hosted": self.hosted}


def _clean(url) -> str:
    return str(url or "").strip().rstrip("/")


def resolve_order_transport(job, *, global_base_url) -> OrderTransport:
    """Resolve the order-bridge base URL for ``job`` from its AUTHORITATIVE execution node.

    LEGACY (non-hosted / dark subsystem): ``(ok=True, OT_LEGACY_GLOBAL, <global>, hosted=False)`` —
    byte-identical to the pre-seam path (Customer Zero / Provider A unaffected, whatever their node
    binding).

    HOSTED (Provider-B, subsystem on): the job's snapshotted node MUST exist AND agree with the account's
    current node (the ``resolve_hosted_route`` agreement invariant), AND that node MUST carry an explicit
    ``order_bridge_base_url`` -> ``(ok=True, OT_NODE_OK, <node url>, hosted=True)``. Otherwise fail closed
    (``ok=False``) with a specific reason — and NEVER the global bridge for a hosted job.
    """
    try:
        from execution.hosted_pin import is_hosted_workspace_account, pin_subsystem_enabled
        account = getattr(job, "account", None)
        # DARK / non-hosted -> the global legacy bridge, unchanged. The flag is checked first (cheap); a
        # dark subsystem dereferences no account state beyond what the classifier already reads.
        if not pin_subsystem_enabled() or not is_hosted_workspace_account(account):
            return OrderTransport(True, OT_LEGACY_GLOBAL, _clean(global_base_url), hosted=False)
    except Exception:  # noqa: BLE001
        # A classification error is ambiguous. Do NOT risk routing an unclassifiable job to Customer
        # Zero's global bridge; fail closed. (In practice the same classifier already ran at job save,
        # ``inject_identity_pin``.)
        return OrderTransport(False, OT_RESOLVE_ERROR, "", hosted=True)

    # HOSTED path — resolve STRICTLY from an authoritative binding; never fall back to the global bridge.
    try:
        # P0-B1: when per-tenant transport is on, route to the account's OWN endpoint (a dedicated bridge
        # process on a unique host:port), NOT the node's single URL — so two tenants on one node reach two
        # different bridges/terminals. DARK-safe: OFF ⇒ the exact per-node resolution below (unchanged).
        from hosted_workspace.flags import hosted_per_tenant_transport_enabled
        if hosted_per_tenant_transport_enabled():
            return _resolve_per_tenant_endpoint(job, account)

        node_id = getattr(job, "terminal_node_id", None)
        if node_id is None:
            return OrderTransport(False, OT_NODE_UNBOUND, "", hosted=True)
        acct_node_id = getattr(account, "terminal_node_id", None)
        if acct_node_id is None or node_id != acct_node_id:
            return OrderTransport(False, OT_NODE_MISMATCH, "", hosted=True)
        node = getattr(job, "terminal_node", None)
        base = _clean(getattr(node, "order_bridge_base_url", "")) if node is not None else ""
        if not base:
            return OrderTransport(False, OT_ENDPOINT_UNCONFIGURED, "", hosted=True)
        return OrderTransport(True, OT_NODE_OK, base, hosted=True)
    except Exception:  # noqa: BLE001 — any resolution error on the hosted path fails closed (never global)
        return OrderTransport(False, OT_RESOLVE_ERROR, "", hosted=True)


def _resolve_per_tenant_endpoint(job, account) -> OrderTransport:
    """P0-B1 — resolve a hosted job to its OWN account's per-tenant execution endpoint (fail-closed).

    Invariant enforced here (the packet's PHASE-5 requirement): a job for Customer A can resolve ONLY
    Customer A's endpoint. The lookup is keyed on the JOB'S account, the loaded row's owner is re-asserted
    against that account (defence in depth), the endpoint MUST be READY, and — preserving the existing
    node-agreement contract — the endpoint's node MUST equal the job's snapshotted node. Any failure refuses
    the order; it is NEVER routed to the node URL or the global bridge, and NEVER to another tenant."""
    from execution.models import HostedExecutionEndpoint

    acct_id = getattr(job, "account_id", None) or getattr(account, "id", None)
    if acct_id is None:
        return OrderTransport(False, OT_ENDPOINT_ACCOUNT_MISMATCH, "", hosted=True)
    ep = (HostedExecutionEndpoint.objects
          .filter(trading_account_id=acct_id)
          .exclude(state=HostedExecutionEndpoint.State.RETIRED)
          .first())
    if ep is None:
        return OrderTransport(False, OT_ENDPOINT_UNCONFIGURED, "", hosted=True)
    # Re-assert ownership against the loaded row (a future query/join change can never silently cross tenants).
    if ep.trading_account_id != acct_id:
        return OrderTransport(False, OT_ENDPOINT_ACCOUNT_MISMATCH, "", hosted=True)
    # Preserve the node-agreement contract AND the DARK per-node behaviour byte-for-byte: a node-unbound job
    # is refused (the flag-OFF path returns OT_NODE_UNBOUND here), so enabling the flag can never make a
    # deliberately node-unbound account executable via a stale endpoint.
    node_id = getattr(job, "terminal_node_id", None)
    if node_id is None:
        return OrderTransport(False, OT_NODE_UNBOUND, "", hosted=True)
    if ep.terminal_node_id != node_id:
        return OrderTransport(False, OT_NODE_MISMATCH, "", hosted=True)
    if ep.state != HostedExecutionEndpoint.State.READY:
        return OrderTransport(False, OT_ENDPOINT_NOT_READY, "", hosted=True)
    base = _clean(ep.base_url)
    if not base:
        return OrderTransport(False, OT_ENDPOINT_UNCONFIGURED, "", hosted=True)
    return OrderTransport(True, OT_NODE_OK, base, hosted=True)
