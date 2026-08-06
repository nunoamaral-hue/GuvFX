"""Minimum-hardening WS-G — Operations-surface presenter for validation-agent status.

Two audiences, one source snapshot (``agent_monitoring.compute_snapshot``):

  - **customer_safe**: a boolean availability + ONE neutral sentence. NEVER a reason code, host detail,
    supervision flag, alert, metric, env value, key, token, credential or stack trace. A customer never
    learns that the agent is "unsupervised" or that a probe "connect-timed-out" — only whether validation is
    available right now, phrased so it never implies the customer's details are wrong.
  - **operator_safe**: the full operational picture (state, band, sanitised reason, fired alerts, windowed
    rates, supervision) — but still only the SANITISED fields the snapshot already carries; there is no raw
    agent string, exception text, secret, env var or path anywhere in the snapshot, so operator_safe cannot
    leak one either.

The invariant (customer_safe carries none of the forbidden keys/values) is enforced here AND asserted by
tests — it is the whole point of the workstream.
"""
from __future__ import annotations

from . import agent_health_probe as probe

CUSTOMER = "customer_safe"
OPERATOR = "operator_safe"

# Keys that must NEVER appear in the customer payload (defence-in-depth over the neutral copy).
_CUSTOMER_FORBIDDEN_KEYS = frozenset({
    "reason", "state", "band", "supervised", "alerts", "rates", "readiness", "layers", "correlation_id",
    "detail", "elapsed_ms", "validate_login_available",
})

# Neutral, customer-safe copy keyed by coarse band. No internal vocabulary; never blames the customer.
_CUSTOMER_COPY = {
    probe.BAND_HEALTHY: ("available", "Broker validation is available."),
    probe.BAND_DEGRADED: ("temporarily_unavailable",
                          "Broker validation is temporarily unavailable. Please try again shortly — there is "
                          "nothing you need to change."),
    probe.BAND_UNAVAILABLE: ("temporarily_unavailable",
                            "Broker validation is temporarily unavailable. Please try again shortly — there "
                            "is nothing you need to change."),
}


def present_customer(snapshot: dict) -> dict:
    """Customer-facing status: availability + one neutral sentence ONLY."""
    band = (snapshot or {}).get("band", probe.BAND_UNAVAILABLE)
    status, message = _CUSTOMER_COPY.get(band, _CUSTOMER_COPY[probe.BAND_UNAVAILABLE])
    return {"available": band == probe.BAND_HEALTHY, "status": status, "message": message}


def present_operator(snapshot: dict) -> dict:
    """Operator-facing status: the full sanitised operational picture for the Ops surface."""
    snap = snapshot or {}
    return {
        "state": snap.get("state", probe.UNCONFIGURED),
        "band": snap.get("band", probe.BAND_UNAVAILABLE),
        "supervised": snap.get("supervised", None),
        "reason": (snap.get("readiness") or {}).get("reason", ""),
        "alerts": snap.get("alerts", []),
        "rates": snap.get("rates", {}),
        "correlation_id": (snap.get("readiness") or {}).get("correlation_id", ""),
    }


def present_agent_status(snapshot: dict, *, audience: str) -> dict:
    """Present the agent status for the given audience. Unknown audience fails SAFE to the customer view."""
    if audience == OPERATOR:
        return present_operator(snapshot)
    return present_customer(snapshot)


def customer_payload_is_safe(payload: dict) -> bool:
    """True iff a customer payload contains NONE of the forbidden operational keys. Used by the audience
    invariant test; cheap enough to also assert at the call site if desired."""
    return not (_CUSTOMER_FORBIDDEN_KEYS & set(payload or {}))
