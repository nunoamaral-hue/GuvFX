"""ADR-0034 / M3c — the SINGLE authoritative Workspace state writer (DARK, read-model only).

``persist_workspace_decision`` is the ONE and only code path that mutates a workspace's canonical M3c
state (``HostedMt5Workspace.canonical_state`` / ``canonical_reason`` / projection cache / versions), appends
its provenance (``WorkspaceTransition``), and emits its lifecycle telemetry. Nothing else writes those
fields. This is the persistence seam the whole subsystem funnels through, and the ONLY place a
``workspace.*`` operational event is emitted (M4 telemetry lives here, at the write, not scattered).

Hard guarantees (all fail-closed):

- **Row-level serialisation.** The workspace row is locked ``select_for_update`` for the whole decision, so
  two concurrent observations can never interleave a read-modify-write.
- **Stale-observation protection.** A caller supplies a strictly-increasing per-workspace
  ``observation_version``; an observation whose version is ``<=`` the stored one is REJECTED_STALE with no
  mutation. A non-positive / non-integer version is REJECTED_INVALID.
- **Stale-decision protection.** The decision was derived by the manager against a possibly-outdated view
  of the workspace. The writer RE-VALIDATES the transition against the AUTHORITATIVE locked
  ``canonical_state`` (``evaluate_workspace_transition``); an illegal non-idempotent move is REJECTED_ILLEGAL
  and holds the stored state. It never trusts the decision's own ``previous_state`` premise.
- **Idempotency.** A material decision writes exactly one ``WorkspaceTransition`` keyed by a ``dedupe_key``
  ``{uuid}:{obs_version}:{to_state}:{reason}``; the same key (a replay) is inserted at most once and reuses
  the SAME key as the operational event's ``dedup_key`` — so a replay double-appends neither a transition
  nor an event.
- **Atomic state + event.** The state update, the transition row, and the telemetry emission all commit
  inside one ``transaction.atomic``. ``record_event`` is itself fail-open (ADR-0032), so a telemetry hiccup
  can never roll back a committed state change — but a rolled-back state change emits no event.

SECURITY: persists / emits NO credential. It writes canonical enum values, booleans, versions, a masked
correlation id, and identifiers only. It performs NO attach, launch, login, or order — it consumes an
already-produced observation and an already-derived decision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.utils import timezone

from operational_events.events import record_event

from hosted_workspace.manager import WorkspaceDecision, WorkspaceObservation
from hosted_workspace.models import HostedMt5Workspace, WorkspaceTransition
from hosted_workspace.state_machine import evaluate_workspace_transition
from hosted_workspace.telemetry import WorkspaceEvent, build_workspace_event

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.persistence"


class PersistStatus:
    """Outcome of a single ``persist_workspace_decision`` call (string constants, stable for evidence)."""
    APPLIED = "APPLIED"                  # a material change was persisted (+ transition row, maybe telemetry)
    IDEMPOTENT = "IDEMPOTENT"            # accepted, no material change (versions/projection refreshed only)
    REJECTED_STALE = "REJECTED_STALE"    # observation_version <= stored — older observation, ignored
    REJECTED_ILLEGAL = "REJECTED_ILLEGAL"  # target not reachable from the locked canonical_state
    REJECTED_INVALID = "REJECTED_INVALID"  # unusable input (bad version / unsaved workspace / bad decision)


@dataclass(frozen=True)
class PersistResult:
    """What the writer did — a description, never itself an action."""
    status: str
    canonical_state: str
    canonical_reason: str
    observation_version: int
    decision_version: int
    transition_created: bool
    telemetry_emitted: bool
    detail: str = ""


def _as_bool(value) -> bool:
    """None (unknown projection) counts as False for change-detection — an unknown→False is not a flip."""
    return value is True


# PositiveBigIntegerField ceiling. A larger value would overflow the column at ``save()`` and raise out of
# the writer; we reject it up front so the writer stays fail-closed (a PersistResult, never an exception).
_MAX_VERSION = (1 << 63) - 1


def _coerce_version(value) -> Optional[int]:
    """A usable observation version is an integer in ``[1, _MAX_VERSION]``. bool is rejected (``True``/
    ``False`` are ints in Python but never a valid sequence); an out-of-range or non-int value -> None so the
    caller maps it to REJECTED_INVALID rather than overflowing the column."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= _MAX_VERSION else None


