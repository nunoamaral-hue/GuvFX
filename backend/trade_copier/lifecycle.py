"""trade_copier.lifecycle — the per-master-ticket LIFECYCLE state machine + its DARK, read-only projection.

ADR-0071 (hybrid change detection & propagation). This is the connective tissue the copier was missing: a
single, explicit lifecycle per ``(follower, master, master_position_ticket)`` that governs WHICH master
changes may be propagated to the follower, in WHICH order, and WHEN an entry is admissible — on top of the
existing observation ledger (``MasterEvent``), replication intents (``CopierReplication``) and follower-ticket
reconciliation (``trade_copier.reconcile``). It introduces NO new execution path: it records decisions and
lifecycle state; it never creates an ``ExecutionJob`` and never places/modifies/closes an order. The
execute/arm gates remain the sole, independent authority for broker contact.

The module has two halves:

* A PURE state machine (no Django, no I/O, no wall clock beyond an injected ``now``) — ``decide_master_event``
  / ``decide_link_update`` and the ``apply_all`` reducer. Unit-tested without a database.
* A gated projection (``project_follower_ticket`` / ``run_lifecycle_pass``) that folds the authoritative
  ``MasterEvent`` stream + the reconcile-derived link facts through the pure engine and materialises durable
  ``CopierPositionLifecycle`` rows + an append-only ``CopierLifecycleTransition`` audit. Idempotent: a
  per-ticket ``applied_sequence`` high-water mark means a re-run reprocesses nothing and terminal states absorb
  every later event (no resurrection).

States (packet GFX-COPIER-HYBRID-CHANGE-001):

    OBSERVED_OPEN        a master OPEN was detected and is admissible; shadow intent only, no follower attempt
    ENTRY_PENDING        an armed follower entry was attempted and we are awaiting its confirmed link
    LINKED               the follower entry is confirmed and bound to an immutable follower ticket
    MODIFY_PENDING       a LINKED position has an admissible SL/TP change queued
    CLOSE_PENDING        a LINKED position has a close (partial/full) queued — close SUPERSEDES a queued modify
    ENTRY_FAILED_FINAL   terminal: the entry was inadmissible/too late/failed — NEVER re-entered (no late recovery)
    CLOSED               terminal: a full close was confirmed on the follower
    CONTAINED_FAILURE    terminal: a divergence/ambiguity needing an operator — never auto-retried

Core safety properties the engine guarantees (asserted by the Phase-3 tests):

* An OPEN detected later than the entry-admissibility deadline, or whose armed attempt links after it, is
  ``ENTRY_FAILED_FINAL`` — a delayed entry is never placed and no later snapshot resurrects it.
* Only a ``LINKED`` position receives a modify/close propagation.
* A close supersedes any queued modify; a modify arriving after a close is dropped.
* Stale / out-of-order / duplicate events (sequence ``<=`` the applied high-water mark) are rejected.
* Terminal states absorb all further events.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from trade_copier import diff

logger = logging.getLogger(__name__)


# ─────────────────────────────────────── states & ops ───────────────────────────────────────

class LifecycleState(str, Enum):
    OBSERVED_OPEN = "OBSERVED_OPEN"
    ENTRY_PENDING = "ENTRY_PENDING"
    LINKED = "LINKED"
    MODIFY_PENDING = "MODIFY_PENDING"
    CLOSE_PENDING = "CLOSE_PENDING"
    ENTRY_FAILED_FINAL = "ENTRY_FAILED_FINAL"
    CLOSED = "CLOSED"
    CONTAINED_FAILURE = "CONTAINED_FAILURE"


TERMINAL_STATES = frozenset({
    LifecycleState.ENTRY_FAILED_FINAL, LifecycleState.CLOSED, LifecycleState.CONTAINED_FAILURE})
# A position is "linked" (a follower ticket exists and may receive modify/close) in exactly these states.
LINKED_STATES = frozenset({
    LifecycleState.LINKED, LifecycleState.MODIFY_PENDING, LifecycleState.CLOSE_PENDING})

OP_NONE = "NONE"
OP_MODIFY = "MODIFY"
OP_CLOSE = "CLOSE"

# Master-event categories the engine reasons about (mapped from MasterEvent.event_type by the projection).
CAT_OPEN = "OPEN"
CAT_FULL_CLOSE = "FULL_CLOSE"
CAT_PARTIAL_CLOSE = "PARTIAL_CLOSE"
CAT_MODIFY = "MODIFY"
CAT_SCALE_IN = "SCALE_IN"
CAT_UNKNOWN = "UNKNOWN"

_EVENT_CATEGORY = {
    diff.OPEN: CAT_OPEN,
    diff.FULL_CLOSE: CAT_FULL_CLOSE,
    diff.PARTIAL_CLOSE: CAT_PARTIAL_CLOSE,
    diff.VOLUME_INCREASE: CAT_SCALE_IN,
    diff.SL_SET: CAT_MODIFY, diff.SL_CHANGE: CAT_MODIFY, diff.SL_REMOVE: CAT_MODIFY,
    diff.TP_SET: CAT_MODIFY, diff.TP_CHANGE: CAT_MODIFY, diff.TP_REMOVE: CAT_MODIFY,
}


def category_for(event_type: str) -> str:
    return _EVENT_CATEGORY.get(event_type, CAT_UNKNOWN)


# ─────────────────────────────────────── policy & inputs ───────────────────────────────────────

@dataclass(frozen=True)
class LifecyclePolicy:
    """Bounded admissibility policy (seconds). Defaults are conservative and configurable — never hard-coded
    at a call site. ``entry_admissibility_seconds`` bounds the WHOLE observe→place→link path (a link confirmed
    later than this is treated as a late entry and contained); it must therefore be set to comfortably cover
    detection + placement + one reconcile cycle. Closes are intentionally never aged out — a close must always
    propagate, and is higher priority than any modify."""
    entry_admissibility_seconds: float = 120.0
    max_modification_age_seconds: float = 120.0
    max_event_age_seconds: float = 300.0

    def __post_init__(self):
        for name in ("entry_admissibility_seconds", "max_modification_age_seconds", "max_event_age_seconds"):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be > 0")


@dataclass(frozen=True)
class MasterEventInput:
    """A typed master event fed to the engine (classified from a ``MasterEvent`` by the projection)."""
    sequence: int
    category: str
    observed_at: datetime
    broker_event_time: Optional[datetime] = None
    has_protection: bool = False          # OPEN only: whether SL/TP were present (informational — we still admit)
    modify_weakens: bool = False          # MODIFY only: SL remove / widen (never auto-replicated — dropped)

    def reference_time(self) -> datetime:
        return self.broker_event_time or self.observed_at


@dataclass(frozen=True)
class LinkUpdate:
    """Follower-side facts derived from the reconcile/replication state (never from a request)."""
    attempted: bool = False               # an armed entry job was created for this ticket's OPEN
    linked: bool = False                  # follower ticket established (reconcile MATCHED)
    follower_ticket: Optional[int] = None
    failed: bool = False                  # the entry attempt failed terminally
    divergent: bool = False               # ambiguous / divergent reconcile — contain
    modify_confirmed: bool = False        # a queued modify applied + reconciled
    modify_failed: bool = False           # a queued modify was rejected by the follower (non-fatal signal)
    close_confirmed: bool = False         # a queued FULL close applied + reconciled
    close_failed: bool = False            # a queued close was rejected — residual exposure, escalate (contain)
    partial_reconciled: bool = False      # a queued PARTIAL close reconciled (→ back to LINKED)
    ticket_lost: bool = False             # the follower ticket vanished with no pending close — contain
    observed_at: Optional[datetime] = None


@dataclass(frozen=True)
class TicketContext:
    """The durable lifecycle state carried between steps (projected to/from ``CopierPositionLifecycle``)."""
    state: LifecycleState
    applied_sequence: int = 0
    entry_reference_at: Optional[datetime] = None
    follower_ticket: Optional[int] = None
    pending_op: str = OP_NONE
    attempted: bool = False


@dataclass(frozen=True)
class Decision:
    """The outcome of one step. ``next`` is the resulting context (or None when no lifecycle exists yet).

    ``applied`` is True when the state or a material field changed. ``rejected`` is non-empty ONLY when the
    event was NOT consumed (stale sequence / no lifecycle) — a consumed-but-no-change event (e.g. a deferred
    scale-in, a superseded modify) has ``applied=False`` and an empty ``rejected`` with an explanatory
    ``reason``."""
    next: Optional[TicketContext]
    applied: bool
    reason: str
    rejected: str = ""


def is_terminal(state: LifecycleState) -> bool:
    return state in TERMINAL_STATES


def _past_entry_deadline(ctx: TicketContext, now: datetime, policy: LifecyclePolicy) -> bool:
    if ctx.entry_reference_at is None:
        return False
    return (now - ctx.entry_reference_at).total_seconds() > policy.entry_admissibility_seconds


# ─────────────────────────────────────── the pure engine ───────────────────────────────────────

def decide_master_event(ctx: Optional[TicketContext], ev: MasterEventInput, *,
                        now: datetime, policy: LifecyclePolicy) -> Decision:
    """Apply one master event. Pure. ``ctx`` is None before any lifecycle exists (only an OPEN creates one)."""
    # 1) No lifecycle yet — only an OPEN may create one.
    if ctx is None:
        if ev.category != CAT_OPEN:
            return Decision(None, applied=False, reason="no_open_yet", rejected="no_lifecycle")
        ref = ev.reference_time()
        base = TicketContext(state=LifecycleState.OBSERVED_OPEN, applied_sequence=ev.sequence,
                             entry_reference_at=ref, pending_op=OP_NONE, attempted=False)
        if (now - ref).total_seconds() > policy.entry_admissibility_seconds:
            # Detected too late to ever enter — terminal, never a late entry.
            return Decision(replace(base, state=LifecycleState.ENTRY_FAILED_FINAL),
                            applied=True, reason="entry_deadline_exceeded")
        return Decision(base, applied=True, reason="observed_open")

    # 2) Terminal states absorb everything (consume the sequence, no state change — no resurrection).
    if ctx.state in TERMINAL_STATES:
        nxt = ctx if ev.sequence <= ctx.applied_sequence else replace(ctx, applied_sequence=ev.sequence)
        return Decision(nxt, applied=False, reason="terminal_absorbed")

    # 3) Stale / out-of-order / duplicate — reject WITHOUT advancing the high-water mark.
    if ev.sequence <= ctx.applied_sequence:
        return Decision(ctx, applied=False, reason="", rejected="stale_sequence")

    # Consume the event (advance the monotone high-water mark); decide the transition below.
    base = replace(ctx, applied_sequence=ev.sequence)
    cat = ev.category

    if cat == CAT_OPEN:
        return Decision(base, applied=False, reason="duplicate_open")
    if cat == CAT_SCALE_IN:
        return Decision(base, applied=False, reason="scale_in_deferred")
    if cat == CAT_UNKNOWN:
        return Decision(base, applied=False, reason="unknown_event_ignored")

    if cat == CAT_FULL_CLOSE:
        if ctx.state in (LifecycleState.LINKED, LifecycleState.MODIFY_PENDING):
            return Decision(replace(base, state=LifecycleState.CLOSE_PENDING, pending_op=OP_CLOSE),
                            applied=True, reason="full_close_requested")     # supersedes a queued modify
        if ctx.state == LifecycleState.CLOSE_PENDING:
            return Decision(base, applied=False, reason="close_already_pending")
        if ctx.state == LifecycleState.OBSERVED_OPEN:
            # Master closed before we ever attempted an entry — there is no follower position.
            return Decision(replace(base, state=LifecycleState.ENTRY_FAILED_FINAL),
                            applied=True, reason="closed_before_entry")
        # ENTRY_PENDING: an attempt is in flight but unconfirmed — a follower position MAY exist. Contain it
        # (reconcile/operator must establish whether an entry filled) rather than silently drop or re-enter.
        return Decision(replace(base, state=LifecycleState.CONTAINED_FAILURE),
                        applied=True, reason="closed_before_link_contained")

    if cat == CAT_PARTIAL_CLOSE:
        if ctx.state in (LifecycleState.LINKED, LifecycleState.MODIFY_PENDING):
            return Decision(replace(base, state=LifecycleState.CLOSE_PENDING, pending_op=OP_CLOSE),
                            applied=True, reason="partial_close_requested")  # close-family supersedes modify
        if ctx.state == LifecycleState.CLOSE_PENDING:
            return Decision(base, applied=False, reason="close_already_pending")
        return Decision(base, applied=False, reason="partial_close_before_link")

    if cat == CAT_MODIFY:
        if ctx.state == LifecycleState.CLOSE_PENDING:
            return Decision(base, applied=False, reason="superseded_by_close")   # ordering contract
        if ctx.state in (LifecycleState.LINKED, LifecycleState.MODIFY_PENDING):
            if ev.modify_weakens:
                return Decision(base, applied=False, reason="modify_weakens_stop_deferred")
            age = (now - ev.reference_time()).total_seconds()
            if age > min(policy.max_modification_age_seconds, policy.max_event_age_seconds):
                return Decision(base, applied=False, reason="modify_too_old")
            return Decision(replace(base, state=LifecycleState.MODIFY_PENDING, pending_op=OP_MODIFY),
                            applied=(ctx.state != LifecycleState.MODIFY_PENDING), reason="modify_requested")
        return Decision(base, applied=False, reason="modify_before_link")

    return Decision(base, applied=False, reason="unhandled_category")


def decide_link_update(ctx: Optional[TicketContext], link: LinkUpdate, *,
                       now: datetime, policy: LifecyclePolicy) -> Decision:
    """Apply one follower-side link update. Pure. Never advances the master-event high-water mark."""
    if ctx is None:
        return Decision(None, applied=False, reason="no_open_yet", rejected="no_lifecycle")
    if ctx.state in TERMINAL_STATES:
        return Decision(ctx, applied=False, reason="terminal_absorbed")

    # A divergence/ambiguity is contained from any non-terminal state.
    if link.divergent or link.ticket_lost:
        if link.ticket_lost and ctx.state == LifecycleState.CLOSE_PENDING:
            # The ticket is gone AND we were closing it — that is the expected full-close confirmation.
            return Decision(replace(ctx, state=LifecycleState.CLOSED, pending_op=OP_NONE),
                            applied=True, reason="close_confirmed")
        return Decision(replace(ctx, state=LifecycleState.CONTAINED_FAILURE, pending_op=OP_NONE),
                        applied=True, reason=("link_lost" if link.ticket_lost else "link_divergent"))

    if ctx.state == LifecycleState.OBSERVED_OPEN:
        if link.attempted:
            if _past_entry_deadline(ctx, now, policy):
                return Decision(replace(ctx, state=LifecycleState.ENTRY_FAILED_FINAL),
                                applied=True, reason="attempt_after_deadline")
            return Decision(replace(ctx, state=LifecycleState.ENTRY_PENDING, attempted=True),
                            applied=True, reason="entry_attempted")
        return Decision(ctx, applied=False, reason="observing")

    if ctx.state == LifecycleState.ENTRY_PENDING:
        if link.failed:
            return Decision(replace(ctx, state=LifecycleState.ENTRY_FAILED_FINAL),
                            applied=True, reason="entry_attempt_failed")
        if link.linked:
            if _past_entry_deadline(ctx, now, policy):
                return Decision(replace(ctx, state=LifecycleState.ENTRY_FAILED_FINAL),
                                applied=True, reason="linked_after_deadline")
            return Decision(replace(ctx, state=LifecycleState.LINKED,
                                    follower_ticket=link.follower_ticket),
                            applied=True, reason="linked")
        if _past_entry_deadline(ctx, now, policy):
            return Decision(replace(ctx, state=LifecycleState.ENTRY_FAILED_FINAL),
                            applied=True, reason="link_deadline_exceeded")
        return Decision(ctx, applied=False, reason="awaiting_link")

    if ctx.state in LINKED_STATES:
        if ctx.state == LifecycleState.CLOSE_PENDING and link.close_confirmed:
            return Decision(replace(ctx, state=LifecycleState.CLOSED, pending_op=OP_NONE),
                            applied=True, reason="close_confirmed")
        if ctx.state == LifecycleState.CLOSE_PENDING and link.partial_reconciled:
            return Decision(replace(ctx, state=LifecycleState.LINKED, pending_op=OP_NONE),
                            applied=True, reason="partial_reconciled")
        if ctx.state == LifecycleState.CLOSE_PENDING and link.close_failed:
            # A rejected close leaves the follower exposed while the master is closing — escalate, never retry.
            return Decision(replace(ctx, state=LifecycleState.CONTAINED_FAILURE, pending_op=OP_NONE),
                            applied=True, reason="close_failed")
        if ctx.state == LifecycleState.MODIFY_PENDING and link.modify_confirmed:
            return Decision(replace(ctx, state=LifecycleState.LINKED, pending_op=OP_NONE),
                            applied=True, reason="modify_confirmed")
        if ctx.state == LifecycleState.MODIFY_PENDING and link.modify_failed:
            # A rejected SL/TP modify is non-fatal: the position stays LINKED (levels simply unchanged); the
            # failure is surfaced as the transition reason, not by closing or abandoning the position.
            return Decision(replace(ctx, state=LifecycleState.LINKED, pending_op=OP_NONE),
                            applied=True, reason="modify_failed")
        return Decision(ctx, applied=False, reason="no_link_change")

    return Decision(ctx, applied=False, reason="unhandled_state")


def apply_all(ctx: Optional[TicketContext], triggers, *, policy: LifecyclePolicy):
    """Fold an ordered list of ``(now, kind, obj)`` triggers through the engine. ``kind`` is 'event' or
    'link'. Returns ``(final_ctx, decisions)``. Pure — the convenience used by property/determinism tests."""
    decisions = []
    for now, kind, obj in triggers:
        if kind == "event":
            d = decide_master_event(ctx, obj, now=now, policy=policy)
        elif kind == "link":
            d = decide_link_update(ctx, obj, now=now, policy=policy)
        else:
            raise ValueError(f"unknown trigger kind {kind!r}")
        decisions.append(d)
        if d.next is not None:
            ctx = d.next
    return ctx, decisions


# ─────────────────────────────────────── the gated projection ───────────────────────────────────────
# Everything below reads the DB and materialises lifecycle rows. It is DARK (gated on
# ``copier_lifecycle_enabled``) and strictly non-executing: no ExecutionJob, no order, no broker contact.

def _ctx_from_row(row) -> TicketContext:
    return TicketContext(
        state=LifecycleState(row.state),
        applied_sequence=int(row.applied_sequence or 0),
        entry_reference_at=row.entry_reference_at,
        follower_ticket=row.follower_ticket,
        pending_op=row.pending_op or OP_NONE,
        attempted=bool(row.attempted),
    )


def _modify_weakens(event) -> bool:
    """Classify whether replicating this master SL change would WEAKEN the follower stop (SL remove / widen).

    Mirrors ``execution_producer._sl_weakening_reason`` so the lifecycle's "deferred modify" matches the
    producer's fail-closed-on-weaken rule, but returns a bare bool (the engine only needs to know to defer).
    TP changes and an SL that tightens are never weakening. An unverifiable SL change is treated as weakening
    (fail closed)."""
    et = event.event_type
    if et not in (diff.SL_CHANGE, diff.SL_REMOVE):
        return False
    if et == diff.SL_REMOVE:
        return True
    new_sl = float(event.new_sl) if event.new_sl is not None else None
    if new_sl is None or abs(new_sl) <= diff._PRICE_EPS:
        return True
    prev_sl = float(event.prev_sl) if event.prev_sl is not None else None
    from trade_copier.replication import _master_position_from_event
    snap = _master_position_from_event(event)
    entry = float(snap.price_open) if (snap is not None and snap.price_open is not None) else None
    if entry is None or prev_sl is None or abs(prev_sl) <= diff._PRICE_EPS:
        return True   # cannot prove it is not a weaken → fail closed (defer)
    return abs(entry - new_sl) > abs(entry - prev_sl) + diff._PRICE_EPS


def _classify(event) -> MasterEventInput:
    cat = category_for(event.event_type)
    return MasterEventInput(
        sequence=int(event.sequence),
        category=cat,
        observed_at=event.observed_at,
        # POLL events expose no per-change broker timestamp (diff.py docstring) → stays None → the deadline is
        # measured from observed_at exactly as before. EVENT-sourced events (ADR-0074 projection) DO carry the
        # true broker change time, so a stale/buffered emission's admissibility is judged from when the master
        # actually changed — a late OPEN is ruled ENTRY_FAILED_FINAL, never entered.
        broker_event_time=getattr(event, "broker_event_time", None),
        has_protection=bool(
            (event.new_sl is not None and abs(float(event.new_sl)) > diff._PRICE_EPS)
            or (event.new_tp is not None and abs(float(event.new_tp)) > diff._PRICE_EPS)),
        modify_weakens=(_modify_weakens(event) if cat == CAT_MODIFY else False),
    )


def _derive_link_update(follower, master, ticket, *, now):
    """Build the current ``LinkUpdate`` for a ticket from the follower's OPEN replication + reconcile state.

    READ-ONLY. In the DARK/shadow reality (nothing armed) the OPEN replication is a SHADOW record, so
    ``attempted``/``linked`` are False and the lifecycle rests at OBSERVED_OPEN — a truthful reflection that we
    observed but took no follower action. The armed transitions are exercised by the pure-engine unit tests."""
    from trade_copier.models import CopierReplication, ReplicationStatus
    from trade_copier.reconcile import MATCHED, AMBIGUOUS, DIVERGENT_MISSING
    open_repl = (CopierReplication.objects
                 .filter(follower=follower, requested_op="OPEN",
                         master_event__master_id=master.id, master_event__position_ticket=ticket)
                 .order_by("-id").first())
    if open_repl is None:
        return LinkUpdate(observed_at=now), None
    attempted = open_repl.status in (ReplicationStatus.EXECUTION_REQUESTED,
                                     ReplicationStatus.EXECUTION_VERIFIED)
    linked = (open_repl.reconciliation_status == MATCHED and open_repl.follower_ticket is not None)
    failed = open_repl.status == ReplicationStatus.EXECUTION_FAILED
    divergent = open_repl.reconciliation_status in (AMBIGUOUS, DIVERGENT_MISSING)
    return LinkUpdate(
        attempted=attempted, linked=linked, follower_ticket=open_repl.follower_ticket,
        failed=failed, divergent=divergent, observed_at=now), open_repl


def project_follower_ticket(follower, master, ticket, *, now, policy, open_event=None):
    """Project (idempotently) the lifecycle for one ``(follower, master, ticket)``. Must run inside a
    transaction with the row locked by the caller. Returns a summary dict."""
    from trade_copier.models import (CopierPositionLifecycle, CopierLifecycleTransition, MasterEvent)

    row = (CopierPositionLifecycle.objects
           .filter(follower=follower, master=master, master_position_ticket=ticket).first())
    ctx = _ctx_from_row(row) if row is not None else None
    applied_seq = int(row.applied_sequence) if row is not None else 0

    transitions: list[tuple] = []   # (from_state, to_state, reason, sequence)
    events = (MasterEvent.objects
              .filter(master=master, position_ticket=ticket, sequence__gt=applied_seq)
              .order_by("sequence"))
    for ev in events:
        prev = ctx
        d = decide_master_event(ctx, _classify(ev), now=now, policy=policy)
        if d.rejected == "stale_sequence":
            continue
        if d.next is not None:
            if d.applied:
                transitions.append(((prev.state.value if prev else ""), d.next.state.value, d.reason, ev.sequence))
            elif (d.reason == "terminal_absorbed" and prev is not None
                  and category_for(ev.event_type) in (CAT_OPEN, CAT_MODIFY, CAT_FULL_CLOSE, CAT_PARTIAL_CLOSE)):
                # Phase 6 (ADR-0074): a later SL/TP/close change on a FINALLY-failed/closed/contained entry must
                # never create an entry — record it as NON-EXECUTABLE lifecycle evidence (same state, clear
                # reason), not a state change. Idempotent: each absorbed event's sequence is recorded once.
                cat = category_for(ev.event_type).lower()
                transitions.append((prev.state.value, d.next.state.value,
                                    f"post_terminal_{cat}_ignored"[:48], ev.sequence))
            ctx = d.next

    # Overlay the current follower-side link facts (one update reflecting present reconcile state).
    link, _open_repl = _derive_link_update(follower, master, ticket, now=now)
    if ctx is not None:
        prev = ctx
        d = decide_link_update(ctx, link, now=now, policy=policy)
        if d.next is not None:
            if d.applied:
                transitions.append((prev.state.value, d.next.state.value, d.reason, 0))
            ctx = d.next

    if ctx is None:
        return {"follower_id": follower.pk, "ticket": ticket, "state": None, "created": False}

    deadline = (ctx.entry_reference_at + timedelta(seconds=policy.entry_admissibility_seconds)
                if ctx.entry_reference_at is not None else None)
    terminal = ctx.state in TERMINAL_STATES
    failure_reason = ""
    if transitions and ctx.state in (LifecycleState.ENTRY_FAILED_FINAL, LifecycleState.CONTAINED_FAILURE):
        failure_reason = transitions[-1][2][:64]

    if row is None:
        row = CopierPositionLifecycle(follower=follower, master=master, master_position_ticket=ticket)
    row.state = ctx.state.value
    row.pending_op = ctx.pending_op
    row.applied_sequence = ctx.applied_sequence
    row.entry_reference_at = ctx.entry_reference_at
    row.entry_deadline_at = deadline
    row.follower_ticket = ctx.follower_ticket
    row.attempted = ctx.attempted
    row.is_terminal = terminal
    if failure_reason:
        row.failure_reason = failure_reason
    if open_event is not None and row.open_event_id is None:
        row.open_event = open_event
        row.open_observed_at = open_event.observed_at
    if ctx.state == LifecycleState.LINKED and row.linked_at is None:
        row.linked_at = now
    if ctx.state == LifecycleState.CLOSED and row.closed_at is None:
        row.closed_at = now
    row.last_transition_at = now
    row.save()

    for from_state, to_state, reason, seq in transitions:
        CopierLifecycleTransition.objects.create(
            lifecycle=row, from_state=from_state, to_state=to_state,
            trigger=reason[:48], sequence=int(seq or 0))

    return {"follower_id": follower.pk, "ticket": ticket, "state": ctx.state.value,
            "terminal": terminal, "transitions": len(transitions), "created": row.pk is not None}


def project_master(master, *, now=None, policy: LifecyclePolicy, limit: int = 500) -> dict:
    """Project every (follower, OPEN-ticket) lifecycle for one master. Gated at the pass level; this helper
    assumes the gate has passed. One ticket's failure never sinks the others."""
    from django.db import transaction
    from django.utils import timezone
    from trade_copier.models import CopierPositionLifecycle, MasterEvent

    now = now or timezone.now()
    # The entry points are OPEN events: (position_ticket, the OPEN event) pairs for this master.
    open_events = {}
    for ev in (MasterEvent.objects.filter(master=master, event_type=diff.OPEN)
               .order_by("position_ticket", "sequence")[:limit]):
        open_events.setdefault(ev.position_ticket, ev)   # the first OPEN for each ticket

    projected = 0
    states: dict[str, int] = {}
    for follower in master.followers.all():
        for ticket, open_ev in open_events.items():
            try:
                with transaction.atomic():
                    # Serialise concurrent projections for this (follower, ticket).
                    locked = (CopierPositionLifecycle.objects.select_for_update()
                              .filter(follower=follower, master=master, master_position_ticket=ticket).first())
                    _ = locked   # lock held for the row's lifetime in this txn (None on first creation)
                    out = project_follower_ticket(follower, master, ticket, now=now, policy=policy,
                                                  open_event=open_ev)
                if out.get("state"):
                    projected += 1
                    states[out["state"]] = states.get(out["state"], 0) + 1
            except Exception as e:  # noqa: BLE001 — one ticket's error must not sink the master's pass
                logger.exception("copier lifecycle projection failed follower=%s ticket=%s: %s",
                                 follower.pk, ticket, e)
    return {"master_id": master.pk, "projected": projected, "states": states}


def run_lifecycle_pass(*, now=None, policy: Optional[LifecyclePolicy] = None, limit: int = 500) -> dict:
    """One DARK, read-only lifecycle-projection pass over configured masters. Gated + fail-closed.

    Inert unless ``COPIER_ENABLED`` AND ``COPIER_LIFECYCLE_ENABLED`` are on. Creates no ExecutionJob and
    contacts no broker. Returns a secret-free summary."""
    from trade_copier.flags import copier_lifecycle_enabled
    from trade_copier.models import CopierMaster

    if not copier_lifecycle_enabled():
        return {"enabled": False, "masters": 0, "projected": 0}
    policy = policy or LifecyclePolicy()
    masters = CopierMaster.objects.all().prefetch_related("followers")
    total = 0
    states: dict[str, int] = {}
    n = 0
    for master in masters:
        n += 1
        out = project_master(master, now=now, policy=policy, limit=limit)
        total += out.get("projected", 0)
        for s, c in out.get("states", {}).items():
            states[s] = states.get(s, 0) + c
    return {"enabled": True, "masters": n, "projected": total, "states": states}
