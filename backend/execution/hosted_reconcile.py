"""ADR-0034 Execution Engine (G10) — Hosted Workspace ambiguous-send reconciliation driver (DARK).

The pure classifier (``hosted_idempotency.classify_ambiguous_result``) already decides an ambiguous
``order_send`` outcome from reconciled broker/terminal truth. What was missing (scope items 8/10) is the
DRIVER that: takes the authoritative evidence for one ambiguous hosted job, runs the classifier, PERSISTS
the verdict as append-only provenance, and ALERTS + quarantines on ``STILL_AMBIGUOUS`` — while NEVER
re-sending an order.

Following the exact pattern of the rest of the hosted subsystem (producer / agent), the host-side EVIDENCE
SOURCE (a live broker/terminal query) is INJECTED, not performed here — so this module is pure, testable,
and inert-by-design: the live consumer that supplies real evidence is part of the Sponsor-gated host
consumer topology. This module performs NO order, NO attach, NO login; it records + alerts only, and the
live bridge gate remains the sole order-time authority.

Retry stance (item 9), adopted explicitly: ``may_retry_after_ambiguous`` is ADVISORY only. This driver
NEVER auto-resends — not even for ``CONFIRMED_NOT_EXECUTED``. It adopts the legacy PLACE_ORDER discipline
(``execution_health``: never auto-retry an ambiguous order-open; reconcile + surface for a human-gated
re-submission). A retry can only ever happen through an explicit, human-gated control path, never here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("guvfx.execution.hosted")

SOURCE = "execution.hosted_reconcile"


@dataclass(frozen=True)
class AmbiguousEvidence:
    """Authoritative broker/terminal truth for one ambiguous send — supplied by the (host-side) evidence
    source, never fabricated here. ``reconciliation_authoritative`` asserts the query itself was complete."""
    order_found: bool
    position_found: bool
    deal_found: bool
    reconciliation_authoritative: bool


@dataclass(frozen=True)
class ReconcileResult:
    """The reconciliation verdict — a description, never an action. ``retry_permitted`` is ADVISORY (this
    driver never auto-resends); ``quarantined`` marks a STILL_AMBIGUOUS send that must not be retried."""
    classification: str
    retry_permitted: bool
    quarantined: bool
    recorded: bool
    alerted: bool


def reconcile_hosted_execution(job, evidence: AmbiguousEvidence, *, correlation_id: str = "") -> ReconcileResult:
    """Decide one ambiguous hosted send from ``evidence``, persist an append-only RECONCILED provenance row,
    and alert on ``STILL_AMBIGUOUS``. DARK / fail-safe: a no-op (``recorded=False``) for a non-hosted job or
    a dark subsystem. NEVER re-sends an order."""
    from execution.hosted_execution import _hosted_context
    from execution.hosted_idempotency import (
        STILL_AMBIGUOUS,
        classify_ambiguous_result,
        may_retry_after_ambiguous,
    )

    classification = classify_ambiguous_result(
        reconciliation_authoritative=bool(evidence.reconciliation_authoritative),
        order_found=bool(evidence.order_found),
        position_found=bool(evidence.position_found),
        deal_found=bool(evidence.deal_found),
    )
    retry_permitted = may_retry_after_ambiguous(classification)  # advisory ONLY — never auto-acted here
    quarantined = classification == STILL_AMBIGUOUS

    wuuid, enabled = _hosted_context(job)
    if not enabled or getattr(job, "pk", None) is None:
        return ReconcileResult(classification, retry_permitted, quarantined, recorded=False, alerted=False)

    recorded = _record_reconciliation(job, wuuid, classification, correlation_id)
    alerted = _alert_if_ambiguous(job, wuuid, classification, quarantined, correlation_id)
    return ReconcileResult(classification, retry_permitted, quarantined, recorded=recorded, alerted=alerted)


def _record_reconciliation(job, wuuid, classification, correlation_id) -> bool:
    """Append the RECONCILED provenance row (idempotent per job; append-only). Fail-safe."""
    try:
        from execution.models import HostedWorkspaceExecution
        _, created = HostedWorkspaceExecution.objects.get_or_create(
            job=job, phase=HostedWorkspaceExecution.Phase.RECONCILED,
            defaults=dict(workspace_uuid=wuuid, outcome=str(classification)[:24], seq=3,
                          hosted_idempotency_key=str(getattr(job, "hosted_idempotency_key", "") or ""),
                          correlation_id=str(correlation_id or "")[:128]))
        return created
    except Exception:  # noqa: BLE001 — provenance must never break anything
        logger.warning("reconcile record skipped for job=%s (fail-safe)", getattr(job, "pk", None))
        return False


def _alert_if_ambiguous(job, wuuid, classification, quarantined, correlation_id) -> bool:
    """On STILL_AMBIGUOUS, emit a WARNING operational event (quarantine signal — a human must resolve it).
    Confirmed outcomes emit nothing (no alert noise). Fail-open. Secret-free."""
    if not quarantined:
        return False
    try:
        from operational_events.events import record_event

        from hosted_workspace.telemetry import WorkspaceEvent, build_workspace_event
        # Route through the workspace.* taxonomy (like the started/finished emits) so this safety event gets a
        # consistent category/severity + the taxonomy's secret-free redaction, and is a real member of the
        # WorkspaceEvent enum rather than a hand-rolled string. Preserves event_type + WARNING severity.
        kwargs = build_workspace_event(
            WorkspaceEvent.EXECUTION_AMBIGUOUS, wuuid, account_id=getattr(job, "account_id", None),
            correlation_id=str(correlation_id or ""),
            summary="hosted ambiguous send — quarantined, human resolution required",
            detail={"job_id": job.pk, "classification": classification})
        kwargs.pop("account_id", None)
        metadata = kwargs.pop("detail", None)
        dto = record_event(
            **kwargs, account=getattr(job, "account", None), metadata=metadata, source=SOURCE,
            correlation_id=str(correlation_id or ""),
            dedup_key=f"{wuuid}:workspace.execution_ambiguous:{job.pk}"[:200])
        return dto is not None
    except Exception:  # noqa: BLE001
        return False