def persist_workspace_decision(
    workspace: HostedMt5Workspace,
    observation: WorkspaceObservation,
    decision: WorkspaceDecision,
    *,
    observation_version: int,
    correlation_id: str = "",
    source: str = SOURCE,
) -> PersistResult:
    """Persist ``decision`` (derived from ``observation``) as the workspace's new canonical state, exactly
    once, fail-closed. Returns a ``PersistResult``; performs no host/broker action.

    ``observation_version`` MUST be a strictly-increasing per-workspace integer supplied by the caller (the
    poll/orchestration sequence). It is the sole staleness key.
    """
    version = _coerce_version(observation_version)
    if version is None or workspace.pk is None:
        return PersistResult(
            status=PersistStatus.REJECTED_INVALID,
            canonical_state=str(workspace.canonical_state),
            canonical_reason=str(workspace.canonical_reason),
            observation_version=int(getattr(workspace, "observation_version", 0) or 0),
            decision_version=int(getattr(workspace, "decision_version", 0) or 0),
            transition_created=False, telemetry_emitted=False,
            detail=("unsaved_workspace" if workspace.pk is None else "invalid_observation_version"))

    corr = str(correlation_id or "")[:128]
    src = str(source or SOURCE)[:64]

    with transaction.atomic():
        # Authoritative, serialised read of the row under lock — every gate below tests the LOCKED truth.
        locked = HostedMt5Workspace.objects.select_for_update().get(pk=workspace.pk)
        stored_state = str(locked.canonical_state)
        stored_reason = str(locked.canonical_reason)
        stored_version = int(locked.observation_version or 0)

        # Gate 1 — stale observation: an older-or-equal sequence never overwrites newer authoritative state.
        if version <= stored_version:
            return PersistResult(
                status=PersistStatus.REJECTED_STALE, canonical_state=stored_state,
                canonical_reason=stored_reason, observation_version=stored_version,
                decision_version=int(locked.decision_version or 0),
                transition_created=False, telemetry_emitted=False,
                detail=f"version {version} <= stored {stored_version}")

        target = str(decision.next_state)
        idempotent_state = (target == stored_state)

        # Gate 2 — stale-decision / illegal transition: the decision was derived against a possibly-old
        # premise; re-validate against the LOCKED state. Illegal non-idempotent move -> hold, reject.
        if not idempotent_state:
            allowed, _why = evaluate_workspace_transition(stored_state, target)
            if not allowed:
                return PersistResult(
                    status=PersistStatus.REJECTED_ILLEGAL, canonical_state=stored_state,
                    canonical_reason=stored_reason, observation_version=stored_version,
                    decision_version=int(locked.decision_version or 0),
                    transition_created=False, telemetry_emitted=False,
                    detail=f"illegal {stored_state}->{target}")

        # Determine what materially changes.
        new_reason = str(decision.reason)
        state_changed = not idempotent_state
        reason_changed = (new_reason != stored_reason)
        prev_exec_ready = _as_bool(locked.proj_execution_ready)
        exec_ready = bool(decision.execution_ready)
        exec_ready_changed = (prev_exec_ready != exec_ready)
        material = state_changed or reason_changed or exec_ready_changed

        now = timezone.now()
        # Always refresh: applied observation version, health projection cache, correlation, decision-time.
        locked.observation_version = version
        locked.proj_process_running = bool(observation.process_running)
        locked.proj_ipc_available = bool(observation.ipc_available)
        locked.proj_connected = bool(observation.connected)
        locked.proj_account_match = bool(observation.account_match)
        locked.proj_trade_allowed = bool(observation.trade_allowed)
        locked.proj_execution_ready = exec_ready
        locked.last_correlation_id = corr
        locked.last_decision_at = now
        # NB: ``last_decision_at`` (stamped here, atomically with ``proj_*`` under the row lock) is the
        # freshness key the execution-readiness gate reads — ``execution.readiness._observation_fresh`` gates
        # on ``last_decision_at`` (ADR-0034 Execution Engine G1). The M3c writer deliberately does NOT touch
        # the LEGACY ``last_observed_at`` (now vestigial for execution — no gate reads it); leaving it untouched
        # is correct. Do NOT re-point readiness back to ``last_observed_at`` (the writer never advances it, so
        # readiness would fail closed forever), and do NOT start stamping ``last_observed_at`` here.

        update_fields = [
            "observation_version", "proj_process_running", "proj_ipc_available", "proj_connected",
            "proj_account_match", "proj_trade_allowed", "proj_execution_ready", "last_correlation_id",
            "last_decision_at", "updated_at",
        ]

        transition_created = False
        telemetry_emitted = False
        result_status = PersistStatus.IDEMPOTENT

        if material:
            locked.canonical_state = target
            locked.canonical_reason = new_reason
            locked.decision_version = int(locked.decision_version or 0) + 1
            update_fields += ["canonical_state", "canonical_reason", "decision_version"]
            if state_changed:
                locked.last_transition_at = now
                update_fields.append("last_transition_at")
            result_status = PersistStatus.APPLIED

            dedupe_key = f"{locked.workspace_uuid}:{version}:{target}:{new_reason}"[:200]
            telemetry_event_type = decision.telemetry_event if state_changed else ""
            _, transition_created = WorkspaceTransition.objects.get_or_create(
                dedupe_key=dedupe_key,
                defaults=dict(
                    workspace=locked, from_state=stored_state, to_state=target, reason=new_reason,
                    observation_version=version, decision_version=locked.decision_version,
                    state_changed=state_changed, execution_ready_changed=exec_ready_changed,
                    telemetry_event=(telemetry_event_type or ""), source=src, correlation_id=corr))

            # Telemetry is emitted ONLY here, ONLY on a real canonical-state change, ONLY inside this
            # transaction, and ONLY when the transition row was freshly created (never on a replay).
            if transition_created and state_changed and decision.telemetry_event:
                telemetry_emitted = _emit_transition_event(
                    locked, decision, target=target, correlation_id=corr, source=src,
                    dedupe_key=dedupe_key)

        locked.save(update_fields=update_fields)

        return PersistResult(
            status=result_status, canonical_state=str(locked.canonical_state),
            canonical_reason=str(locked.canonical_reason),
            observation_version=int(locked.observation_version),
            decision_version=int(locked.decision_version),
            transition_created=transition_created, telemetry_emitted=telemetry_emitted)


def _emit_transition_event(workspace, decision, *, target, correlation_id, source, dedupe_key) -> bool:
    """Emit the ``workspace.*`` operational event for a state change via the ADR-0032 recorder. Fail-closed
    on our own mapping (a bad event type simply emits nothing); ``record_event`` is itself fail-open. Returns
    whether an event row was recorded. Maps the builder's ``account_id``/``detail`` to the recorder's
    ``account``/``metadata`` (the two seams name these differently) and reuses ``dedupe_key`` for idempotency.
    """
    try:
        event = WorkspaceEvent(str(decision.telemetry_event))
    except ValueError:
        logger.warning("hosted_workspace.persistence unknown telemetry_event %r", decision.telemetry_event)
        return False
    kwargs = build_workspace_event(
        event, workspace.workspace_uuid, account_id=workspace.trading_account_id,
        to_state=target, reason=decision.reason, correlation_id=correlation_id,
        summary=f"workspace {target}")
    kwargs.pop("account_id", None)
    metadata = kwargs.pop("detail", None)
    dto = record_event(
        **kwargs, account=workspace.trading_account, metadata=metadata, source=source,
        correlation_id=correlation_id, state_version=int(workspace.decision_version),
        dedup_key=dedupe_key)
    return dto is not None
