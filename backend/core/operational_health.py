"""core.operational_health — the unified Operational Readiness health framework (ADR-0035).

A PURE, READ-ONLY aggregator that rolls the many existing per-subsystem health signals into ONE
operator view over a small, honest state vocabulary. It **owns no state and writes nothing**: every
probe reads an already-authoritative source defensively (fail-open) and maps that source's own status
into the shared :class:`HealthState`. It never runs a health engine, never mutates a row, never touches a
host, and — the load-bearing principle — **never fabricates a healthy reading**: a subsystem is only
``HEALTHY`` when a real source says so; anything unobserved, dark, or ambiguous degrades, it does not
pass.

State vocabulary (exactly the seven operator states of the Operational Readiness packet):

    HEALTHY          a real source confirms the subsystem is up and nominal
    DEGRADED         running but impaired / stale / partially observed (includes "enabled but no data")
    MAINTENANCE      intentionally paused by an operator (an explicit maintenance signal)
    OFFLINE          a source says the subsystem is down / disconnected / unreachable
    MISCONFIGURED    required configuration/credential/flag posture is missing or inconsistent
    BLOCKED          an upstream dependency or gate prevents the subsystem from being usable
    AWAITING_SPONSOR intentionally DARK, waiting for a Sponsor/host gate (flags OFF by design, host-cert
                     pending) — expected, NOT a failure

``AWAITING_SPONSOR`` is treated as *expected darkness* and does not drag the overall rollup into a fault
state; the four fault states (``OFFLINE`` > ``MISCONFIGURED`` > ``BLOCKED`` > ``DEGRADED``) do.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("guvfx.operational_readiness")


class HealthState:
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    MAINTENANCE = "MAINTENANCE"
    OFFLINE = "OFFLINE"
    MISCONFIGURED = "MISCONFIGURED"
    BLOCKED = "BLOCKED"
    AWAITING_SPONSOR = "AWAITING_SPONSOR"

    ALL = (HEALTHY, DEGRADED, MAINTENANCE, OFFLINE, MISCONFIGURED, BLOCKED, AWAITING_SPONSOR)
    # Fault states that represent a real problem to surface (worst-first). AWAITING_SPONSOR/MAINTENANCE/
    # HEALTHY are deliberately NOT here: expected darkness and nominal health never constitute a fault.
    FAULTS = (OFFLINE, MISCONFIGURED, BLOCKED, DEGRADED)


# Worst-first severity used to roll many subsystems into one overall verdict. A lower index is worse.
_SEVERITY = {
    HealthState.OFFLINE: 0,
    HealthState.MISCONFIGURED: 1,
    HealthState.BLOCKED: 2,
    HealthState.DEGRADED: 3,
    HealthState.MAINTENANCE: 4,
    HealthState.AWAITING_SPONSOR: 5,
    HealthState.HEALTHY: 6,
}


def _worse(a: str, b: str) -> str:
    """Return the worse (lower-severity-index) of two states."""
    return a if _SEVERITY.get(a, 99) <= _SEVERITY.get(b, 99) else b


@dataclass(frozen=True)
class SubsystemHealth:
    """One subsystem's health reading. ``observed`` records whether a real source actually reported —
    an unobserved subsystem never claims HEALTHY, so ``observed=False`` always pairs with a non-healthy
    state and a detail explaining why nothing was seen."""
    name: str
    state: str
    observed: bool
    detail: str = ""
    source: str = ""
    dependency: str = ""   # what it is waiting on / blocked by, when applicable

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "observed": self.observed,
            "detail": self.detail,
            "source": self.source,
            "dependency": self.dependency,
        }


def _ok(name, detail, source):
    return SubsystemHealth(name, HealthState.HEALTHY, True, detail, source)


def _dark(name, detail, source, dependency="Sponsor/host gate"):
    return SubsystemHealth(name, HealthState.AWAITING_SPONSOR, True, detail, source, dependency)


def _unobserved(name, detail, source, state=HealthState.DEGRADED):
    """Enabled/expected but nothing reported — never HEALTHY, so it cannot masquerade as ready."""
    return SubsystemHealth(name, state, False, detail, source)


def _guard(fn):
    """Run a probe; a raising probe fails to a conservative DEGRADED reading, never propagates."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — a health probe must never raise into the rollup
        logger.exception("operational_health: probe %s failed", getattr(fn, "__name__", "?"))
        return SubsystemHealth(getattr(fn, "_subsystem", "unknown"), HealthState.DEGRADED, False,
                               f"probe raised: {type(exc).__name__}", "operational_health")


