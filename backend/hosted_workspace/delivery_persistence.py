"""ADR-0034 Workspace Delivery — the SINGLE authoritative delivery-state writer (DARK, read-model only).

Mirrors the M3c persistence seam (``persistence.persist_workspace_decision``): this is the ONE code path
that mutates a workspace's delivery-state fields (``delivery_state`` / ``delivery_reason`` /
``last_delivery_attempt`` / ``last_delivery_success`` / ``remoteapp_ready`` / ``workspace_node``) and the
ONLY place a ``workspace.remoteapp_*`` operational event is emitted. Nothing else writes those fields.

Hard guarantees (fail-closed):

- **Row-level serialisation.** Every write locks the workspace row ``select_for_update`` for the whole
  update, so concurrent delivery events cannot interleave a read-modify-write.
- **Scoped update_fields.** Writes touch ONLY the delivery fields (never ``workspace_uuid`` /
  ``trading_account``), so the model's immutable-binding guard never trips and the canonical M3c state,
  legacy attach state, and observation projection are all left untouched — delivery is a distinct concern.
- **Fail-open telemetry.** ``record_event`` is itself fail-open (ADR-0032); a telemetry hiccup can never
  roll back a committed delivery-state change, but a rolled-back change emits no event.

SECURITY: persists / emits NO credential. It writes enum values, booleans, timestamps, a stable reason
code and a masked correlation id only. It performs NO attach, launch, login, or order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from operational_events.events import record_event

from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.telemetry import WorkspaceEvent, build_workspace_event

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.delivery_persistence"

DS = HostedMt5Workspace.DeliveryState


@dataclass(frozen=True)
class DeliveryWriteResult:
    """What the writer did — a description, never itself an action. ``applied`` is False when a stale/out-of-
    order RemoteApp event was rejected (state held, no telemetry); it is always True for an attempt record."""
    delivery_state: str
    delivery_reason: str
    remoteapp_ready: bool
    telemetry_emitted: bool
    applied: bool = True


# PositiveBigIntegerField ceiling (parity with the M3c writer's version guard) — reject an out-of-range or
# non-integer sequence up front so the writer stays fail-closed (a result, never a column overflow).
_MAX_SEQ = (1 << 63) - 1


def _coerce_seq(value):
    """A usable event sequence is an integer in ``[1, _MAX_SEQ]``; ``bool`` and out-of-range/non-int -> None
    (the caller maps None to a rejected, non-mutating write)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= _MAX_SEQ else None


def assign_workspace_node(workspace: HostedMt5Workspace, node) -> bool:
    """Assign the execution host this workspace is delivered from (server-side only). Idempotent; returns
    whether a change was written. Fail-closed: a falsy node is refused (never clears an assignment as a
    side effect — clearing is an explicit operator action, not this helper's job)."""
    if node is None or getattr(node, "pk", None) is None:
        return False
    # ADR-0043 Addendum B: the delivery host IS the interactive RemoteApp session host — a non-Customer-Zero
    # tenant session on Customer Zero's box is co-residency too. Enforce the SAME fail-closed guard here as at
    # the execution-node single writer, so the delivery binding can never independently place a beta tenant on
    # Customer Zero's node. No-op while the flag is OFF; raises BEFORE any write. (Today the only caller is the
    # allocator, which already vetted the node; this closes the path for any future direct caller.)
    from hosted_workspace.tenant_isolation import assert_allocation_allowed
    assert_allocation_allowed(getattr(workspace, "trading_account_id", None), node)
    if workspace.workspace_node_id == node.pk:
        return False
    workspace.workspace_node = node
    workspace.save(update_fields=["workspace_node", "updated_at"])
    return True


