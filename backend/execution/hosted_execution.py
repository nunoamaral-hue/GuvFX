"""ADR-0034 Execution Engine (G12) — Hosted Workspace execution telemetry + persistence seam (DARK).

Closes the two "defined-only / absent" scope items for the Hosted Workspace (Provider B) path:

- **Execution persistence** — a durable, append-only ``HostedWorkspaceExecution`` record of each hosted
  job's EXECUTING occupancy (STARTED at dispatch, FINISHED at completion, with outcome), plus the job's
  deterministic ``hosted_idempotency_key`` persisted onto the ``ExecutionJob`` itself.
- **Execution telemetry** — emits ``workspace.execution_started`` / ``workspace.execution_finished`` at
  dispatch / completion.

Design boundary (deliberate, documented): this is the ORDER-DRIVEN execution family. It does NOT drive the
M3c canonical ``HostedMt5Workspace.canonical_state`` enum to EXECUTING — the canonical state stays
OBSERVATION-owned by the single M3c writer (``persistence.persist_workspace_decision``), so the M3c
single-writer invariant and the certified readiness gate are untouched (the manager already excludes
EXECUTING from observation derivation). The EXECUTING lifecycle is captured HERE as provenance + telemetry.

Every function is:
- **DARK / fail-closed** — a no-op while the master flag is OFF or for a non-hosted job (zero extra query
  via the flag-first check); the legacy dispatch path is byte-for-byte unchanged.
- **Fail-SAFE** — called from the hot dispatch/complete paths, so any error is swallowed and logged; a
  provenance/telemetry hiccup can NEVER break a claim or a completion.
- **Idempotent** — one ``HostedWorkspaceExecution`` row per (job, phase) (DB unique constraint) and a
  (job, phase)-keyed telemetry ``dedup_key``; a replayed dispatch/completion double-emits nothing.
- **Order-authority-free** — records/telemeters only; it performs NO order, and persisted state is never
  the order-time gate (the live bridge remains sole authority). Carries NO credential.
"""
from __future__ import annotations

import logging

from operational_events.events import record_event

logger = logging.getLogger("guvfx.execution.hosted")

SOURCE = "execution.hosted_execution"


def _hosted_context(job):
    """Return ``(workspace_uuid, enabled)`` for a job: the stamped workspace uuid and whether this is a
    hosted job under an ON subsystem. Flag-checked FIRST (cheap) so a dark subsystem adds zero work."""
    from execution.hosted_pin import pin_subsystem_enabled
    if not pin_subsystem_enabled():
        return "", False
    wuuid = str(getattr(job, "hosted_workspace_uuid", "") or "")
    return wuuid, bool(wuuid)


def stamp_hosted_idempotency_key(job) -> str:
    """Compute + persist the deterministic HWX idempotency key onto ``job`` (needs the job pk, so it is
    stamped at dispatch, not creation). Idempotent: never recomputes over an already-set key. Returns the
    key (or ""). Secret-free (login/server are identifiers folded into a SHA-256 digest)."""
    wuuid, enabled = _hosted_context(job)
    if not enabled or getattr(job, "pk", None) is None:
        return ""
    existing = str(getattr(job, "hosted_idempotency_key", "") or "")
    if existing:
        return existing
    from execution.hosted_idempotency import hosted_idempotency_key
    payload = getattr(job, "payload", None) or {}
    key = hosted_idempotency_key(
        workspace_uuid=wuuid,
        expected_login=str(payload.get("expected_login") or ""),
        expected_server=str(payload.get("expected_server") or ""),
        job_id=job.pk,
        operation=str(getattr(job, "job_type", "") or ""),
        strategy_id=str(getattr(job, "strategy_id", "") or ""),
    )
    try:
        job.hosted_idempotency_key = key
        job.save(update_fields=["hosted_idempotency_key"])
    except Exception:  # noqa: BLE001 — provenance must never break dispatch
        logger.warning("stamp_hosted_idempotency_key: persist skipped for job=%s", getattr(job, "pk", None))
    return key


def record_hosted_dispatch(job, *, correlation_id: str = "") -> bool:
    """At hosted-job dispatch (job → RUNNING): stamp the HWX key, append a STARTED provenance row, and emit
    ``workspace.execution_started``. Fail-safe + idempotent + DARK. Returns whether telemetry was emitted."""
    return _record_phase(job, phase="STARTED", seq=1, outcome="",
                          event_value="workspace.execution_started", correlation_id=correlation_id,
                          stamp_key=True)


def record_hosted_completion(job, *, correlation_id: str = "") -> bool:
    """At hosted-job completion (job → SUCCESS/FAILED): append a FINISHED provenance row carrying the
    sanitised outcome and emit ``workspace.execution_finished``. Fail-safe + idempotent + DARK."""
    outcome = str(getattr(job, "status", "") or "")[:24]
    return _record_phase(job, phase="FINISHED", seq=2, outcome=outcome,
                          event_value="workspace.execution_finished", correlation_id=correlation_id,
                          stamp_key=False)


def _record_phase(job, *, phase, seq, outcome, event_value, correlation_id, stamp_key) -> bool:
    wuuid, enabled = _hosted_context(job)
    if not enabled or getattr(job, "pk", None) is None:
        return False
    try:
        from execution.models import HostedWorkspaceExecution

        key = stamp_hosted_idempotency_key(job) if stamp_key else str(
            getattr(job, "hosted_idempotency_key", "") or "")
        corr = str(correlation_id or "")[:128]
        _, created = HostedWorkspaceExecution.objects.get_or_create(
            job=job, phase=phase,
            defaults=dict(workspace_uuid=wuuid, outcome=outcome, hosted_idempotency_key=key,
                          seq=seq, correlation_id=corr))
        if not created:
            return False  # replay — provenance + telemetry already recorded for this (job, phase)
        return _emit_execution_event(job, wuuid, event_value, phase=phase, outcome=outcome,
                                     correlation_id=corr)
    except Exception:  # noqa: BLE001 — a provenance/telemetry error must never break dispatch/completion
        logger.warning("record %s skipped for job=%s (fail-safe)", phase, getattr(job, "pk", None))
        return False


def _emit_execution_event(job, workspace_uuid, event_value, *, phase, outcome, correlation_id) -> bool:
    """Emit the ORDER-DRIVEN ``workspace.execution_*`` operational event via the ADR-0032 recorder. This is a
    DELIBERATELY separate emit site from the M3c observation-driven lifecycle telemetry (persistence.py):
    different origin (order dispatch/complete vs observation), same secret-free discipline. Fail-open."""
    from hosted_workspace.telemetry import WorkspaceEvent, build_workspace_event

    try:
        event = WorkspaceEvent(event_value)
    except ValueError:
        return False
    dedup_key = f"{workspace_uuid}:{event_value}:{job.pk}:{phase}"[:200]
    kwargs = build_workspace_event(
        event, workspace_uuid, account_id=getattr(job, "account_id", None),
        correlation_id=correlation_id, summary=f"hosted execution {phase.lower()}",
        detail={"phase": phase, "outcome": outcome, "job_id": job.pk})
    kwargs.pop("account_id", None)
    metadata = kwargs.pop("detail", None)
    dto = record_event(
        **kwargs, account=getattr(job, "account", None), metadata=metadata, source=SOURCE,
        correlation_id=correlation_id, dedup_key=dedup_key)
    return dto is not None