def _subsystem(name):
    def deco(fn):
        fn._subsystem = name
        return fn
    return deco


# --------------------------------------------------------------------------------------------------
# Directly observable from THIS backend process (real live probes).
# --------------------------------------------------------------------------------------------------

@_subsystem("backend")
def probe_backend() -> SubsystemHealth:
    """The Django backend process itself. It is running (this code executes), so the only real question
    is whether its primary datastore answers — proven by the database probe; here we report process
    liveness + build provenance."""
    try:
        from core.version import provenance
        prov = provenance()
        commit = str(prov.get("git_commit", "unknown"))[:12]
        detail = f"process up; build {commit}"
    except Exception:
        detail = "process up; build unknown"
    return _ok("backend", detail, "core.version.provenance")


@_subsystem("database")
def probe_database() -> SubsystemHealth:
    from django.db import connection
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        return SubsystemHealth("database", HealthState.OFFLINE, True,
                               f"query failed: {type(exc).__name__}", "django.db")
    return _ok("database", f"{connection.vendor} reachable", "django.db")


@_subsystem("cache")
def probe_cache() -> SubsystemHealth:
    from django.conf import settings
    from django.core.cache import cache
    backend = ""
    try:
        backend = settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1]
    except Exception:
        backend = "unknown"
    local = "LocMem" in backend or "Dummy" in backend
    try:
        cache.get("__ops_health_probe__")   # read-only; raises if the cache backend is unreachable
    except Exception as exc:  # noqa: BLE001
        return SubsystemHealth("cache", HealthState.OFFLINE, True,
                               f"{backend} unreachable: {type(exc).__name__}", "django.core.cache")
    if local:
        # A local/dummy cache is fine for dev/test but is NOT the production Redis — report honestly.
        return SubsystemHealth("cache", HealthState.DEGRADED, True,
                               f"{backend} (no shared Redis configured)", "django.core.cache")
    return _ok("cache", f"{backend} reachable", "django.core.cache")


# --------------------------------------------------------------------------------------------------
# Recorded-state sources (reliability ComponentHealth) — infra/workers/MT5/bridge.
# --------------------------------------------------------------------------------------------------

_COMPONENT_STATE_MAP = {
    "OK": HealthState.HEALTHY,
    "STALE": HealthState.DEGRADED,
    "DEGRADED": HealthState.DEGRADED,
    "FAILED": HealthState.OFFLINE,
    "UNKNOWN": HealthState.DEGRADED,   # recorded-but-unknown is not healthy
}


def _component_rollup(name, components, source_label, *, unobserved_state=HealthState.DEGRADED):
    """Roll the latest ComponentHealth rows for a set of reliability Component names into one reading.
    No rows = unobserved (never HEALTHY)."""
    try:
        from reliability.models import ComponentHealth
    except Exception:
        return _unobserved(name, "reliability app unavailable", source_label, unobserved_state)
    rows = list(ComponentHealth.objects.filter(component__in=components))
    if not rows:
        return _unobserved(name, "no ComponentHealth rows recorded (component not reporting)",
                           source_label, unobserved_state)
    worst = HealthState.HEALTHY
    worst_status = "OK"
    for r in rows:
        mapped = _COMPONENT_STATE_MAP.get(r.status, HealthState.DEGRADED)
        if _SEVERITY.get(mapped, 99) < _SEVERITY.get(worst, 99):
            worst, worst_status = mapped, r.status
    return SubsystemHealth(name, worst, True,
                           f"{len(rows)} component row(s); worst={worst_status}", source_label)


