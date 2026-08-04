"""WP5.2 — the central broker-connectivity → operational-event PROJECTION module (ADR-0032 §WP5.2).

This is the ONE place that maps an already-existing, authoritative broker-connectivity moment onto the
query-optimised OperationalEvent read model. Call sites pass authoritative FACTS only; this module owns
category / event_type / severity / customer-visibility / summary / metadata allow-list / dedup-key /
source. It is a PROJECTION: it never drives a business decision and must never affect the authoritative
operation.

LOAD-BEARING SAFETY RULES (see ADR-0032 §WP5.2 and the WP5.2 tx-safety analysis):
  1. Every write is registered via ``transaction.on_commit`` at the DURABLE emission point — NEVER an
     inline ``record_event`` inside an authoritative ``atomic()`` block. A raised INSERT inside a Postgres
     transaction aborts the whole transaction even when caught, so an inline recorder call could roll back
     the authoritative operation. ``on_commit`` defers the write past COMMIT (discarded on rollback → no
     phantom event) and runs immediately when there is no active transaction (durable autocommit re-emit).
  2. DARK early-return: each ``project_*`` returns immediately when ``operations_events_enabled()`` is
     False — zero extra work, no extra query, no on_commit registration when the subsystem is OFF.
  3. Fail-open: the on_commit registration itself is wrapped; a projection failure can never surface to
     the caller. ``record_event`` is independently fail-open and DARK-gated.
  4. Plain facts only: every value bound into the on_commit closure is a pre-computed scalar
     (int/str/bool). The callback never re-reads a possibly-mutated ORM instance; the ``account`` instance
     is captured solely for the FK (its pk is immutable).
  5. Audit stays authoritative: this module NEVER writes ``core.audit``; it only mirrors that moment.
"""
from __future__ import annotations

import functools
import logging

from django.db import transaction

from .constants import (
    CATEGORY_CONNECTIVITY, CATEGORY_CREDENTIAL, CATEGORY_EXECUTION, CATEGORY_HEALTH,
    CATEGORY_RUNTIME, CATEGORY_VALIDATION, SEV_ERROR, SEV_INFO, SEV_WARNING,
    SOURCE_BROKER_VALIDATION, SOURCE_CREDENTIAL_LIFECYCLE, SOURCE_DISCONNECT,
    SOURCE_EXECUTION_GATE, SOURCE_HEALTH_ENGINE, SOURCE_RUNTIME_PAUSE, SOURCE_RUNTIME_RESUME,
    operations_events_enabled,
)
from .events import record_event

logger = logging.getLogger("guvfx.operational_events")


