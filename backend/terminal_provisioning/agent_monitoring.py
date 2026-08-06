"""Minimum-hardening WS-E — validation-agent monitoring metric + alert computation.

Pure functions that turn durable sources the platform ALREADY holds (``BrokerAccountValidationAttempt``
reason codes over a rolling window) plus the WS-B readiness probe into the metrics and fired alerts defined
in ``docs/operations/validation-agent/monitoring-catalogue.json``. NO new host agent, NO new model, NO DB
write here — the caller supplies the attempts and the readiness; this module only computes. Alert DELIVERY
is a separate concern (``agent_alert_sink``); this module decides WHAT fired, not who is told.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import agent_health_probe as probe

# Reason-code cohorts (sanitised codes only). Kept explicit so a new reason is a deliberate classification,
# never a silent miscount. ``validation_agent_*`` exist only on the PR-#290 branch; listing them here is
# harmless on main (they simply never match) and future-proofs the metric.
IPC_REASONS = frozenset({"validation_ipc_unavailable", "mt5_unavailable", "could_not_verify"})
BROKER_REASONS = frozenset({"server_unavailable", "login_timeout", "invalid_credentials", "invalid_login",
                            "invalid_password", "account_disabled", "invalid_server"})
TRANSPORT_REASONS = frozenset({"validation_agent_unreachable", "validation_agent_timeout"})
BUSY_REASONS = frozenset({"validation_busy"})
HEALTHY_STATUS = "VALID"     # BrokerAccountValidationAttempt.status for a successful validation

DEFAULT_WINDOW_SECONDS = 3600

# Alert thresholds (design defaults; deliberately conservative for a <=5-10-user manual demo).
DEFAULT_THRESHOLDS = {
    "ipc_failure_rate": 0.5,
    "broker_failure_rate": 0.5,
    "transport_failure_rate": 0.3,
    "busy_rate": 0.5,
    "min_window_samples": 3,     # do not page on 1-2 samples — statistically meaningless
    "crash_loop_min_restarts": 2,  # up->down->up transitions within the window that constitute a crash-loop
}


@dataclass(frozen=True)
class Alert:
    name: str
    severity: str
    detects_state: str
    runbook: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "severity": self.severity, "detects_state": self.detects_state,
                "runbook": self.runbook, "detail": self.detail}


def _created_epoch(a) -> float:
    ts = getattr(a, "created_at", None)
    if ts is None:
        return 0.0
    try:
        return ts.timestamp()
    except (AttributeError, ValueError, OSError):
        return float(ts) if isinstance(ts, (int, float)) else 0.0


def window_rates(attempts, *, now, window_seconds: int = DEFAULT_WINDOW_SECONDS) -> dict:
    """Compute per-cohort failure ratios + counts over the trailing ``window_seconds``. ``attempts`` is any
    iterable of objects with ``.reason_code``/``.status``/``.created_at`` (a queryset or list). Pure."""
    floor = float(now) - int(window_seconds)
    total = 0
    failures = 0
    ipc = broker = transport = busy = 0
    for a in attempts:
        if _created_epoch(a) < floor:
            continue
        total += 1
        reason = (getattr(a, "reason_code", "") or "").strip()
        status = (getattr(a, "status", "") or "").strip()
        if status != HEALTHY_STATUS:
            failures += 1
        if reason in IPC_REASONS:
            ipc += 1
        elif reason in BROKER_REASONS:
            broker += 1
        elif reason in TRANSPORT_REASONS:
            transport += 1
        elif reason in BUSY_REASONS:
            busy += 1
    denom = total or 1
    return {
        "window_seconds": int(window_seconds), "attempts_total": total, "failures_total": failures,
        "ipc_failure_rate": ipc / denom, "broker_failure_rate": broker / denom,
        "transport_failure_rate": transport / denom, "busy_rate": busy / denom,
        "ipc_count": ipc, "broker_count": broker, "transport_count": transport, "busy_count": busy,
    }


def evaluate_alerts(readiness, rates: dict, *, thresholds: dict | None = None,
                    readiness_stale: bool = False, crash_loop_restarts: int = 0) -> list[Alert]:
    """Decide which alerts fire from a readiness observation + windowed rates. Ordered most-severe first.
    ``readiness`` is an :class:`agent_health_probe.AgentReadiness` (or None if the probe did not run).
    ``crash_loop_restarts`` is the up->down->up transition count over the window (from
    :class:`agent_health_probe.ReadinessTracker`.up_down_up): when it reaches the threshold the agent is
    flapping and ``agent_crash_loop`` fires — the supervisor keeps restarting (availability) while this
    brings a human (the ADR-0013-addendum crash-loop-paging contract)."""
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    fired: list[Alert] = []

    if int(crash_loop_restarts) >= th["crash_loop_min_restarts"]:
        fired.append(Alert("agent_crash_loop", "HIGH", "RECOVERY", "restart-procedure",
                           f"up->down->up x{int(crash_loop_restarts)} in window (flapping)"))

    # ── agent liveness / supervision (readiness-derived) ──
    if readiness is not None:
        st = readiness.state
        if st == probe.UNSUPERVISED:
            fired.append(Alert("agent_unsupervised_listener", "HIGH", "UNAVAILABLE", "unsupervised-listener",
                               "a listener answers :8791 but is NOT the supervised service"))
        elif st in (probe.UNREACHABLE, probe.UNCONFIGURED):
            fired.append(Alert("agent_down", "HIGH", "UNAVAILABLE", "agent-unavailable", readiness.reason))
        elif st == probe.LISTENING_NO_NEGOTIATE:
            fired.append(Alert("agent_negotiate_failing", "HIGH", "UNAVAILABLE", "negotiate-failed",
                               readiness.reason))
        elif st == probe.INCOMPATIBLE:
            fired.append(Alert("agent_negotiate_failing", "HIGH", "UNAVAILABLE", "negotiate-failed",
                               "contract_incompatible"))
        elif st == probe.READY_UNARMED:
            fired.append(Alert("validate_login_unavailable", "MEDIUM", "DEGRADED", "validate-login-failed",
                               "up but VALIDATE_LOGIN not available (keyring/op)"))
        elif st == probe.SUPERVISION_UNKNOWN:
            fired.append(Alert("agent_supervision_unknown", "MEDIUM", "DEGRADED", "agent-unavailable",
                               "agent cannot attest supervision (older bundle)"))

    if readiness_stale:
        # dead-man's switch: the prober itself has stalled — treat as agent-down.
        fired.append(Alert("readiness_probe_stale", "HIGH", "UNAVAILABLE", "agent-unavailable",
                           "readiness probe result is stale — the prober may have stalled"))

    # ── windowed failure-rate alerts (only when the window has enough samples) ──
    if rates.get("attempts_total", 0) >= th["min_window_samples"]:
        if rates["transport_failure_rate"] > th["transport_failure_rate"]:
            fired.append(Alert("agent_transport_failing", "HIGH", "UNAVAILABLE", "agent-unavailable",
                               f"transport_failure_rate={rates['transport_failure_rate']:.2f}"))
        if rates["ipc_failure_rate"] > th["ipc_failure_rate"]:
            fired.append(Alert("mt5_ipc_failure_rate_high", "MEDIUM", "DEGRADED", "repeated-ipc-failures",
                               f"ipc_failure_rate={rates['ipc_failure_rate']:.2f}"))
        if rates["broker_failure_rate"] > th["broker_failure_rate"]:
            fired.append(Alert("broker_failure_rate_high", "MEDIUM", "DEGRADED", "repeated-broker-failures",
                               f"broker_failure_rate={rates['broker_failure_rate']:.2f}"))
        if rates["busy_rate"] > th["busy_rate"]:
            fired.append(Alert("validation_wedged", "HIGH", "DEGRADED", "agent-wedged",
                               f"busy_rate={rates['busy_rate']:.2f} (single-flight saturated)"))

    _sev = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    fired.sort(key=lambda a: _sev.get(a.severity, 9))
    return fired


def compute_snapshot(readiness, attempts, *, now, window_seconds: int = DEFAULT_WINDOW_SECONDS,
                     thresholds: dict | None = None, readiness_stale: bool = False) -> dict:
    """Convenience: rates + fired alerts + a coarse band, as one JSON-able snapshot for the Ops surface."""
    rates = window_rates(attempts, now=now, window_seconds=window_seconds)
    alerts = evaluate_alerts(readiness, rates, thresholds=thresholds, readiness_stale=readiness_stale)
    return {
        "band": (readiness.band if readiness is not None else probe.BAND_UNAVAILABLE),
        "state": (readiness.state if readiness is not None else probe.UNCONFIGURED),
        "supervised": (readiness.supervised if readiness is not None else None),
        "rates": rates,
        "alerts": [a.as_dict() for a in alerts],
        "readiness": (readiness.as_dict() if readiness is not None else None),
    }