@_subsystem("workers")
def probe_workers() -> SubsystemHealth:
    from reliability.constants import Component
    return _component_rollup(
        "workers",
        [Component.INGEST_WORKER, Component.VALIDATE_WORKER,
         Component.SCHEDULER_H1, Component.SCHEDULER_H4, Component.SCHEDULER_M5],
        "reliability.ComponentHealth")


@_subsystem("bridge")
def probe_bridge() -> SubsystemHealth:
    from reliability.constants import Component
    return _component_rollup(
        "bridge", [Component.EXECUTION_PIPELINE], "reliability.ComponentHealth")


@_subsystem("mt5")
def probe_mt5() -> SubsystemHealth:
    from reliability.constants import Component
    return _component_rollup(
        "mt5", [Component.MT5_TERMINAL, Component.MT5_BROKER], "reliability.ComponentHealth")


@_subsystem("guacamole")
def probe_guacamole() -> SubsystemHealth:
    """Guacamole/RemoteApp delivery infrastructure. The backend cannot read-only probe guacd, and there
    is no ComponentHealth row for it, so it is UNOBSERVED here. Because the RemoteApp host (RDS) is an
    un-begun Sponsor/host gate (Hosted Workspace Host Certification), the honest state is AWAITING_SPONSOR
    once the RemoteApp flag is on, otherwise expected-dark."""
    from django.conf import settings
    from core.credentials import is_placeholder
    guac_base = str(getattr(settings, "GUAC_BASE_URL", "") or "")
    if not guac_base or is_placeholder(guac_base):
        return _dark("guacamole", "GUAC_BASE_URL unset — delivery infra not configured (host gate)",
                     "settings.GUAC_BASE_URL")
    return _dark("guacamole", "configured; live guacd/RemoteApp not probed (host-cert pending)",
                 "settings.GUAC_BASE_URL")


# --------------------------------------------------------------------------------------------------
# Recorded-state sources (broker health, agent monitor, operational events).
# --------------------------------------------------------------------------------------------------

_BROKER_HEALTH_MAP = {
    "HEALTHY": HealthState.HEALTHY,
    "DEGRADED": HealthState.DEGRADED,
    "STALE": HealthState.DEGRADED,
    "DISCONNECTED": HealthState.OFFLINE,
    "TOMBSTONED": HealthState.OFFLINE,
    "UNKNOWN": HealthState.DEGRADED,
}


@_subsystem("broker_health")
def probe_broker_health() -> SubsystemHealth:
    """The WP3 continuous broker-health engine (ADR-0030). DARK by default; when off it is
    AWAITING_SPONSOR, otherwise a fleet rollup over BrokerAccountHealth."""
    try:
        from reliability.constants import broker_health_enabled
    except Exception:
        return _unobserved("broker_health", "reliability constants unavailable", "reliability")
    if not broker_health_enabled():
        return _dark("broker_health", "BROKER_CONNECTIVITY_HEALTH_ENABLED off (WP3 engine dark)",
                     "reliability.broker_health", dependency="Sponsor flag enablement")
    from reliability.models import BrokerAccountHealth
    rows = list(BrokerAccountHealth.objects.all())
    if not rows:
        return _unobserved("broker_health", "engine enabled; no health rows yet",
                           "reliability.BrokerAccountHealth")
    worst, worst_state = HealthState.HEALTHY, "HEALTHY"
    counts: dict = {}
    for r in rows:
        counts[r.state] = counts.get(r.state, 0) + 1
        mapped = _BROKER_HEALTH_MAP.get(r.state, HealthState.DEGRADED)
        if _SEVERITY.get(mapped, 99) < _SEVERITY.get(worst, 99):
            worst, worst_state = mapped, r.state
    return SubsystemHealth("broker_health", worst, True,
                           f"{len(rows)} account(s); worst={worst_state}; {counts}",
                           "reliability.BrokerAccountHealth")