def record_delivery_attempt(workspace: HostedMt5Workspace, authorization, *,
                            correlation_id: str = "") -> DeliveryWriteResult:
    """Record the outcome of one ``authorize_workspace_delivery`` on the OWNER's workspace. AUTHORIZED on
    success, FAILED otherwise; stamps ``last_delivery_attempt`` and the stable reason code. Emits no
    telemetry (an attempt is not itself a RemoteApp connect/disconnect).

    NON-REGRESSIVE over a live session (adversarial-review MEDIUM fix): the observer is the authoritative owner
    of CONNECTED. A re-authorize / re-mint of an ALREADY-CONNECTED workspace (e.g. the customer clicks
    Open/Reconnect again while their RemoteApp is up) must NOT downgrade ``delivery_state`` off CONNECTED — doing
    so would flap CONNECTED→AUTHORIZED and make the observer re-fire a DUPLICATE ``REMOTEAPP_CONNECTED`` (new
    seq/dedup key) for one continuous session, plus a READY→DELIVERABLE→READY UI flap. When already CONNECTED we
    stamp only the attempt bookkeeping (reason/timestamp/correlation) and leave the connection state intact."""
    reason = str(getattr(authorization, "reason", ""))[:64]
    authorized = bool(getattr(authorization, "authorized", False))
    now = timezone.now()
    with transaction.atomic():
        locked = HostedMt5Workspace.objects.select_for_update().get(pk=workspace.pk)
        # A live CONNECTED session is owned by the observer's single writer; an attempt record never regresses it.
        connected = str(locked.delivery_state) == str(DS.CONNECTED)
        fields = ["delivery_reason", "last_delivery_attempt", "last_delivery_correlation_id", "updated_at"]
        if not connected:
            locked.delivery_state = DS.AUTHORIZED if authorized else DS.FAILED
            fields.insert(0, "delivery_state")
        locked.delivery_reason = reason
        locked.last_delivery_attempt = now
        locked.last_delivery_correlation_id = str(correlation_id or "")[:128]
        locked.save(update_fields=fields)
        return DeliveryWriteResult(
            delivery_state=str(locked.delivery_state), delivery_reason=str(locked.delivery_reason),
            remoteapp_ready=bool(locked.remoteapp_ready), telemetry_emitted=False)


def record_remoteapp_connected(workspace: HostedMt5Workspace, *, event_seq: int,
                               correlation_id: str = "") -> DeliveryWriteResult:
    """The customer's RemoteApp reported CONNECTED to the persistent Windows session. Marks the workspace
    delivery CONNECTED + ``remoteapp_ready`` and stamps ``last_delivery_success``; emits REMOTEAPP_CONNECTED.
    ``event_seq`` is a strictly-increasing per-workspace connect/disconnect sequence supplied by the caller
    (the host supervision producer) — a reordered/replayed event whose seq is ``<=`` the applied one is
    REJECTED (state held, no telemetry). NOT the order gate — ``remoteapp_ready`` says the window is up,
    never that an order may be sent."""
    return _record_remoteapp_transition(
        workspace, target=DS.CONNECTED, remoteapp_ready=True,
        stamp_success=True, event=WorkspaceEvent.REMOTEAPP_CONNECTED,
        event_seq=event_seq, correlation_id=correlation_id)


def record_remoteapp_disconnected(workspace: HostedMt5Workspace, *, event_seq: int,
                                  correlation_id: str = "") -> DeliveryWriteResult:
    """The customer's RemoteApp DISCONNECTED. The persistent Windows session is RETAINED (this is a
    disconnect, never a teardown) — the customer reconnects to the SAME session later. Marks delivery
    DISCONNECTED and clears ``remoteapp_ready``; emits REMOTEAPP_DISCONNECTED. See ``event_seq`` above."""
    return _record_remoteapp_transition(
        workspace, target=DS.DISCONNECTED, remoteapp_ready=False,
        stamp_success=False, event=WorkspaceEvent.REMOTEAPP_DISCONNECTED,
        event_seq=event_seq, correlation_id=correlation_id)


