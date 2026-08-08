"""ADR-0034 / M2b — Workspace telemetry taxonomy (DARK foundation).

The ``workspace.*`` operational-event family (ADR-0034 §6) on the ADR-0032 operational-event model. This
increment ships the event taxonomy + a pure, secret-free event-*builder* only — it maps each event to a
category/severity and to the canonical Workspace state (M2a) it corresponds to, and produces the
``OperationalEvent`` field kwargs. It does NOT create/save rows and wires NO emit sites (that is a later
increment, when subsystems transition state). "Emit only what each increment transitions" — nothing emits
here. Fail-closed: an unrecognised event yields a SYSTEM/ERROR ``workspace.unknown_event``; credentials are
redacted from every event detail.
"""
from django.db import models

from operational_events.models import OperationalEvent as OE

from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason


class WorkspaceEvent(models.TextChoices):
    """The ``workspace.*`` event-type taxonomy (ADR-0034 §6). Values are the free-form ``event_type`` strings
    stored on OperationalEvent (a new type never needs a migration)."""
    CREATED = "workspace.created", "Workspace created"
    STARTED = "workspace.started", "Workspace started"
    WAITING_FOR_LOGIN = "workspace.waiting_for_login", "Workspace waiting for login"
    CONNECTED = "workspace.connected", "Workspace connected"
    DISCONNECTED = "workspace.disconnected", "Workspace disconnected"
    ATTACH_SUCCEEDED = "workspace.attach_succeeded", "Attach succeeded"
    ATTACH_FAILED = "workspace.attach_failed", "Attach failed"
    ACCOUNT_CHANGED = "workspace.account_changed", "Active broker account changed"
    EXECUTION_READY = "workspace.execution_ready", "Execution ready"
    EXECUTION_PAUSED = "workspace.execution_paused", "Execution paused"
    EXECUTION_STARTED = "workspace.execution_started", "Execution started"
    EXECUTION_FINISHED = "workspace.execution_finished", "Execution finished"
    EXECUTION_AMBIGUOUS = "workspace.execution_ambiguous", "Execution ambiguous (quarantined)"
    RECOVERING = "workspace.recovering", "Recovering"
    RECOVERED = "workspace.recovered", "Recovered"
    REMOTEAPP_CONNECTED = "workspace.remoteapp_connected", "RemoteApp connected"
    REMOTEAPP_DISCONNECTED = "workspace.remoteapp_disconnected", "RemoteApp disconnected"
    CRASHED = "workspace.crashed", "Workspace crashed"
    RESTARTED = "workspace.restarted", "Workspace restarted"