# The durable probe STATE (terminal_provisioning agent_health_probe.STATES) mapped to health. This is the
# AUTHORITATIVE signal — the runner always writes ``current_state`` — unlike ``current_band``, which the
# UNCONFIGURED branch leaves STALE. UNCONFIGURED means the monitor is blind (missing config/credential):
# a genuine MISCONFIGURED fault that must never be reported HEALTHY off a stale band.
_AGENT_STATE_MAP = {
    "HEALTHY": HealthState.HEALTHY,
    "READY_UNARMED": HealthState.HEALTHY,          # ready; simply not armed — nominal for a validation agent
    "UNCONFIGURED": HealthState.MISCONFIGURED,
    "INCOMPATIBLE": HealthState.MISCONFIGURED,
    "UNREACHABLE": HealthState.OFFLINE,
    "LISTENING_NO_NEGOTIATE": HealthState.DEGRADED,
    "UNSUPERVISED": HealthState.DEGRADED,
    "SUPERVISION_UNKNOWN": HealthState.DEGRADED,
}
_AGENT_BAND_MAP = {"HEALTHY": HealthState.HEALTHY, "DEGRADED": HealthState.DEGRADED,
                   "UNAVAILABLE": HealthState.OFFLINE}


@_subsystem("agent_monitor")
def probe_agent_monitor() -> SubsystemHealth:
    """The validation/beta agent monitor. Reads the durable coarse projection ONLY (no network probe) and
    WITHOUT creating it — never ``.load()`` (which ``get_or_create``s a row); this must be a pure read.
    Classifies off BOTH ``current_state`` (authoritative) and ``current_band`` and takes the WORSE of the
    two, so the stale-``current_band`` gap (the UNCONFIGURED branch updates state but not band) can never
    hide a MISCONFIGURED/OFFLINE fault behind a leftover HEALTHY band."""
    try:
        from terminal_provisioning.agent_monitor_runner import load_config, state_evidence
        from terminal_provisioning.models import AgentMonitorState
    except Exception:
        return _unobserved("agent_monitor", "terminal_provisioning unavailable", "terminal_provisioning")
    cfg = load_config()
    if not getattr(cfg, "enabled", False):
        return _dark("agent_monitor", "VALIDATION_AGENT_MONITORING_ENABLED off (monitor dark)",
                     "terminal_provisioning.agent_monitor", dependency="Sponsor flag enablement")
    # READ-ONLY: fetch the singleton without get_or_create; a missing row = the monitor has never run.
    row = AgentMonitorState.objects.filter(pk=AgentMonitorState.SINGLETON_ID).first()
    if row is None:
        return _unobserved("agent_monitor", "enabled; monitor has not run yet (no state row)",
                           "terminal_provisioning.agent_monitor")
    ev = state_evidence(row, config=cfg)
    cur = str(ev.get("current_state", "") or "")
    band = str(ev.get("current_band", "") or "")
    # Unknown-but-present values map to a conservative DEGRADED (never HEALTHY); missing values contribute
    # nothing. The reading is the WORST of whatever signals are present.
    s_state = _AGENT_STATE_MAP.get(cur, HealthState.DEGRADED if cur else None)
    s_band = _AGENT_BAND_MAP.get(band, HealthState.DEGRADED if band else None)
    candidates = [s for s in (s_state, s_band) if s is not None]
    if not candidates:
        return _unobserved("agent_monitor", "enabled; no probe recorded yet",
                           "terminal_provisioning.agent_monitor")
    state = candidates[0]
    for s in candidates[1:]:
        state = _worse(state, s)
    open_alerts = ev.get("open_alert_names") or []
    detail = f"state={cur or 'none'}; band={band or 'none'}; open_alerts={list(open_alerts)}"
    return SubsystemHealth("agent_monitor", state, True, detail, "terminal_provisioning.agent_monitor")