def _record_remoteapp_transition(workspace, *, target, remoteapp_ready, stamp_success, event,
                                 event_seq, correlation_id) -> DeliveryWriteResult:
    corr = str(correlation_id or "")[:128]
    now = timezone.now()
    seq = _coerce_seq(event_seq)
    with transaction.atomic():
        locked = HostedMt5Workspace.objects.select_for_update().get(pk=workspace.pk)
        stored_seq = int(locked.delivery_event_seq or 0)
        # Staleness gate (mirrors persist_workspace_decision Gate 1): an invalid or older-or-equal sequence
        # never overwrites newer state and never emits. This orders reordered/replayed connect/disconnect
        # events by the caller's monotonic seq — "last-actual" wins, not "last-arrived".
        if seq is None or seq <= stored_seq:
            return DeliveryWriteResult(
                delivery_state=str(locked.delivery_state), delivery_reason=str(locked.delivery_reason),
                remoteapp_ready=bool(locked.remoteapp_ready), telemetry_emitted=False, applied=False)
        locked.delivery_state = target
        locked.remoteapp_ready = remoteapp_ready
        locked.delivery_event_seq = seq
        locked.last_delivery_correlation_id = corr
        fields = ["delivery_state", "remoteapp_ready", "delivery_event_seq",
                  "last_delivery_correlation_id", "updated_at"]
        if stamp_success:
            locked.last_delivery_success = now
            fields.append("last_delivery_success")
        locked.save(update_fields=fields)
        emitted = _emit_delivery_event(locked, event, event_seq=seq, correlation_id=corr)
        return DeliveryWriteResult(
            delivery_state=str(locked.delivery_state), delivery_reason=str(locked.delivery_reason),
            remoteapp_ready=bool(locked.remoteapp_ready), telemetry_emitted=emitted, applied=True)


def _emit_delivery_event(workspace, event, *, event_seq, correlation_id) -> bool:
    """Emit a ``workspace.remoteapp_*`` operational event via the ADR-0032 recorder. Fail-open (record_event
    swallows its own errors); maps the builder's ``account_id``/``detail`` to the recorder's
    ``account``/``metadata`` (parity with ``persistence._emit_transition_event``). Dedup is keyed on the
    MONOTONIC ``event_seq`` (``{uuid}:{event}:{seq}``) — a replay of the same applied seq is idempotent, and
    distinct connect/disconnect events (distinct seqs) are never conflated. No timestamp fallback, so no
    accidental double-emit or silent drop (the two failure modes of a correlation-only/time-based key)."""
    dedup_key = f"{workspace.workspace_uuid}:{str(event)}:{event_seq}"[:200]
    kwargs = build_workspace_event(
        event, workspace.workspace_uuid, account_id=workspace.trading_account_id,
        correlation_id=correlation_id, summary=f"remoteapp {workspace.delivery_state}")
    kwargs.pop("account_id", None)
    metadata = kwargs.pop("detail", None)
    dto = record_event(
        **kwargs, account=workspace.trading_account, metadata=metadata, source=SOURCE,
        correlation_id=correlation_id, dedup_key=dedup_key)
    return dto is not None


def reusable_delivery_session(workspace: HostedMt5Workspace):
    """Return the most recent MT5Session linked to this workspace that is genuinely REUSABLE — not
    ``ended``/``failed`` AND not past its ``expires_at`` — else None. Supports reconnect-to-the-same-
    persistent-session: a live delivery session row is reused rather than re-created. A time-expired session
    is NOT reusable even if its state field was never updated to ``ended`` (a session whose lease lapsed must
    not be handed back). Read-only — creating/linking the row is the caller's job. Guarded so the delivery
    writer never hard-depends on the mt5 app being migrated in a given test."""
    manager = getattr(workspace, "delivery_sessions", None)
    if manager is None:
        return None
    now = timezone.now()
    return (manager.exclude(state__in=["ended", "failed"])
            .exclude(expires_at__isnull=False, expires_at__lt=now)  # drop lapsed leases
            .order_by("-created_at").first())
