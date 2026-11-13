"""execution.monitoring_flags — DARK-by-default gates for the per-host Monitoring Gateway (ADR-0075).

GFX-MT5-BRIDGE-SCALE-001. Same idiom as ``hosted_workspace.flags`` (settings-first-then-env, tolerant
parser, read LIVE so a toggle needs no restart). EVERY accessor defaults OFF (empty default = falsey).

Deliberately NOT placed in ``settings.py`` (that file is production configuration and is owned by a
parallel workstream); these gates live in the ``execution`` app and are read live from the environment.

Everything the GFX-MT5-BRIDGE-SCALE-001 foundation adds is inert until the relevant gate is on:
  * ``monitoring_gateway_enabled`` — master gate for the monitoring-route model + allocator + readiness
    contract. OFF ⇒ the subsystem allocates nothing and evaluates nothing.
  * ``monitoring_gateway_routing_enabled`` — when ON, the backend monitoring client routes through a
    PhysicalHostGateway's single port instead of a per-account bridge endpoint (the scale change). OFF ⇒
    the client uses the legacy per-account endpoint transition path (byte-identical reachability).
  * ``monitoring_route_reconcile_enabled`` — the default-OFF reconciliation command is a dormant no-op
    until on; even on, it only READS and raises operator alerts (never mutates a bridge/ACL/account).
  * ``monitoring_readiness_api_enabled`` — the read-only structured-readiness API stays 404-invisible
    until explicitly on (operational/observability only; never an order path).

None of these gates arms execution, places/sizes/approves an order, contacts a host by itself, mutates
a Tailscale policy, or deletes an account. They gate OBSERVATION/ROUTING scaffolding only.
"""
import os

from django.conf import settings

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    """Settings override wins, else env; empty/falsey default = OFF. Tolerant token set.

    Mirrors ``hosted_workspace.flags._flag`` exactly so operators toggle these the same way."""
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def monitoring_gateway_enabled() -> bool:
    """Master gate for the Monitoring Gateway subsystem (model/allocator/readiness). DEFAULT OFF.

    OFF ⇒ nothing in this subsystem does work: the allocator refuses (fail-closed), the readiness
    evaluator returns an inert subsystem-disabled result, and the reconcile command is a no-op. ON only
    lets the deterministic route model be reserved/registered/verified — it NEVER arms execution or
    authorises an order; the live order-time bridge gate remains the sole order authority."""
    return _flag("MONITORING_GATEWAY_ENABLED")


def monitoring_gateway_routing_enabled() -> bool:
    """Route monitoring reads through the per-host authenticated gateway's SINGLE port (ADR-0075 §A).
    DEFAULT OFF. OFF ⇒ the backend monitoring client uses the legacy per-account endpoint transition
    path (one Tailnet port per account — byte-identical reachability to today). ON ⇒ the client addresses
    the physical host's one gateway port and the gateway routes internally by registered account identity,
    eliminating per-account ACL grants. ANDed with the master gate. Read-only transport only; no orders."""
    return _flag("MONITORING_GATEWAY_ROUTING_ENABLED")


def monitoring_route_reconcile_enabled() -> bool:
    """Gate for the default-OFF ``reconcile_monitoring_routes`` command. DEFAULT OFF, and DISTINCT from the
    master gate (two-level darkness: the command is a dormant no-op unless BOTH are on). Even on it only
    READS route/endpoint/gateway state, classifies faults into durable reason codes, and raises governed
    reliability alerts — it NEVER mutates a bridge, ACL, account, workspace, or places any trading action,
    and it adds NO production schedule."""
    return _flag("MONITORING_ROUTE_RECONCILE_ENABLED")


def monitoring_readiness_api_enabled() -> bool:
    """Gate for the read-only structured monitoring-readiness API. DEFAULT OFF ⇒ the route stays
    404-invisible even when every other monitoring flag is on. Observability only (owner/staff-scoped);
    it exposes the per-account readiness enum + reason code so a client can distinguish 'monitoring route
    ready' from 'unavailable' from 'unsupported' instead of collapsing unavailable into zero. It NEVER
    mutates state, contacts a host, or authorises an order."""
    return _flag("MONITORING_READINESS_API_ENABLED")


def monitoring_enrolment_reconcile_enabled() -> bool:
    """Gate for the APPLY (route-writing) half of the ADR-0103 enrolment reconciler. DEFAULT OFF, and DISTINCT
    from the master gate (two-level darkness, like ``monitoring_route_reconcile_enabled``): the reconciler's
    ``--apply`` is a dormant no-op unless BOTH this and ``monitoring_gateway_enabled`` are on (and the underlying
    ``reserve_route_for_workspace`` is itself gated on the master flag). Even on, ``apply`` only RESERVES and
    REGISTERS deterministic ``MonitoringRoute`` rows from each account's EXISTING endpoint and raises governed
    reliability alerts — it NEVER marks a route READY (that needs the gateway's live identity probe), contacts a
    host, mutates a bridge/ACL/account/workspace, or places any trading action. The read-only enrolment
    PROJECTION (the dashboard's per-account stage) is NOT gated by this — truthful presentation is always on;
    only the route mutation is dark."""
    return _flag("MONITORING_ENROLMENT_RECONCILE_ENABLED")
