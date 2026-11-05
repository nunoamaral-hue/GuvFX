"""execution.monitoring_onboarding — DARK onboarding integration hooks for the monitoring gateway (ADR-0075).

GFX-MT5-BRIDGE-SCALE-001, Phase 6. Composable, fail-closed hooks the provisioning flow CAN call to reserve
a deterministic monitoring route during onboarding — WITHOUT marking a route ready before reachability +
identity are proven. This packet does NOT wire these inline into the hosted-workspace provisioning writer
(that is a separately-reviewed, flagged deploy step — preserving the canonical state machine); the hooks
exist so that wiring ships gated from its first line. Everything here is a no-op unless the master gate is
on. No production onboarding mutation occurs by importing or shipping this module.
"""
from __future__ import annotations

import logging

from execution import monitoring_allocation as alloc

logger = logging.getLogger("guvfx.execution.monitoring.onboarding")


def _enabled() -> bool:
    try:
        from execution.monitoring_flags import monitoring_gateway_enabled
        return monitoring_gateway_enabled()
    except Exception:  # noqa: BLE001
        return False


def reserve_route_for_workspace(workspace, *, actor: str = "onboarding"):
    """Reserve (idempotently) a monitoring route for a hosted workspace's account on its execution node's
    physical host. Returns the ``RouteResult`` or None when the subsystem is off / preconditions are not
    met (fail-closed). Reserves + registers only; NEVER marks ready (readiness requires a later verified,
    reachable, identity-attested probe). Does not contact a host or arm anything."""
    if not _enabled():
        return None
    account = getattr(workspace, "trading_account", None)
    node = getattr(workspace, "execution_node", None) or getattr(workspace, "workspace_node", None)
    host_key = str(getattr(node, "rdp_host", "") or "").strip()
    if account is None or not host_key:
        return None
    try:
        gw = alloc.ensure_gateway(host_key, actor=actor)
        res = alloc.reserve_route(account, gateway=gw, actor=actor,
                                  workspace_uuid=getattr(workspace, "workspace_uuid", None))
        # Bind the existing per-account endpoint if one is already allocated (REUSE, never replace).
        ep = _existing_endpoint(workspace)
        if res.ok and ep is not None:
            from execution.monitoring_models import MonitoringRoute
            route = MonitoringRoute.objects.get(pk=res.route_id)
            if route.execution_endpoint_id is None:
                alloc.register_route(route, execution_endpoint=ep, local_port=ep.port, actor=actor)
        return res
    except alloc.MonitoringError as e:
        logger.info("monitoring route reserve skipped account=%s host=%s reason=%s",
                    getattr(account, "id", None), host_key, e.reason)
        return None


def sync_routes_from_endpoints(*, actor: str = "monitoring_sync") -> dict:
    """Bootstrap/refresh monitoring routes from the CURRENT per-account READY endpoints (the transition
    path: every live per-account bridge gets a deterministic route record so reconcile can report it).
    DARK (no-op unless master gate on). Idempotent. Never retires an endpoint or contacts a host."""
    if not _enabled():
        return {"skipped": "disabled"}
    from execution.models import HostedExecutionEndpoint
    from execution.monitoring_models import MonitoringRoute
    created = reused = skipped = 0
    eps = (HostedExecutionEndpoint.objects.exclude(state=HostedExecutionEndpoint.State.RETIRED)
           .select_related("workspace", "terminal_node", "trading_account"))
    for ep in eps:
        ws = ep.workspace
        node = ep.terminal_node
        host_key = str(getattr(node, "rdp_host", "") or "").strip()
        if ws is None or not host_key:
            skipped += 1
            continue
        try:
            gw = alloc.ensure_gateway(host_key, actor=actor)
            res = alloc.reserve_route(ep.trading_account, gateway=gw, actor=actor,
                                      workspace_uuid=getattr(ws, "workspace_uuid", None),
                                      execution_endpoint=ep)
            route = MonitoringRoute.objects.get(pk=res.route_id)
            if route.execution_endpoint_id is None:
                alloc.register_route(route, execution_endpoint=ep, local_port=ep.port, actor=actor)
            created += 1 if res.reason == alloc.ALLOC_OK else 0
            reused += 1 if res.reason == alloc.ALLOC_REACTIVATED else 0
        except alloc.MonitoringError:
            skipped += 1
    return {"created": created, "reused": reused, "skipped": skipped}


def _existing_endpoint(workspace):
    try:
        return getattr(workspace, "execution_endpoint", None)
    except Exception:  # noqa: BLE001
        return None
