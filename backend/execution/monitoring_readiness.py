"""execution.monitoring_readiness — the per-account monitoring READINESS contract (ADR-0075, Phase 5).

"Broker connected" is NOT sufficient for monitoring readiness. A route becomes MONITORING_READY only when
ALL eight gates pass:

  1. workspace active and not tombstoned;
  2. terminal identity matches the expected account/login/server;
  3. gateway / bridge process healthy;
  4. backend can reach the monitoring boundary;
  5. authenticated health/identity response matches;
  6. the capability the dashboard needs is advertised;
  7. freshness within policy;
  8. no duplicate route.

The result is a STRUCTURED value: a readiness enum (the UI/API distinction — connected-to-broker /
monitoring-route-ready / monitoring-partial / monitoring-unavailable / unsupported), a durable reason
code (the precise Phase-1 cause — ``account_not_demo`` → feature_disabled, ``mt5_init_failed`` →
terminal_init_failed, ACL/unreachable → endpoint_unreachable, …), and the eight gate booleans. It NEVER
collapses "unavailable" into "zero": a flat (0-position) READY account and an UNAVAILABLE account are
different results.

This COMPOSES WITH, and does not replace, the existing ADR-0072 workspace monitoring eligibility
(``execution.readiness.evaluate_monitoring_readiness``): that decides whether the workspace is connected +
identity-matched + customer-confirmed + fresh; this adds the transport/route gates on top.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from execution import monitoring_models as mm

# Freshness bound for a route's last successful verification (mirrors the workspace observation freshness).
ROUTE_FRESH_SECONDS = 300

# UI/API distinction states (packet Phase 5) — kept separate from the stored Readiness enum so the API can
# describe "connected to broker but route not ready" without overloading a single value.
UI_CONNECTED_TO_BROKER = "connected_to_broker"
UI_TERMINAL_AVAILABLE = "terminal_available"
UI_MONITORING_ROUTE_READY = "monitoring_route_ready"
UI_MONITORING_PARTIAL = "monitoring_partial"
UI_MONITORING_UNAVAILABLE = "monitoring_unavailable"
UI_UNSUPPORTED = "unsupported"


@dataclass
class ReadinessResult:
    readiness: str                      # one of MonitoringRoute.Readiness
    reason_code: str
    ui_state: str
    gates: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"readiness": self.readiness, "reason_code": self.reason_code,
                "ui_state": self.ui_state, "gates": dict(self.gates)}


def _fresh(ts, now, bound=ROUTE_FRESH_SECONDS) -> bool:
    if ts is None:
        return False
    age = (now - ts).total_seconds()
    return 0 <= age <= bound


def _active_route_count(account_id) -> int:
    return (mm.MonitoringRoute.objects.filter(trading_account_id=account_id)
            .exclude(state=mm.MonitoringRoute.State.TOMBSTONED).count())


def compute_gates(route, probe, *, now=None, duplicate_count=None) -> dict:
    """The eight gate booleans. Pure given ``route``, ``probe`` and ``now`` (and an optional precomputed
    duplicate_count so callers can avoid a per-route query in a batch)."""
    now = now or timezone.now()
    R = mm.MonitoringRoute
    gw_healthy = bool(route.gateway.is_routable) if route.gateway_id else bool(route.execution_endpoint_id)
    if duplicate_count is None:
        duplicate_count = _active_route_count(route.trading_account_id)
    acct = route.trading_account
    workspace_active = (route.state in R._ACTIVE_STATES
                        and getattr(acct, "disconnected_at", None) is None)
    capability_ok = probe.reason_code != mm.RC_FEATURE_DISABLED
    return {
        "g1_workspace_active": bool(workspace_active),
        "g2_identity_match": probe.identity_ok is True,
        "g3_boundary_healthy": gw_healthy,
        "g4_reachable": bool(probe.reachable),
        "g5_auth_identity_match": probe.identity_ok is True,
        "g6_capability_advertised": bool(capability_ok),
        "g7_fresh": _fresh(route.last_verified_at, now) or probe.positions_ok is True,
        "g8_no_duplicate_route": duplicate_count <= 1,
    }


def evaluate_route_readiness(route, probe, *, now=None, duplicate_count=None) -> ReadinessResult:
    """Deterministic readiness decision from a route + its probe result. Most-specific-first so each reason
    code stays reachable. Fail-closed — any ambiguity is UNAVAILABLE/UNKNOWN, never a false READY."""
    now = now or timezone.now()
    R = mm.MonitoringRoute
    if route is None:
        return ReadinessResult(R.Readiness.UNSUPPORTED, mm.RC_NO_ENDPOINT, UI_UNSUPPORTED, {})
    gates = compute_gates(route, probe, now=now, duplicate_count=duplicate_count)

    # Integrity / lifecycle terminal states first.
    if route.state == R.State.QUARANTINED:
        return ReadinessResult(R.Readiness.MONITORING_UNAVAILABLE, "route_integrity_violation",
                               UI_MONITORING_UNAVAILABLE, gates)
    if route.state == R.State.TOMBSTONED:
        return ReadinessResult(R.Readiness.MONITORING_UNAVAILABLE, mm.RC_TOMBSTONED,
                               UI_MONITORING_UNAVAILABLE, gates)
    if not gates["g8_no_duplicate_route"]:
        return ReadinessResult(R.Readiness.MONITORING_UNAVAILABLE, mm.RC_DUPLICATE_ROUTE,
                               UI_MONITORING_UNAVAILABLE, gates)
    if not gates["g1_workspace_active"]:
        return ReadinessResult(R.Readiness.MONITORING_UNAVAILABLE, mm.RC_WORKSPACE_NOT_READY,
                               UI_MONITORING_UNAVAILABLE, gates)
    if not gates["g3_boundary_healthy"]:
        return ReadinessResult(R.Readiness.MONITORING_UNAVAILABLE, mm.RC_GATEWAY_UNHEALTHY,
                               UI_MONITORING_UNAVAILABLE, gates)
    if not gates["g4_reachable"]:
        return ReadinessResult(R.Readiness.MONITORING_UNAVAILABLE, probe.reason_code or mm.RC_UNREACHABLE,
                               UI_MONITORING_UNAVAILABLE, gates)
    # Reachable. Identity attestation is the authority for "connected to the RIGHT terminal".
    if not gates["g2_identity_match"]:
        # Reachable but identity not attested — classify by the probe's precise reason (e.g. init-failed
        # vs mismatch). "connected to broker" is NOT claimed, because identity is unproven.
        return ReadinessResult(R.Readiness.MONITORING_UNAVAILABLE,
                               probe.reason_code or mm.RC_IDENTITY_MISMATCH,
                               UI_MONITORING_UNAVAILABLE, gates)
    # Identity matched. If positions read fine → READY (flat 0-position is READY, NOT unavailable).
    if probe.positions_ok is True and gates["g6_capability_advertised"]:
        if gates["g7_fresh"]:
            return ReadinessResult(R.Readiness.MONITORING_READY, probe.reason_code or mm.RC_OK,
                                   UI_MONITORING_ROUTE_READY, gates)
        return ReadinessResult(R.Readiness.MONITORING_PARTIAL, mm.RC_STALE_HEALTH,
                               UI_MONITORING_PARTIAL, gates)
    # Identity matched (terminal available, equity-level monitoring works) but positions unavailable →
    # PARTIAL with the precise positions reason (feature_disabled / terminal_init_failed / …). The UI can
    # show "connected, positions unavailable" instead of a misleading zero.
    return ReadinessResult(R.Readiness.MONITORING_PARTIAL, probe.reason_code or mm.RC_FEATURE_DISABLED,
                           UI_MONITORING_PARTIAL, gates)