@_subsystem("operational_events")
def probe_operational_events() -> SubsystemHealth:
    """The WP5 operational event model (ADR-0032). DARK by default; reports its own enablement and,
    when on, whether any open (unresolved WARNING/ERROR/CRITICAL) events exist fleet-wide."""
    try:
        from operational_events.constants import OPEN_SEVERITIES, operations_events_enabled
    except Exception:
        return _unobserved("operational_events", "operational_events unavailable", "operational_events")
    if not operations_events_enabled():
        return _dark("operational_events", "OPERATIONS_EVENTS_ENABLED off (event model dark)",
                     "operational_events", dependency="Sponsor flag enablement")
    from operational_events.models import OperationalEvent
    open_n = OperationalEvent.objects.filter(resolved=False, severity__in=OPEN_SEVERITIES).count()
    if open_n:
        return SubsystemHealth("operational_events", HealthState.DEGRADED, True,
                               f"{open_n} open (unresolved) event(s)", "operational_events")
    return _ok("operational_events", "enabled; no open events", "operational_events")


# --------------------------------------------------------------------------------------------------
# Hosted Workspace subsystem family (master / execution / delivery / onboarding).
# All DARK by default -> AWAITING_SPONSOR, and host-cert-blocked for the delivery/execution HOST paths.
# --------------------------------------------------------------------------------------------------

@_subsystem("hosted_workspace")
def probe_hosted_workspace() -> SubsystemHealth:
    from hosted_workspace.flags import hosted_persistent_mt5_enabled
    if not hosted_persistent_mt5_enabled():
        return _dark("hosted_workspace", "HOSTED_PERSISTENT_MT5_ENABLED off (master gate dark)",
                     "hosted_workspace.flags", dependency="Sponsor flag enablement")
    # Master on: roll up canonical workspace states (fleet). Any degraded/suspended -> DEGRADED.
    from hosted_workspace.models import HostedMt5Workspace
    from hosted_workspace.state_machine import WorkspaceLifecycleState as S
    rows = list(HostedMt5Workspace.objects.values_list("canonical_state", flat=True))
    if not rows:
        return _unobserved("hosted_workspace", "master enabled; no workspaces", "hosted_workspace")
    bad = {S.DISCONNECTED, S.RECOVERING, S.SUSPENDED}
    worst = HealthState.HEALTHY
    for st in rows:
        if st in bad:
            worst = HealthState.DEGRADED
    return SubsystemHealth("hosted_workspace", worst, True, f"{len(rows)} workspace(s)",
                           "hosted_workspace")


@_subsystem("delivery")
def probe_delivery() -> SubsystemHealth:
    """RemoteApp delivery. Gated by HOSTED_MT5_REMOTEAPP_ENABLED AND the RDS/RemoteApp host gate (which is
    un-begun), so even flag-on it is AWAITING_SPONSOR until host certification. Delivery health is NEVER
    execution readiness."""
    from hosted_workspace.flags import hosted_mt5_remoteapp_enabled, hosted_persistent_mt5_enabled
    if not (hosted_persistent_mt5_enabled() and hosted_mt5_remoteapp_enabled()):
        return _dark("delivery", "RemoteApp delivery flags off (delivery dark)",
                     "hosted_workspace.flags", dependency="Sponsor flag + RDS/RemoteApp host gate")
    return _dark("delivery", "flags on but RDS/RemoteApp host not certified (host-cert pending)",
                 "hosted_workspace.delivery", dependency="RDS/RemoteApp host certification")


@_subsystem("execution")
def probe_execution() -> SubsystemHealth:
    """Execution gate + Hosted Workspace execution enablement. The live bridge gate is the sole order-time
    authority; this reports the gate/flag posture only, never authorises anything."""
    from hosted_workspace.flags import hosted_mt5_execution_enabled, hosted_persistent_mt5_enabled
    try:
        from execution.broker_gate import execution_gate_enabled
        gate_on = execution_gate_enabled()
    except Exception:
        gate_on = False
    hosted_exec = hosted_persistent_mt5_enabled() and hosted_mt5_execution_enabled()
    if not gate_on and not hosted_exec:
        return _dark("execution", "execution gate + hosted execution flags off (execution dark)",
                     "execution.broker_gate", dependency="Sponsor flag + disposable-demo host cert")
    detail = f"gate_enabled={gate_on}; hosted_execution_enabled={hosted_exec}"
    # Enabled but the disposable-demo execution host cert is still pending -> not provably healthy.
    return SubsystemHealth("execution", HealthState.AWAITING_SPONSOR, True, detail,
                           "execution.broker_gate", dependency="disposable-demo execution host cert")