def _failopen(fn):
    """Make a projection helper STRUCTURALLY fail-open: NOTHING it does — including synchronous fact
    extraction before the on_commit registration — can ever raise into the authoritative caller. This is
    the packet's core invariant (a projection must never affect the authoritative operation), enforced at
    the module boundary rather than relying on each call site to wrap the call."""
    @functools.wraps(fn)
    def _wrap(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 — a projection can NEVER surface to the authoritative caller
            logger.warning("operational_events.broker_projection.%s failed",
                           getattr(fn, "__name__", "?"), exc_info=True)
            return None
    return _wrap


def _safe_on_commit(fn) -> None:
    """Register ``fn`` to run after the current transaction commits (or immediately in autocommit),
    fail-open. Registration is not a DB write, so it can never abort the authoritative transaction."""
    try:
        transaction.on_commit(fn)
    except Exception:  # pragma: no cover - registration is near-infallible; never surface to the caller
        logger.warning("operational_events.broker_projection on_commit registration failed", exc_info=True)


def _emit(**kwargs) -> None:
    """DARK-gated, fail-open, on-commit projection write."""
    if not operations_events_enabled():
        return
    _safe_on_commit(lambda: record_event(**kwargs))


# ── VALIDATION ───────────────────────────────────────────────────────────────────────────────────────
# The attempt.status vocabulary (validator side) → operational severity. HEALTHY = success (INFO);
# NEEDS_ATTENTION = customer failure (WARNING); UNAVAILABLE = technical failure (ERROR).
_VALIDATION_SEVERITY = {"HEALTHY": SEV_INFO, "NEEDS_ATTENTION": SEV_WARNING, "UNAVAILABLE": SEV_ERROR}
_VALIDATION_SUMMARY = {
    "HEALTHY": "Broker connection validated.",
    "NEEDS_ATTENTION": "Broker connection needs attention.",
    "UNAVAILABLE": "Broker validation could not be completed.",
}


@_failopen
def project_validation(account, *, attempt_id, status, reason_code="", retryable=False,
                       is_demo=None, trigger="", correlation_id="") -> None:
    """Project one broker-validation outcome (the BrokerAccountValidationAttempt row is the durable
    source). Customer-visible (VALIDATION). dedup on the attempt id — one attempt == one outcome."""
    if not operations_events_enabled():
        return
    st = str(status or "")
    _emit(
        category=CATEGORY_VALIDATION, source=SOURCE_BROKER_VALIDATION,
        event_type="broker_validation_result",
        severity=_VALIDATION_SEVERITY.get(st, SEV_INFO),
        account=account, status=st, reason_code=str(reason_code or ""),
        summary=_VALIDATION_SUMMARY.get(st, "Broker validation recorded."),
        correlation_id=str(correlation_id or ""),
        customer_visible=True,
        metadata={"status": st, "reason_code": str(reason_code or ""),
                  "retryable": bool(retryable), "is_demo": is_demo, "trigger": str(trigger or "")},
        dedup_key=f"broker_validation:attempt:{int(attempt_id)}",
    )


# ── HEALTH ───────────────────────────────────────────────────────────────────────────────────────────
# One HEALTH event per NET transition. DISCONNECTED → ERROR (per the WP5.2 severity policy's explicit
# list; deliberately NOT inflated to CRITICAL even though the underlying audit severity is CRITICAL — the
# operational classification is distinct from the internal audit severity; documented in ADR-0032).
_HEALTH_SEVERITY = {
    "HEALTHY": SEV_INFO, "DEGRADED": SEV_WARNING, "STALE": SEV_WARNING,
    "DISCONNECTED": SEV_ERROR, "TOMBSTONED": SEV_INFO, "UNKNOWN": SEV_INFO,
}
_HEALTH_SUMMARY = {
    "HEALTHY": "Broker connection healthy.",
    "DEGRADED": "Broker connection degraded.",
    "STALE": "Broker connection has not been confirmed recently.",
    "DISCONNECTED": "Broker connection lost.",
    "TOMBSTONED": "Broker account disconnected.",
    "UNKNOWN": "Broker connection status reset.",
}


def _health_event_type(from_state, to_state) -> str:
    if to_state == "HEALTHY":
        return "broker_health_validated" if from_state == "UNKNOWN" else "broker_health_recovered"
    return f"broker_health_{str(to_state or '').lower()}"


@_failopen
def project_health_transition(account, *, from_state, to_state, reason_code="", state_version,
                              pause_required=False, resume_eligible=False) -> None:
    """Project one net broker-health state transition. Customer-visible (HEALTH). dedup on the monotonic
    per-account state_version (increments by exactly one per net transition → exactly-once)."""
    if not operations_events_enabled():
        return
    to = str(to_state or "")
    sv = int(state_version or 0)
    _emit(
        category=CATEGORY_HEALTH, source=SOURCE_HEALTH_ENGINE,
        event_type=_health_event_type(str(from_state or ""), to),
        severity=_HEALTH_SEVERITY.get(to, SEV_INFO),
        account=account, status=to, reason_code=str(reason_code or ""),
        summary=_HEALTH_SUMMARY.get(to, "Broker health changed."),
        state_version=sv, customer_visible=True,
        metadata={"from_state": str(from_state or ""), "to_state": to, "state_version": sv,
                  "pause_required": bool(pause_required), "resume_eligible": bool(resume_eligible),
                  "reason_code": str(reason_code or "")},
        dedup_key=f"broker_health:{account.pk}:{sv}",
    )


@_failopen
def project_credential_invalidation(account, *, state_version, reason_code="credential_replaced") -> None:
    """Project the health-engine reset to UNKNOWN caused by a credential replacement. OPERATOR-ONLY (the
    customer-facing signal is the CREDENTIAL 'replaced' event); dedup on the new health state_version."""
    if not operations_events_enabled():
        return
    sv = int(state_version or 0)
    _emit(
        category=CATEGORY_CREDENTIAL, source=SOURCE_HEALTH_ENGINE,
        event_type="broker_health_credential_invalidated",
        severity=SEV_WARNING, account=account, status="UNKNOWN", reason_code=str(reason_code or ""),
        summary="Broker health reset after credential change.",
        state_version=sv, customer_visible=False,
        metadata={"state_version": sv, "reason_code": str(reason_code or "")},
        dedup_key=f"broker_health_invalidated:{account.pk}:{sv}",
    )


# ── CREDENTIAL replacement + DISCONNECT ──────────────────────────────────────────────────────────────
@_failopen
def project_credential_rotation(account, *, resulting_status="NEVER", updated_at_iso="",
                                validation_invalidated=True) -> None:
    """Project a completed customer credential replacement. Customer-visible (CREDENTIAL). No natural
    event id → dedup on account + the freshly-bumped updated_at (distinct per genuine rotation)."""
    if not operations_events_enabled():
        return
    key = f"broker_credential_replaced:{account.pk}:{updated_at_iso}" if updated_at_iso else ""
    _emit(
        category=CATEGORY_CREDENTIAL, source=SOURCE_CREDENTIAL_LIFECYCLE,
        event_type="broker_credential_replaced",
        severity=SEV_INFO, account=account, status=str(resulting_status or ""),
        reason_code="credential_replace",
        summary="Broker credentials replaced.",
        customer_visible=True,
        metadata={"resulting_status": str(resulting_status or ""),
                  "validation_invalidated": bool(validation_invalidated)},
        dedup_key=key,
    )


@_failopen
def project_disconnect(account, *, disconnected_at_iso="", credential_destroyed=False) -> None:
    """Project a completed broker disconnect/tombstone. Customer-visible (CONNECTIVITY). dedup on account
    (a tombstone is once-per-account, first-wins). The credential-destroyed fact is folded into metadata,
    NOT emitted as a second event."""
    if not operations_events_enabled():
        return
    _emit(
        category=CATEGORY_CONNECTIVITY, source=SOURCE_DISCONNECT,
        event_type="broker_disconnected",
        severity=SEV_INFO, account=account, status="DISCONNECTED", reason_code="customer_disconnect",
        summary="Broker account disconnected.",
        customer_visible=True,
        metadata={"credential_destroyed": bool(credential_destroyed), "row_deleted": False,
                  "disconnected_at": str(disconnected_at_iso or "")},
        dedup_key=f"broker_disconnect:{account.pk}",
    )


# ── RUNTIME pause / resume (hooked at runtime_pause._audit / _resume_audit) ──────────────────────────
# event string → (event_type, severity, category, customer_visible). Pause/resume ARE the RUNTIME
# category. EXECUTION_GATE_REFUSED that flows through the pause helper is NOT projected here — it is an
# execution refusal, projected via the execution-gate/scheduler durable points (avoids double-emit).
_PAUSE_SPEC = {
    "BROKER_RUNTIME_PAUSED": ("broker_runtime_paused", SEV_WARNING, CATEGORY_RUNTIME, True, "paused"),
    "BROKER_HEALTH_PAUSE_REQUESTED": ("broker_runtime_pause_requested", SEV_WARNING, CATEGORY_RUNTIME, True, "paused"),
    "BROKER_RECOVERY_DETECTED": ("broker_recovery_detected", SEV_INFO, CATEGORY_RUNTIME, False, "recovery"),
    "BROKER_HEALTH_STALE_PAUSE_VERSION_IGNORED": ("broker_stale_pause_ignored", SEV_INFO, CATEGORY_RUNTIME, False, "stale"),
}


@_failopen
def project_pause_audit(event, account, *, version=None, rec=None) -> None:
    """Project one runtime-pause event (called from execution.runtime_pause._audit). Returns without
    projecting for events not owned here (e.g. EXECUTION_GATE_REFUSED)."""
    if not operations_events_enabled():
        return
    spec = _PAUSE_SPEC.get(str(event or ""))
    if spec is None:
        return
    event_type, severity, category, visible, kind = spec
    sv = int(version or 0)
    rec_id = int(getattr(rec, "id", 0) or 0)
    reason_code = str(getattr(rec, "reason_code", "") or "")
    _emit(
        category=category, source=SOURCE_RUNTIME_PAUSE, event_type=event_type, severity=severity,
        account=account, status=event_type, reason_code=reason_code,
        summary="Trading paused for this account." if visible else "Runtime pause reconciled.",
        state_version=sv, customer_visible=visible,
        metadata={"state_version": sv, "reason_code": reason_code, "pause_record_id": rec_id or None},
        dedup_key=f"runtime_pause:{rec_id}:{sv}:{kind}",
    )


# NOTE: the resume event_type literals below deliberately use "broker_resume_*", NOT the controlled-
# resume SERVICE's distinctive function-name core (broker + _runtime_ + resume). A source-coupling test
# (execution.tests_runtime_resume.NoAutomaticResumeTests) proves no non-definition file references that
# name; this module is a read-only PROJECTION invoked from within the resume audit helper — it never
# calls the resume service, so it must not carry the service's name.
_RESUME_SPEC = {
    "BROKER_RUNTIME_RESUMED": ("broker_resume_completed", SEV_INFO, True, "resumed"),
    "BROKER_RUNTIME_RESUME_IDEMPOTENT": ("broker_resume_idempotent", SEV_INFO, False, "idempotent"),
    "BROKER_RUNTIME_RESUME_REFUSED": ("broker_resume_refused", SEV_WARNING, False, "refused"),
    "BROKER_HEALTH_STALE_RESUME_VERSION_IGNORED": ("broker_stale_resume_ignored", SEV_INFO, False, "stale"),
}


@_failopen
def project_resume_audit(event, account, *, requested=None, current=None, reason="", rec=None) -> None:
    """Project one controlled-resume event (called from execution.runtime_pause._resume_audit)."""
    if not operations_events_enabled():
        return
    spec = _RESUME_SPEC.get(str(event or ""))
    if spec is None:
        return
    event_type, severity, visible, kind = spec
    cur = int(current or 0)
    # Refusals often carry no durable version (three of the refusal paths pass no `current` → cur=0), so a
    # "{pk}:0:refused" key would false-merge distinct-reason refusals (first-wins drops the rest). Per the
    # ADR dedup policy, use an EMPTY key for refusals → each distinct operator refusal is its own row.
    dedup = "" if kind == "refused" else f"runtime_resume:{account.pk}:{cur}:{kind}"
    _emit(
        category=CATEGORY_RUNTIME, source=SOURCE_RUNTIME_RESUME, event_type=event_type, severity=severity,
        account=account, status=event_type, reason_code=str(reason or ""),
        summary="Trading resumed for this account." if visible else "Runtime resume reconciled.",
        state_version=cur, customer_visible=visible,
        metadata={"requested_state_version": int(requested or 0), "current_state_version": cur,
                  "reason_code": str(reason or "")},
        dedup_key=dedup,
    )


# ── EXECUTION gate refusals ──────────────────────────────────────────────────────────────────────────
@_failopen
def project_execution_refusal(account, *, reason_code="", phase="creation", job_id=None, trigger="",
                              correlation_id="", bar_close_iso="", state_version=None) -> None:
    """Project one execution-gate refusal (creation or dispatch). OPERATOR-ONLY (EXECUTION) — the
    customer-facing cause is already surfaced by the VALIDATION/HEALTH/CONNECTIVITY categories.

    dedup: dispatch → job_id; scheduler creation → account + bar close (dedups a re-evaluated bar);
    otherwise no natural id → empty key (each API refusal is a distinct row)."""
    if not operations_events_enabled():
        return
    ph = str(phase or "creation")
    event_type = "broker_execution_dispatch_refused" if ph == "dispatch" else "broker_execution_gate_refused"
    if ph == "dispatch" and job_id is not None:
        key = f"exec:dispatch:{int(job_id)}"
    elif bar_close_iso:
        key = f"exec:gate:{account.pk}:{bar_close_iso}:{str(reason_code or '')}"
    elif job_id is not None:
        key = f"exec:{ph}:{int(job_id)}"
    else:
        key = ""
    _emit(
        category=CATEGORY_EXECUTION, source=SOURCE_EXECUTION_GATE, event_type=event_type,
        severity=SEV_WARNING, account=account, status=ph, reason_code=str(reason_code or ""),
        summary="Trade blocked by the broker-connectivity gate.",
        correlation_id=str(correlation_id or ""),
        state_version=(int(state_version) if state_version is not None else None),
        customer_visible=False,
        metadata={"reason_code": str(reason_code or ""), "phase": ph,
                  "job_id": (int(job_id) if job_id is not None else None), "trigger": str(trigger or "")},
        dedup_key=key,
    )


@_failopen
def project_promotion_rejection(account, *, plan_id, reason_code="") -> None:
    """Project an auto-demo promotion rejected by the broker-connectivity gate (reason 'broker_gate_*').
    OPERATOR-ONLY (EXECUTION). dedup on the plan id."""
    if not operations_events_enabled():
        return
    _emit(
        category=CATEGORY_EXECUTION, source=SOURCE_EXECUTION_GATE,
        event_type="broker_promotion_rejected",
        severity=SEV_WARNING, account=account, status="promotion", reason_code=str(reason_code or ""),
        summary="Auto-trade promotion blocked by the broker-connectivity gate.",
        customer_visible=False,
        metadata={"reason_code": str(reason_code or ""), "plan_id": int(plan_id)},
        # Include the reason so a later broker-gate rejection of the same plan with a DIFFERENT reason is a
        # distinct row (a same-reason retry still dedups).
        dedup_key=f"exec:promotion:{int(plan_id)}:{str(reason_code or '')}",
    )
