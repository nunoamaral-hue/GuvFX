"""Validation-agent production-hardening — EXECUTABLE DESIGN SPEC (WS-C/WS-D).

This module is a **pure, side-effect-free reference** for the health model and monitoring calculations
described in ``docs/operations/validation-agent/health-model.json`` and ``monitoring-catalogue.json`` and the
authoritative doc ``docs/VALIDATION_AGENT_PRODUCTION_HARDENING.md``. It is **NOT the agent runtime** and is
imported by **nothing in production** — only by the design tests (``tests_validation_agent_hardening.py``). It
exists so the state machine + the monitoring maths are executable and test-guarded rather than prose-only.

No Django, no host, no I/O, no MetaTrader5 — stdlib only.
"""
from __future__ import annotations

# The six agent-listener health states (must match health-model.json ``states[].name``).
STATES = ("STARTING", "HEALTHY", "DEGRADED", "UNAVAILABLE", "STOPPING", "RECOVERY")

# Attempt reason_codes that mean a validation FAILED at a downstream layer (host IPC / MT5 / broker /
# transport) while the AGENT LISTENER may still be up. Used for the DEGRADED failure-rate signal — NOT for
# agent up/down (that is the readiness probe). Kept in sync with the taxonomy by intent, not import.
DOWNSTREAM_FAILURE_REASONS = frozenset({
    "validation_ipc_unavailable", "validation_busy", "mt5_unavailable", "runtime_unavailable",
    "could_not_verify", "server_unavailable", "login_timeout", "invalid_password", "invalid_login",
    "account_disabled", "server_not_found", "validation_agent_unreachable", "validation_agent_timeout",
})

# Lifecycle intent hints a supervisor/operator supplies alongside the observed readiness signals.
LIFECYCLE_HINTS = (None, "starting", "stopping", "recovery")


def derive_agent_state(*, process_running: bool, socket_listening: bool, negotiate_ok: bool,
                       downstream_failure_rate: float = 0.0, degraded_threshold: float = 0.5,
                       lifecycle=None) -> str:
    """Derive the agent-listener health state from observed readiness signals + a lifecycle hint. Total and
    deterministic; mirrors the transitions in health-model.json.

    Readiness ladder: process_running AND socket_listening AND negotiate_ok.
    - STOPPING  — an operator/supervisor stop is in progress (wins over everything).
    - not ready + lifecycle 'starting'  -> STARTING ; + 'recovery' -> RECOVERY ; else -> UNAVAILABLE.
    - ready + downstream_failure_rate >= degraded_threshold -> DEGRADED (agent up, validations failing).
    - ready otherwise -> HEALTHY.
    Invariant guaranteed here: a downstream failure NEVER yields UNAVAILABLE (that needs a readiness failure)."""
    if lifecycle not in LIFECYCLE_HINTS:
        raise ValueError(f"invalid lifecycle hint: {lifecycle!r}")
    if lifecycle == "stopping":
        return "STOPPING"
    ready = bool(process_running and socket_listening and negotiate_ok)
    if not ready:
        if lifecycle == "starting":
            return "STARTING"
        if lifecycle == "recovery":
            return "RECOVERY"
        return "UNAVAILABLE"
    if float(downstream_failure_rate) >= float(degraded_threshold):
        return "DEGRADED"
    return "HEALTHY"


def window_failure_rate(reason_codes, failing_reasons=DOWNSTREAM_FAILURE_REASONS) -> float:
    """Fraction of attempts in the window whose reason_code is a failure. Empty window -> 0.0 (no evidence of
    failure, not a division error)."""
    codes = list(reason_codes or [])
    if not codes:
        return 0.0
    failed = sum(1 for c in codes if str(c) in failing_reasons)
    return failed / len(codes)


def uptime_ratio(up_samples) -> float:
    """Mean of 0/1 readiness samples over a window (1 == agent_up). Empty -> 0.0 (unknown treated as down for
    a conservative uptime figure)."""
    s = [1 if x else 0 for x in (up_samples or [])]
    return (sum(s) / len(s)) if s else 0.0


def is_connect_timeout_signature(total_ms, connect_timeout_ms: int = 10000, tol_ms: int = 1500) -> bool:
    """True when an end-to-end validation duration is within ``tol_ms`` of the backend->agent CONNECT timeout —
    the tell-tale of a validation_agent_unreachable (the agent was never reached), e.g. ~10s. Distinguishes an
    'agent unreachable' latency from a genuine MT5/broker latency, per the monitoring latency_breakdown."""
    if total_ms is None:
        return False
    return abs(float(total_ms) - float(connect_timeout_ms)) <= float(tol_ms)


def dominant_latency_segment(segments: dict):
    """Given {segment_name: duration_ms}, return (name, ms) of the largest contributor, or (None, 0.0) when
    empty/all-None. Used to attribute a slow validation to the correct layer (agent vs MT5 vs broker)."""
    clean = {k: float(v) for k, v in (segments or {}).items() if v is not None}
    if not clean:
        return (None, 0.0)
    name = max(clean, key=clean.get)
    return (name, clean[name])