@_subsystem("onboarding")
def probe_onboarding() -> SubsystemHealth:
    """The customer onboarding journey. Unlike delivery/execution it has NO host dependency — when enabled
    it is genuinely serving NEW customers — so it CAN be HEALTHY. "enabled" is a flag, not a health
    confirmation, so we back HEALTHY with a REAL read (a workspace count) — never observed=True off flags
    alone. Onboarding health is strictly "can a NEW customer onboard?"; it does NOT fault on the state of
    existing or decommissioned workspaces (a terminal RETIRED / operator-paused SUSPENDED workspace is a
    per-customer / hosted_workspace concern, not an onboarding-subsystem fault — mirroring
    ``probe_hosted_workspace`` which likewise excludes RETIRED)."""
    from hosted_workspace.flags import hosted_persistent_mt5_enabled, hosted_workspace_onboarding_enabled
    if not (hosted_persistent_mt5_enabled() and hosted_workspace_onboarding_enabled()):
        return _dark("onboarding", "onboarding flags off (journey dark)",
                     "hosted_workspace.flags", dependency="Sponsor flag enablement")
    from hosted_workspace.models import HostedMt5Workspace
    n = HostedMt5Workspace.objects.count()   # the real read backing observed=True (proves the DB path)
    return SubsystemHealth("onboarding", HealthState.HEALTHY, True,
                           f"enabled; journey available ({n} workspace(s))",
                           "hosted_workspace.onboarding")


# The registry — ordered, and grouped so the rollup can report by tier.
_PROBES = (
    probe_backend, probe_database, probe_cache,
    probe_workers, probe_bridge, probe_mt5, probe_guacamole,
    probe_broker_health, probe_agent_monitor, probe_operational_events,
    probe_hosted_workspace, probe_delivery, probe_execution, probe_onboarding,
)


def build_operational_health() -> dict:
    """Aggregate every subsystem probe into one read-only rollup. Deterministic given DB/flag/config
    state (no wall-clock in the payload except an optional caller-stamped time). MUTATES NOTHING.

    ``overall`` is the worst FAULT state present across subsystems (OFFLINE > MISCONFIGURED > BLOCKED >
    DEGRADED); if there is no fault it is HEALTHY when at least one subsystem is genuinely healthy, else
    AWAITING_SPONSOR (everything expected-dark). ``awaiting_sponsor`` lists the intentionally-dark
    subsystems so darkness is never silently counted as health."""
    subsystems = [_guard(p) for p in _PROBES]

    faults = [s for s in subsystems if s.state in HealthState.FAULTS]
    overall = HealthState.HEALTHY
    if faults:
        overall = faults[0].state
        for s in faults[1:]:
            overall = _worse(overall, s.state)
    else:
        healthy = [s for s in subsystems if s.state == HealthState.HEALTHY]
        maint = [s for s in subsystems if s.state == HealthState.MAINTENANCE]
        if maint and not healthy:
            overall = HealthState.MAINTENANCE
        elif not healthy:
            overall = HealthState.AWAITING_SPONSOR   # nothing is a fault, but nothing is proven healthy

    by_state: dict = {}
    for s in subsystems:
        by_state[s.state] = by_state.get(s.state, 0) + 1

    return {
        "overall": overall,
        "healthy": overall == HealthState.HEALTHY,
        "fault_count": len(faults),
        "counts_by_state": by_state,
        "awaiting_sponsor": [s.name for s in subsystems if s.state == HealthState.AWAITING_SPONSOR],
        "faults": [s.as_dict() for s in faults],
        "subsystems": [s.as_dict() for s in subsystems],
    }
