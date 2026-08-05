"""WS-D (2026-08-05) — support-grade broker-validation TIMELINE (operator/admin only).

Goal: no SSH should be required to understand a validation failure. Given a correlation id, assemble an
ordered, customer-safe timeline of the validation pipeline from **durable records the backend already holds**:

  * the ``BrokerAccountValidationAttempt`` (final outcome: status / reason_code / is_demo / server /
    login_masked / trigger / created_at) — the authoritative terminal record;
  * the ``CREDENTIAL_ACCESSED`` ``AuditEvent`` for the same correlation window (the real decrypt timestamp);
  * (when the operational-events subsystem is armed) any ``VALIDATION_STAGE`` ``OperationalEvent`` rows.

The per-stage ✓/✕ is DERIVED from the outcome ``reason_code`` via ``_REASON_FURTHEST_OK`` — the classifier
already localises the failure (WS-A), so the reason tells us the furthest stage the run reached. Fine-grained
agent-internal per-stage *timings* (MT5-launch → GUI → IPC → broker-TCP) live only in the agent's on-host
diagnostic artefact; surfacing those without SSH needs the agent to forward its sanitised operator summary in
the VALIDATE_LOGIN response (a separate, gated agent + host change — see docs). This module deliberately does
NOT depend on that, and never instruments the validation hot path.

SECRET-SAFE: only allow-listed, already-masked fields are emitted (no password/ciphertext/host path/session
id/pid). The stage labels are generic; the operator detail is a human sentence, never a raw host artefact.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# ── canonical support timeline (ordered). Each stage: internal key + operator label + customer-safe label. ──
STAGES = (
    ("api_received", "API received the request", "We received your request"),
    ("credential_decrypted", "Credential decrypted at point of use", "Prepared your saved credentials"),
    ("envelope_sealed", "Credential envelope sealed", "Secured your credentials"),
    ("request_signed", "Signed request built", "Started the secure validation session"),
    ("agent_received", "Validation agent received the request", "Reached the validation service"),
    ("mt5_launched", "Validation terminal / local IPC ready", "Started the broker session"),
    ("broker_login", "Broker login attempted", "Contacted your broker"),
    ("broker_response", "Broker responded", "Your broker responded"),
    ("persisted", "Result persisted", "Recorded the result"),
    ("browser_response", "Response returned to the browser", "Showed you the result"),
)
_STAGE_KEYS = [k for k, _o, _c in STAGES]
_STAGE_INDEX = {k: i for i, k in enumerate(_STAGE_KEYS)}

# The furthest stage a run reached, keyed by outcome reason. The NEXT stage after this is the failing one.
# Success reaches the end. A broker rejection means the broker WAS reached (fail at broker_response). A
# host/agent-IPC failure never reaches the broker (fail at mt5_launched). Signing/config failures never reach
# the agent. Anything unknown is conservative (agent_received) — never claims the broker was reached.
_LAST = _STAGE_INDEX["browser_response"]
_REASON_FURTHEST_OK = {
    # success — full pipeline
    "demo_ok": _LAST, "is_demo": _LAST, "verified": _LAST, "live_detected": _LAST,
    # broker REACHED, then rejected/unavailable → fail at broker_response
    "invalid_password": _STAGE_INDEX["broker_login"], "invalid_login": _STAGE_INDEX["broker_login"],
    "account_disabled": _STAGE_INDEX["broker_login"], "server_not_found": _STAGE_INDEX["broker_login"],
    "classification_mismatch": _STAGE_INDEX["broker_login"], "server_unavailable": _STAGE_INDEX["broker_login"],
    # local validation-host / MT5 IPC failure → terminal launched, IPC never ready; broker NOT reached
    "validation_ipc_unavailable": _STAGE_INDEX["agent_received"],
    # TRANSPORT ambiguity (review WS-P2): login_timeout AND bridge_unavailable both come from the SAME
    # backend transport(base,req) call — a no-response timeout (possibly a bare connect timeout) can NEVER
    # confirm the agent received the request, so both stop conservatively at request_signed (fail at
    # agent_received). Never claim the agent/broker was reached from a black-box transport failure.
    "login_timeout": _STAGE_INDEX["request_signed"], "bridge_unavailable": _STAGE_INDEX["request_signed"],
    "validation_runner_unavailable": _STAGE_INDEX["request_signed"],
    "validation_runner_timeout": _STAGE_INDEX["request_signed"],
    # agent/host platform conditions that PROVE the agent processed the request (agent-origin reasons)
    "mt5_unavailable": _STAGE_INDEX["agent_received"], "runtime_unavailable": _STAGE_INDEX["agent_received"],
    "validation_baseline_dirty": _STAGE_INDEX["agent_received"],
    "isolation_check_failed": _STAGE_INDEX["agent_received"],
    "could_not_verify": _STAGE_INDEX["agent_received"],
    # credential_unsealable is AGENT-origin (the agent failed to OPEN the sealed envelope AFTER receiving the
    # signed request) — the backend never emits it pre-transport. So the agent was reached (review WS-P2).
    "credential_unsealable": _STAGE_INDEX["agent_received"],
    # post-probe conditions: the probe ran (login attempted) but a POST-login step failed (diagnostic capture
    # / credential scrub). No pipeline stage is the culprit — mark all validation stages ok; the reason carries
    # it (review WS-P2: never render 'Contacted your broker' as the failing stage for a post-login fault).
    "diagnostic_capture_failed": _STAGE_INDEX["broker_response"],
    "credential_scrub_unverified": _STAGE_INDEX["broker_response"],
    # backend-side config / credential failures → never signed / never reached the agent
    "validation_unconfigured": _STAGE_INDEX["envelope_sealed"],
    "credential_missing": _STAGE_INDEX["api_received"],
    "broker_server_missing": _STAGE_INDEX["api_received"],
}
_SUCCESS_REASONS = {"demo_ok", "is_demo", "verified", "live_detected"}


@dataclass
class TimelineStage:
    key: str
    operator_label: str
    customer_label: str
    state: str            # "ok" | "failed" | "not_reached"
    reason: str = ""      # populated only on the failing stage (operator-facing reason code)


@dataclass
class ValidationTimeline:
    correlation_id: str
    found: bool
    status: str = ""                 # attempt status (HEALTHY / NEEDS_ATTENTION / UNAVAILABLE)
    reason_code: str = ""
    is_demo: object = None
    server: str = ""
    login_masked: str = ""
    trigger: str = ""
    started_at: str = ""             # ISO — the CREDENTIAL_ACCESSED audit time when available
    finished_at: str = ""            # ISO — the attempt created_at (terminal)
    duration_ms: object = None
    stages: object = None
    customer_summary: str = ""
    operator_summary: str = ""


def _furthest_ok(reason: str) -> int:
    return _REASON_FURTHEST_OK.get(str(reason or ""), _STAGE_INDEX["agent_received"])


# Compact, secret-safe customer-summary lines (the authoritative customer wording is the frontend's
# broker-status map; this backend copy is for the operator timeline's "what the customer saw" field only —
# it never leaks IPC/session/MT5/host detail and never claims a broker outage for a host/agent failure).
_CUSTOMER_SUMMARY = {
    "validation_ipc_unavailable": "We couldn't start the secure broker-validation session; your details "
                                  "weren't rejected — please try again later.",
    "validation_unconfigured": "Broker validation isn't available for this account yet.",
    "invalid_password": "The password was not accepted.",
    "invalid_login": "The login was not accepted.",
    "account_disabled": "The account appears to be disabled at the broker.",
    "server_unavailable": "The broker server is temporarily unavailable.",
    # neutral — a transport timeout can't confirm the broker was reached (must not contradict the stages).
    "login_timeout": "The validation didn't complete in time. Please try again.",
    "credential_missing": "No saved password for this account.",
}


def _customer_summary(status: str, reason: str) -> str:
    if str(reason) in _SUCCESS_REASONS or status == "HEALTHY":
        return "Broker connection verified."
    return _CUSTOMER_SUMMARY.get(str(reason), "We couldn't complete the connection check. Please try again "
                                             "shortly.")


def build_timeline(correlation_id: str) -> ValidationTimeline:
    """Assemble the operator/admin timeline for one correlation id from durable records. Never raises; returns
    ``found=False`` when the correlation id is unknown."""
    cid = str(correlation_id or "").strip()
    if not cid:
        return ValidationTimeline(correlation_id="", found=False, stages=[])
    try:
        from trading.models import BrokerAccountValidationAttempt
        attempt = (BrokerAccountValidationAttempt.objects
                   .filter(correlation_id=cid).order_by("-id").first())
    except Exception:  # noqa: BLE001 — read-side must never raise to the caller
        attempt = None
    if attempt is None:
        return ValidationTimeline(correlation_id=cid, found=False, stages=[])

    status = str(attempt.status or "")
    reason = str(attempt.reason_code or "")
    succeeded = reason in _SUCCESS_REASONS or status == "HEALTHY"
    # The last VALIDATION stage (broker_response); "persisted"/"browser_response" are POST-PROCESSING and
    # ALWAYS complete — the result is persisted and returned to the browser whether the login succeeded or not.
    last_val = _STAGE_INDEX["broker_response"]
    term_start = _STAGE_INDEX["persisted"]
    ok_idx = last_val if succeeded else _furthest_ok(reason)
    # The failing VALIDATION stage is the next one after the furthest reached — but only if that is still a
    # validation stage (a post-broker-response failure has no pipeline-stage marker; the reason carries it).
    fail_idx = None if succeeded else (ok_idx + 1 if ok_idx + 1 <= last_val else None)

    stages = []
    for i, (key, op_label, cust_label) in enumerate(STAGES):
        if i >= term_start:
            state = "ok"                          # persisted + browser_response always complete
        elif succeeded or i <= ok_idx:
            state = "ok"
        elif i == fail_idx:
            state = "failed"
        else:
            state = "not_reached"
        stages.append(TimelineStage(key=key, operator_label=op_label, customer_label=cust_label,
                                    state=state, reason=(reason if state == "failed" else "")))

    # real timestamps we DO have: the CREDENTIAL_ACCESSED audit (decrypt) as start, the attempt as finish.
    started = _credential_access_time(attempt)
    finished = attempt.created_at.isoformat() if getattr(attempt, "created_at", None) else ""
    duration_ms = None
    if started and getattr(attempt, "created_at", None):
        try:
            from django.utils.dateparse import parse_datetime
            s = parse_datetime(started)
            if s is not None:
                duration_ms = int((attempt.created_at - s).total_seconds() * 1000)
        except Exception:  # noqa: BLE001
            duration_ms = None

    failing = next((s for s in stages if s.state == "failed"), None)
    if succeeded:
        op_summary = "Validation succeeded (broker connection verified)."
    elif failing is not None:
        op_summary = (f"Furthest stage reached: {STAGES[ok_idx][1]}. First failing stage: "
                      f"{failing.operator_label} (reason: {reason or 'unknown'}).")
    else:
        op_summary = f"Login pipeline completed but the outcome was not healthy (reason: {reason or 'unknown'})."
    return ValidationTimeline(
        correlation_id=cid, found=True, status=status, reason_code=reason,
        is_demo=attempt.is_demo, server=str(attempt.server or ""), login_masked=str(attempt.login_masked or ""),
        trigger=str(attempt.trigger or ""), started_at=started, finished_at=finished, duration_ms=duration_ms,
        stages=[asdict(s) for s in stages],
        customer_summary=_customer_summary(status, reason), operator_summary=op_summary)


def _credential_access_time(attempt) -> str:
    """The CREDENTIAL_ACCESSED audit time for this account nearest at/before the attempt (best-effort start
    marker). Returns '' when unavailable. Never raises."""
    try:
        from core.models import AuditEvent
        acct_id = getattr(attempt, "account_id", None)
        if not acct_id or not getattr(attempt, "created_at", None):
            return ""
        ev = (AuditEvent.objects
              .filter(event_type="CREDENTIAL_ACCESSED", entity_type="TradingAccount",
                      entity_id=str(acct_id), created_at__lte=attempt.created_at)
              .order_by("-created_at").first())
        return ev.created_at.isoformat() if ev else ""
    except Exception:  # noqa: BLE001
        return ""