# event -> (category, severity, canonical state it corresponds to | None). The state links each event to the
# M2a state machine (ADR-0034 §3/§6); None = a supervision/edge fact that is not itself a lifecycle state.
EVENT_META = {
    WorkspaceEvent.CREATED: (OE.Category.RUNTIME, OE.Severity.INFO, S.PROVISIONING),
    WorkspaceEvent.STARTED: (OE.Category.RUNTIME, OE.Severity.INFO, S.PROVISIONING),
    WorkspaceEvent.WAITING_FOR_LOGIN: (OE.Category.CONNECTIVITY, OE.Severity.INFO, S.WAITING_FOR_LOGIN),
    WorkspaceEvent.CONNECTED: (OE.Category.CONNECTIVITY, OE.Severity.INFO, S.CONNECTED),
    WorkspaceEvent.DISCONNECTED: (OE.Category.CONNECTIVITY, OE.Severity.WARNING, S.DISCONNECTED),
    WorkspaceEvent.ATTACH_SUCCEEDED: (OE.Category.CONNECTIVITY, OE.Severity.INFO, None),
    WorkspaceEvent.ATTACH_FAILED: (OE.Category.CONNECTIVITY, OE.Severity.ERROR, None),
    WorkspaceEvent.ACCOUNT_CHANGED: (OE.Category.RUNTIME, OE.Severity.WARNING, None),
    WorkspaceEvent.EXECUTION_READY: (OE.Category.EXECUTION, OE.Severity.INFO, S.EXECUTION_READY),
    WorkspaceEvent.EXECUTION_PAUSED: (OE.Category.EXECUTION, OE.Severity.WARNING, S.SUSPENDED),
    WorkspaceEvent.EXECUTION_STARTED: (OE.Category.EXECUTION, OE.Severity.INFO, S.EXECUTING),
    WorkspaceEvent.EXECUTION_FINISHED: (OE.Category.EXECUTION, OE.Severity.INFO, S.EXECUTION_READY),
    # Ambiguous order_send outcome quarantined pending human resolution (never resend). A supervision edge
    # fact, not itself a lifecycle state → canonical None.
    WorkspaceEvent.EXECUTION_AMBIGUOUS: (OE.Category.EXECUTION, OE.Severity.WARNING, None),
    WorkspaceEvent.RECOVERING: (OE.Category.RUNTIME, OE.Severity.WARNING, S.RECOVERING),
    WorkspaceEvent.RECOVERED: (OE.Category.RUNTIME, OE.Severity.INFO, S.CONNECTED),
    WorkspaceEvent.REMOTEAPP_CONNECTED: (OE.Category.CONNECTIVITY, OE.Severity.INFO, None),
    WorkspaceEvent.REMOTEAPP_DISCONNECTED: (OE.Category.CONNECTIVITY, OE.Severity.INFO, None),
    WorkspaceEvent.CRASHED: (OE.Category.RUNTIME, OE.Severity.CRITICAL, None),
    WorkspaceEvent.RESTARTED: (OE.Category.RUNTIME, OE.Severity.WARNING, None),
}

# Keys dropped entirely from any event detail (never telemetered), plus `login` which is masked.
_SECRET_KEYS = {"password", "pwd", "token", "secret", "api_key", "accounts_dat", "keyring"}


def _redact(detail):
    """Return a copy of `detail` with credentials removed and any login masked — telemetry never carries a
    secret. Fail-closed on shape: a non-dict detail becomes empty."""
    if not isinstance(detail, dict):
        return {}
    out = {}
    for key, value in detail.items():
        low = str(key).lower()
        if low in _SECRET_KEYS:
            continue
        if low == "login" and value is not None:
            out[key] = "****" + str(value)[-4:]
        else:
            out[key] = value
    return out


def build_workspace_event(event, workspace_uuid, *, account_id=None, runtime_uuid="",
                          to_state=None, reason=WorkspaceReason.NONE, correlation_id="",
                          summary="", detail=None):
    """Pure, secret-free builder for a ``workspace.*`` operational event — returns the ``OperationalEvent``
    field kwargs (it does NOT create/save the row; no emit this increment). FAIL-CLOSED: an unrecognised
    event yields a SYSTEM/ERROR ``workspace.unknown_event`` rather than a mis-categorised event."""
    meta = EVENT_META.get(event)
    if meta is None:
        category, severity, canonical = OE.Category.SYSTEM, OE.Severity.ERROR, None
        event_type = "workspace.unknown_event"
    else:
        category, severity, canonical = meta
        event_type = str(event)
    payload = {
        "workspace_uuid": str(workspace_uuid),
        "canonical_state": (str(canonical) if canonical is not None else None),
        "to_state": (str(to_state) if to_state is not None else None),
        "correlation_id": correlation_id,
    }
    payload.update(_redact(detail))
    return {
        "category": str(category),
        "event_type": event_type,
        "severity": str(severity),
        "status": (str(canonical) if canonical is not None else ""),
        "reason_code": (str(reason) if reason else ""),
        "summary": summary,
        "account_id": account_id,
        "runtime_uuid": runtime_uuid,
        "customer_visible": False,  # workspace lifecycle telemetry is operator-facing by default
        "detail": payload,
    }
