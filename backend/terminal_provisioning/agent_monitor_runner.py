"""Monitoring-Runner WS-B/F — the ONE orchestration that turns the DARK monitoring capabilities merged for
the beta validation agent into an ACTUALLY-RUNNING pipeline: probe -> durable hysteresis -> alert policy ->
delivery, in a single deterministic pass.

Design guarantees (each proven by ``tests_agent_monitor_runner``):

  * **Read-only w.r.t. the agent and the estate.** The only outbound action is the signed-NEGOTIATE probe
    (``agent_health_probe.probe_agent_readiness``) and, on a fired alert, a message to the ops alert sink.
    It performs NO broker validation, touches NO broker credential, creates NO validation attempt, starts NO
    MT5, contacts NO broker, and reads/writes NO customer account. The ONLY row it writes is the singleton
    ``AgentMonitorState`` (operational metadata).
  * **Hysteresis + suppression survive restart.** All state lives in the durable ``AgentMonitorState`` row,
    not process memory: recovery requires consecutive successes, and per-alert cooldown is enforced from the
    stored ``last_alerts`` timestamps — a backend redeploy cannot re-page a still-open outage or reset a
    recovery streak.
  * **Never raises for an agent/delivery condition.** Every failure maps to a deterministic ``RunOutcome``
    (and exit code); a delivery failure is itself surfaced (RR-11: an alert that pages nobody is the outage).
  * **Fail-closed and quiet by default.** With monitoring disabled it is inert; with the sink NULL it is a
    dry evaluation. It is not on any request path.

This module is pure orchestration over a supplied ``state`` object + injected ``sink``/``probe_fn``/``now``;
the management command owns loading/saving the row under a single-flight lock.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from . import agent_health_probe as probe
from . import agent_monitoring
from .agent_alert_sink import deliver_alerts

# ── deterministic run outcomes (mapped to CLI exit codes by the command) ──
STATUS_HEALTHY = "healthy"                       # probe ran, agent HEALTHY, nothing to page
STATUS_AGENT_UNHEALTHY = "agent_unhealthy"       # probe ran, agent not HEALTHY (monitoring WORKED)
STATUS_CONFIG_ERROR = "config_error"             # monitor cannot probe (unconfigured) or is disabled
STATUS_PROBE_FAILURE = "probe_failure"           # unexpected orchestration error (belt-and-braces)
STATUS_ALERT_DELIVERY_FAILURE = "alert_delivery_failure"  # >=1 alert fired but could not be delivered
STATUS_DISABLED = "disabled"                     # monitoring flag OFF (safe default posture)

EXIT_CODES = {
    STATUS_HEALTHY: 0,
    STATUS_DISABLED: 0,
    STATUS_AGENT_UNHEALTHY: 10,
    STATUS_CONFIG_ERROR: 20,
    STATUS_PROBE_FAILURE: 30,
    STATUS_ALERT_DELIVERY_FAILURE: 40,
    # overlap-refused (50) is raised by the command's lock, never here.
}

DEFAULT_COOLDOWN_SECONDS = 900          # per-alert durable suppression window (RR-11 storm guard)
DEFAULT_PROBE_INTERVAL_SECONDS = 60     # nominal scheduler tick; drives stale detection
STALE_INTERVAL_MULTIPLIER = 4           # a gap > N intervals since the last run == a coverage gap
# The flap counter (up->down->up transitions) is decayed once the agent has been continuously HEALTHY for
# this many probes, so a crash-loop alert reflects RECENT flapping and does not persist forever after the
# agent has stabilised (the tracker's own counter is a lifetime counter with no window).
FLAP_DECAY_HEALTHY_STREAK = 5


@dataclass
class RunOutcome:
    status: str
    band: str = probe.BAND_UNAVAILABLE
    state: str = probe.UNCONFIGURED
    supervised: object = None
    correlation_id: str = ""
    alerts_fired: list = field(default_factory=list)
    deliveries: list = field(default_factory=list)
    alerts_delivered: int = 0
    alerts_failed: int = 0
    stale: bool = False
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.status, 30)

    def as_dict(self) -> dict:
        return {"status": self.status, "exit_code": self.exit_code, "band": self.band, "state": self.state,
                "supervised": self.supervised, "correlation_id": self.correlation_id,
                "alerts_fired": list(self.alerts_fired), "deliveries": list(self.deliveries),
                "alerts_delivered": self.alerts_delivered, "alerts_failed": self.alerts_failed,
                "stale": self.stale, "reason": self.reason}


@dataclass
class MonitorConfig:
    """Resolved, validated monitor knobs (never carries a secret — the sink resolves its own credential)."""
    enabled: bool = False
    probe_interval_seconds: int = DEFAULT_PROBE_INTERVAL_SECONDS
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    stale_detection_enabled: bool = False


def load_config(settings_obj=None) -> MonitorConfig:
    if settings_obj is None:
        from django.conf import settings as settings_obj  # noqa: PLC0415
    return MonitorConfig(
        enabled=bool(getattr(settings_obj, "VALIDATION_AGENT_MONITORING_ENABLED", False)),
        probe_interval_seconds=_pos_int(getattr(settings_obj, "VALIDATION_AGENT_PROBE_INTERVAL_SECONDS",
                                               DEFAULT_PROBE_INTERVAL_SECONDS), DEFAULT_PROBE_INTERVAL_SECONDS),
        cooldown_seconds=_pos_int(getattr(settings_obj, "VALIDATION_AGENT_ALERT_COOLDOWN_SECONDS",
                                          DEFAULT_COOLDOWN_SECONDS), DEFAULT_COOLDOWN_SECONDS),
        stale_detection_enabled=bool(getattr(settings_obj, "VALIDATION_AGENT_STALE_DETECTION_ENABLED", False)),
    )


def _pos_int(value, default: int) -> int:
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _rehydrate_tracker(state) -> probe.ReadinessTracker:
    """Rebuild the pure hysteresis tracker from the durable row so recovery/backoff streaks continue across
    process restarts instead of resetting."""
    return probe.ReadinessTracker(
        consecutive_unavailable=int(getattr(state, "consecutive_unavailable", 0) or 0),
        consecutive_healthy=int(getattr(state, "consecutive_healthy", 0) or 0),
        alerting=bool(getattr(state, "alerting", False)),
        last_band=str(getattr(state, "current_band", "") or ""),
        up_down_up=int(getattr(state, "flap_count", 0) or 0),
    )


def _epoch(dt) -> float | None:
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (AttributeError, ValueError, OSError):
        return float(dt) if isinstance(dt, (int, float)) else None


def _fingerprint(alert) -> str:
    """Storm-safe dedup key: the alert NAME only. Deliberately excludes ``detail`` (which can carry a
    per-tick counter, e.g. crash-loop ``xN``) so a wiggling detail cannot defeat the cooldown."""
    return str(getattr(alert, "name", "unknown"))


def run_once(*, state, sink, now: float, config: MonitorConfig | None = None, settings_obj=None,
             transport=None, probe_fn=None, correlation_id: str = "", synthetic_readiness=None) -> RunOutcome:
    """Execute ONE monitor pass, MUTATING ``state`` in place (the caller persists it). Returns a
    deterministic :class:`RunOutcome`. Never raises for an agent/delivery condition.

    ``synthetic_readiness`` (test-only) bypasses the network probe with a supplied ``AgentReadiness`` so a
    scheduler/hysteresis path can be exercised without contacting the agent. It NEVER performs any broker or
    customer action regardless."""
    cfg = config or load_config(settings_obj)
    correlation_id = correlation_id or f"agent-monitor-{uuid.uuid4().hex[:12]}"

    if not cfg.enabled:
        return RunOutcome(status=STATUS_DISABLED, correlation_id=correlation_id,
                          reason="VALIDATION_AGENT_MONITORING_ENABLED is off")

    # ── 1. probe (read-only signed NEGOTIATE) ──
    if synthetic_readiness is not None:
        readiness = synthetic_readiness
    else:
        pf = probe_fn or probe.probe_agent_readiness
        readiness = pf(transport=transport)

    band, st = readiness.band, readiness.state
    prev_probe_epoch = _epoch(getattr(state, "last_probe_at", None))

    # record coverage timestamps regardless of outcome (feeds the ops heartbeat / stale evidence)
    _touch_probe_timestamps(state, now=now, band=band)

    # ── 2. unconfigured monitor => config error, NOT a false agent-down page ──
    # A monitor that cannot probe (no base_url/keyring) tells us NOTHING about the agent, so we must NOT
    # advance the hysteresis with a synthetic UNAVAILABLE observation: leave current_band and the
    # consecutive_*/alerting/flap_count fields exactly as they were, or a transient misconfig would fabricate
    # an up->down->up flap on the next real HEALTHY probe. Only the state label + coverage counters move.
    if st == probe.UNCONFIGURED:
        state.previous_state = state.current_state
        state.current_state = st
        state.last_reason = readiness.reason
        _bump_runcount(state)
        return RunOutcome(status=STATUS_CONFIG_ERROR, band=band, state=st, supervised=readiness.supervised,
                          correlation_id=correlation_id, reason="monitor unconfigured (no base_url/keyring)")

    # ── 3. durable hysteresis ──
    was_alerting = bool(getattr(state, "alerting", False))
    tracker = _rehydrate_tracker(state)
    obs = tracker.observe(band)
    now_alerting = bool(obs["alerting"])

    # decay the lifetime flap counter once the agent is durably healthy again, so agent_crash_loop reflects
    # RECENT flapping and clears after the agent stabilises (otherwise it would fire forever once tripped).
    if tracker.consecutive_healthy >= FLAP_DECAY_HEALTHY_STREAK:
        tracker.up_down_up = 0

    # ── 4. stale (coverage-gap) detection — opt-in. NOTE this only catches a scheduler that STOPPED and then
    # RESUMED (it runs inside a probe pass): a fully-dead cron freezes the durable row and is never seen here.
    # True dead-prober detection is an EXTERNAL watchdog over AgentMonitorState.last_probe_at age (surfaced by
    # `agent_monitor_status` / state_evidence.last_probe_age_seconds). Kept OFF by default so a deliberately
    # paused scheduler does not page on resume.
    stale = False
    if cfg.stale_detection_enabled and prev_probe_epoch is not None:
        gap = float(now) - prev_probe_epoch
        stale = gap > (STALE_INTERVAL_MULTIPLIER * cfg.probe_interval_seconds)

    # ── 5. evaluate alerts (readiness + crash-loop + stale; NO customer attempt query in this runner) ──
    empty_rates = {"attempts_total": 0}
    fired = agent_monitoring.evaluate_alerts(
        readiness, empty_rates, readiness_stale=stale, crash_loop_restarts=tracker.up_down_up)

    # recovery: the outage just cleared (was alerting, now not) => one explicit RECOVERED message. It is
    # subject to the SAME durable per-name cooldown as every other alert, so a rapidly-flapping agent cannot
    # emit a recovery storm (it is the crash-loop alert, not a burst of recovered/agent_down pages, that
    # tells the operator about flapping).
    candidates = list(fired)
    recovered = was_alerting and not now_alerting
    if recovered:
        candidates.append(agent_monitoring.Alert(
            name="agent_recovered", severity="RECOVERY", detects_state=band, runbook="agent-recovered",
            detail=f"recovered after {tracker.consecutive_healthy} consecutive HEALTHY probes"))

    # ── 6. durable per-alert cooldown -> the DUE list ──
    # The cooldown map is NEVER blanket-cleared: entries age out per-name over the window, so a still-firing
    # alert (e.g. crash-loop) is not re-paged the instant an outage recovers, and a re-outage inside the
    # window is covered by the crash-loop alert rather than a fresh agent_down storm.
    last_alerts = dict(getattr(state, "last_alerts", {}) or {})
    due, suppressed = _apply_cooldown(candidates, last_alerts, now=now, cooldown=cfg.cooldown_seconds)

    # ── 7. deliver ──
    deliveries = deliver_alerts(sink, due, now=now, correlation_id=correlation_id)
    delivered = sum(1 for d in deliveries if d.get("delivered"))
    # a suppressed(debounced) delivery is not a failure, and a NULL sink ("no_channel_configured") is
    # INTENTIONAL non-delivery (the dark default / --dry-run), not a failure. An undelivered, non-suppressed
    # delivery with any OTHER reason IS a real delivery failure (RR-11).
    failed = sum(1 for d in deliveries if _is_delivery_failure(d))

    # ── 8. persist projection back onto the row (caller saves) ──
    _record_alert_delivery(state, due, deliveries, last_alerts, now=now)
    state.previous_state = state.current_state
    state.current_state = st
    state.current_band = band
    state.supervised = readiness.supervised if isinstance(readiness.supervised, bool) else None
    state.consecutive_healthy = tracker.consecutive_healthy
    state.consecutive_unavailable = tracker.consecutive_unavailable
    state.flap_count = tracker.up_down_up
    state.alerting = now_alerting
    state.last_reason = readiness.reason
    if st != state.previous_state:
        _set_transition(state, now)
    _bump_runcount(state)

    # ── 9. status: derive from what actually FIRED, not the band alone. A HIGH alert (e.g. crash-loop) can
    # fire on a HEALTHY-band probe; reporting exit 0 there would hide the very thing we paged about. HEALTHY
    # is reserved for a healthy band with NO alert firing (a lone recovery message still counts as healthy).
    if failed:
        status = STATUS_ALERT_DELIVERY_FAILURE
    elif band == probe.BAND_HEALTHY and not fired:
        status = STATUS_HEALTHY
    else:
        status = STATUS_AGENT_UNHEALTHY
    return RunOutcome(status=status, band=band, state=st, supervised=readiness.supervised,
                      correlation_id=correlation_id, alerts_fired=[a.as_dict() for a in fired],
                      deliveries=deliveries, alerts_delivered=delivered, alerts_failed=failed, stale=stale,
                      reason=f"{len(due)} due, {delivered} delivered, {failed} failed, "
                             f"{len(suppressed)} cooled-down")


# reasons that are NOT a delivery failure: a debounced duplicate, or a NULL sink (no channel configured).
_BENIGN_NONDELIVERY = frozenset({"no_channel_configured"})


def _is_delivery_failure(d: dict) -> bool:
    return (not d.get("delivered") and not d.get("suppressed")
            and d.get("reason") not in _BENIGN_NONDELIVERY)


def _apply_cooldown(fired, last_alerts: dict, *, now: float, cooldown: int):
    """Split fired alerts into (due, suppressed) using the DURABLE per-name cooldown window."""
    due, suppressed = [], []
    for a in fired:
        fp = _fingerprint(a)
        prev = last_alerts.get(fp)
        prev_ts = None
        if isinstance(prev, dict):
            prev_ts = prev.get("ts")
        if prev_ts is not None and (float(now) - float(prev_ts)) < cooldown:
            suppressed.append(a)
        else:
            due.append(a)
    return due, suppressed


def _record_alert_delivery(state, due, deliveries, last_alerts: dict, *, now: float) -> None:
    """Update the durable ``last_alerts`` cooldown map: stamp EVERY delivered (non-suppressed) alert's name —
    including ``agent_recovered`` — so its own cooldown applies. The map is never blanket-cleared; entries age
    out per-name over the cooldown window."""
    # `due` and `deliveries` are aligned by index (deliver_alerts preserves order).
    for a, d in zip(due, deliveries):
        if d.get("delivered"):
            last_alerts[_fingerprint(a)] = {"ts": float(now), "fingerprint": _fingerprint(a)}
    # last_delivery = coarse worst-case of this pass (for ops evidence; never a secret)
    if deliveries:
        if any(_is_delivery_failure(d) for d in deliveries):
            state.last_delivery = "failed"
        elif any(d.get("delivered") for d in deliveries):
            state.last_delivery = "delivered"
    state.last_alerts = last_alerts


def _touch_probe_timestamps(state, *, now, band) -> None:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(float(now), tz=timezone.utc)
    state.last_probe_at = dt
    if band == probe.BAND_HEALTHY:
        state.last_healthy_at = dt


def _set_transition(state, now) -> None:
    from datetime import datetime, timezone
    state.last_transition_at = datetime.fromtimestamp(float(now), tz=timezone.utc)


def _bump_runcount(state) -> None:
    state.run_count = int(getattr(state, "run_count", 0) or 0) + 1


# ── read-only ops evidence (WS-I) — sanitised; NEVER a token/chat-id/credential/customer datum ──
def state_evidence(state, *, config: MonitorConfig | None = None, settings_obj=None, now: float | None = None,
                   sink_channel: str = "", sink_owner: str = "") -> dict:
    """Coarse, staff-only snapshot of the durable monitor row for the Ops surface. Everything here is already
    safe to log: enums, counters, timestamps, a sanitised reason. It deliberately omits any destination
    (chat id / recipient / token)."""
    cfg = config or load_config(settings_obj)
    last_probe = _epoch(getattr(state, "last_probe_at", None))
    age = None
    if last_probe is not None and now is not None:
        age = max(0.0, float(now) - last_probe)
    return {
        "monitoring_enabled": cfg.enabled,
        "scheduler_interval_seconds": cfg.probe_interval_seconds,
        "current_state": getattr(state, "current_state", "") or "",
        "current_band": getattr(state, "current_band", "") or "",
        "supervised": getattr(state, "supervised", None),
        "alerting": bool(getattr(state, "alerting", False)),
        "consecutive_healthy": int(getattr(state, "consecutive_healthy", 0) or 0),
        "consecutive_unavailable": int(getattr(state, "consecutive_unavailable", 0) or 0),
        "flap_count": int(getattr(state, "flap_count", 0) or 0),
        "last_reason": getattr(state, "last_reason", "") or "",
        "last_delivery": getattr(state, "last_delivery", "") or "",
        "last_probe_at": last_probe,
        "last_probe_age_seconds": age,
        "last_healthy_at": _epoch(getattr(state, "last_healthy_at", None)),
        "last_transition_at": _epoch(getattr(state, "last_transition_at", None)),
        "run_count": int(getattr(state, "run_count", 0) or 0),
        "open_alert_names": sorted((getattr(state, "last_alerts", {}) or {}).keys()),
        "alert_channel": sink_channel,
        "alert_owner": sink_owner,
    }
