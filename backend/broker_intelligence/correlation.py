"""WP5 — Withdrawal correlation engine: project append-only ``BrokerEvent``s into durable ``Withdrawal`` records.

The single, deterministic path from parsed broker events to a member-visible withdrawal. Contract:

* **Only ``EXTERNAL_WITHDRAWAL``** events are correlated. INTERNAL_TRANSFER / DEPOSIT / UNKNOWN never create or
  advance a Withdrawal — so an internal transfer (e.g. the genuine TradersWay "confirm your withdrawal request"
  email, which is an internal transfer) can never inflate withdrawal statistics.
* **Reference-id first (deterministic).** An event carrying a ``broker_reference_id`` is correlated to the single
  Withdrawal keyed by ``(trading_account, broker_reference_id)`` — idempotent via the partial-unique constraint.
* **Bounded heuristic fallback.** With no reference id, match an EXISTING ref-less Withdrawal on
  account + amount + currency within a bounded time window. **Exactly one** candidate ⇒ correlate; **two or more**
  ⇒ AMBIGUOUS (never guessed); **zero** ⇒ a requested-class event starts a new record, a completion-class event is
  left UNRESOLVED (a completion is never fabricated into a request).
* **Monotonic status, positive evidence only.** ``Withdrawal.status`` only ever ADVANCES
  (REQUESTED→PROCESSING→terminal); it never regresses, and a lower-rank event never downgrades a higher state. A
  requested/completion anchor is backfilled when first observed, enriching provenance without regressing status.
* **Idempotent.** Re-running correlates each event at most once (``BrokerEvent.correlation_status`` advances
  UNRESOLVED→CORRELATED and CORRELATED events are skipped); reference-id get-or-create + monotonic advance mean a
  redelivered event is a no-op.
* **Append-only evidence preserved.** A ``BrokerEvent`` is NEVER edited except its single mutable
  ``correlation_status`` (the model's own ``save()`` guard + DB trigger enforce this); the Withdrawal is the mutable
  projection, as its WP2 schema intends.

ISOLATION: imports ONLY ``broker_intelligence`` models — no execution/strategies/trading mutation, no order, no
credential. Pure + DB-only; ``provenance`` is inherited from the event (defaults SYNTHETIC), so a fixture-derived
withdrawal can never be mistaken for a genuine one. DARK: the live run loop is gated by
``broker_withdrawal_correlation_enabled()``; the engine functions here are pure/testable and have no live caller.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db import IntegrityError, transaction

from .models import BrokerEvent, TransactionCategory, Withdrawal

logger = logging.getLogger("guvfx.broker_intelligence.correlation")

# Lifecycle event_type (from BROKER_EVENT_TYPES) -> the Withdrawal.Status it maps to. A confirmation-required /
# confirmed event is still "requested" (completion not yet observed); approved/processing is in-flight; completed /
# rejected / cancelled are terminal. An event_type outside this map is not a withdrawal-lifecycle event and is not
# correlatable.
# An OPENING event starts a withdrawal record; every other lifecycle event ADVANCES an existing one. In the
# heuristic (ref-less) path this distinction is load-bearing: an opening request is never merged into an existing
# record (two distinct ref-less requests are two withdrawals), while an advancing event attaches to exactly one.
_OPENING_EVENTS = {"WITHDRAWAL_REQUESTED"}
_REQUESTED_EVENTS = {"WITHDRAWAL_REQUESTED", "WITHDRAWAL_CONFIRMATION_REQUIRED", "WITHDRAWAL_CONFIRMED"}
_PROCESSING_EVENTS = {"WITHDRAWAL_PROCESSING", "WITHDRAWAL_APPROVED"}
_COMPLETED_EVENTS = {"WITHDRAWAL_COMPLETED"}
_REJECTED_EVENTS = {"WITHDRAWAL_REJECTED"}
_CANCELLED_EVENTS = {"WITHDRAWAL_CANCELLED"}

# Monotonic rank — status only advances to a strictly higher rank. All three terminal states share rank 3 so a
# terminal withdrawal is never flipped to a different terminal by a later (contradictory) event.
_STATUS_RANK = {
    Withdrawal.Status.UNRESOLVED: 0,
    Withdrawal.Status.PENDING: 1,
    Withdrawal.Status.REQUESTED: 1,
    Withdrawal.Status.PROCESSING: 2,
    Withdrawal.Status.COMPLETED: 3,
    Withdrawal.Status.REJECTED: 3,
    Withdrawal.Status.CANCELLED: 3,
}

# Bounded fallback window for heuristic matching (account+amount+currency within +/- this of the event time).
HEURISTIC_WINDOW = timedelta(days=14)


@dataclass(frozen=True)
class CorrelationResult:
    event_id: int
    outcome: str   # see OUTCOMES
    withdrawal_id: Optional[int] = None


# Stable outcome codes (secret-free).
OUT_REFERENCE = "correlated_reference"
OUT_HEURISTIC = "correlated_heuristic"
OUT_CREATED = "created"                 # a new withdrawal was started from a requested-class event
OUT_AMBIGUOUS = "ambiguous"             # >=2 heuristic candidates — never guessed
OUT_UNRESOLVED = "unresolved"           # a completion with no ref and no match — never fabricated
OUT_ALREADY = "already_correlated"      # event already CORRELATED (idempotent no-op under re-run/concurrency)
OUT_SKIP_NOT_EXTERNAL = "skipped_not_external"
OUT_SKIP_NO_ACCOUNT = "skipped_no_account"
OUT_SKIP_NOT_LIFECYCLE = "skipped_not_lifecycle"


def _status_for_event(event: BrokerEvent) -> Optional[str]:
    et = (event.event_type or "").strip()
    if et in _COMPLETED_EVENTS:
        return Withdrawal.Status.COMPLETED
    if et in _REJECTED_EVENTS:
        return Withdrawal.Status.REJECTED
    if et in _CANCELLED_EVENTS:
        return Withdrawal.Status.CANCELLED
    if et in _PROCESSING_EVENTS:
        return Withdrawal.Status.PROCESSING
    if et in _REQUESTED_EVENTS:
        return Withdrawal.Status.REQUESTED
    return None


def _is_completion(status: str) -> bool:
    """A COMPLETED outcome (sets completed_at). REJECTED/CANCELLED are terminal but NOT completions (no completed_at
    — a rejected/cancelled withdrawal has no processing duration)."""
    return status == Withdrawal.Status.COMPLETED


def _is_terminal(status: str) -> bool:
    return status in (Withdrawal.Status.COMPLETED, Withdrawal.Status.REJECTED, Withdrawal.Status.CANCELLED)


def _event_time(event: BrokerEvent):
    """Broker-stated time when known, else the ingestion receipt time (always present)."""
    return event.occurred_at or event.received_at


def _apply_event(withdrawal: Withdrawal, event: BrokerEvent, target_status: str, *, method: str,
                 created: bool) -> None:
    """Advance ``withdrawal`` from ``event`` (monotonic) and backfill anchors/amount — positive evidence only.
    Persists the withdrawal and marks the event CORRELATED. Never regresses status."""
    dirty = created
    # Monotonic status advance (never regress; never flip one terminal to another).
    if _STATUS_RANK.get(target_status, 0) > _STATUS_RANK.get(withdrawal.status, 0):
        withdrawal.status = target_status
        dirty = True
    # Backfill amount/currency if the record has none and the event supplies it (never overwrite a known value).
    if withdrawal.amount is None and event.amount is not None:
        withdrawal.amount = event.amount
        dirty = True
    if not withdrawal.currency and event.currency:
        withdrawal.currency = event.currency
        dirty = True
    # Requested anchor: first requested/processing-class observation sets requested_at + requested_event.
    if not _is_terminal(target_status):
        if withdrawal.requested_at is None and event.occurred_at is not None:
            withdrawal.requested_at = event.occurred_at
            dirty = True
        if withdrawal.requested_event_id is None:
            withdrawal.requested_event = event
            dirty = True
    # Completion anchor: only a COMPLETED event sets completed_at/completed_event (for duration_seconds()).
    if _is_completion(target_status):
        if withdrawal.completed_at is None:
            withdrawal.completed_at = _event_time(event)
            dirty = True
        if withdrawal.completed_event_id is None:
            withdrawal.completed_event = event
            dirty = True
    # Record the correlation method once (first correlation wins; a later ref-id match never downgrades to heuristic).
    if not withdrawal.correlation_method:
        withdrawal.correlation_method = method
        dirty = True
    if dirty:
        withdrawal.save()
    _mark_event(event, BrokerEvent.CorrelationStatus.CORRELATED)


def _mark_event(event: BrokerEvent, status: str) -> None:
    """Advance ONLY the event's mutable correlation_status (evidential fields stay write-once)."""
    if event.correlation_status != status:
        event.correlation_status = status
        event.save(update_fields=["correlation_status"])


def _find_by_reference(event: BrokerEvent):
    return (Withdrawal.objects
            .filter(trading_account_id=event.trading_account_id,
                    broker_reference_id=event.broker_reference_id).first())


def _correlate_by_reference(event: BrokerEvent, target_status: str) -> CorrelationResult:
    """Deterministic: the Withdrawal keyed by (account, reference-id). If none exists yet, ADOPT a single matching
    ref-less heuristic withdrawal (promote it to this ref) so a ref-less REQUESTED followed by a ref-carrying
    COMPLETED resolves to ONE record — never two. Idempotent under concurrency: the create runs in its OWN savepoint
    so a lost (account, ref) uniqueness race rolls back ONLY that savepoint and the recovery re-reads the winner on a
    still-usable transaction (the WP1 tombstone-poison lesson: never run recovery queries inside an aborted block)."""
    withdrawal = _find_by_reference(event)
    if withdrawal is not None:
        _apply_event(withdrawal, event, target_status, method=Withdrawal.CorrelationMethod.REFERENCE_ID,
                     created=False)
        return CorrelationResult(event.id, OUT_REFERENCE, withdrawal.id)

    # No ref row yet: adopt a single ref-less heuristic candidate (amount+currency+window) rather than create a
    # duplicate. >=2 candidates is ambiguous -> never merge; create the authoritative ref row instead.
    cands = _heuristic_candidates(event) if (event.amount is not None and event.currency) else []
    if len(cands) == 1:
        adopted = cands[0]
        adopted.broker_reference_id = event.broker_reference_id   # promote the heuristic row to the authoritative ref
        _apply_event(adopted, event, target_status, method=Withdrawal.CorrelationMethod.REFERENCE_ID, created=True)
        return CorrelationResult(event.id, OUT_REFERENCE, adopted.id)

    try:
        with transaction.atomic():   # OWN savepoint: a uniqueness race aborts only this, leaving the outer txn usable
            withdrawal = Withdrawal.objects.create(
                trading_account_id=event.trading_account_id,
                broker_reference_id=event.broker_reference_id,
                amount=event.amount, currency=event.currency or "",
                status=target_status, correlation_method=Withdrawal.CorrelationMethod.REFERENCE_ID,
                provenance=event.provenance,
                requested_at=event.occurred_at if not _is_terminal(target_status) else None,
                requested_event=event if not _is_terminal(target_status) else None,
                completed_at=_event_time(event) if _is_completion(target_status) else None,
                completed_event=event if _is_completion(target_status) else None,
            )
    except IntegrityError:
        withdrawal = _find_by_reference(event)   # lost the create race -> the winner exists; advance against it
        if withdrawal is None:
            raise
        _apply_event(withdrawal, event, target_status, method=Withdrawal.CorrelationMethod.REFERENCE_ID,
                     created=False)
        return CorrelationResult(event.id, OUT_REFERENCE, withdrawal.id)
    _mark_event(event, BrokerEvent.CorrelationStatus.CORRELATED)
    return CorrelationResult(event.id, OUT_CREATED, withdrawal.id)


def _heuristic_candidates(event: BrokerEvent):
    """Ref-less Withdrawals for this account matching amount+currency within the bounded window. Ref-id withdrawals
    are excluded (they are matched deterministically by ref, never by heuristic)."""
    anchor = _event_time(event)
    qs = Withdrawal.objects.filter(
        trading_account_id=event.trading_account_id,
        broker_reference_id="",
        amount=event.amount, currency=event.currency,
    )
    lo, hi = anchor - HEURISTIC_WINDOW, anchor + HEURISTIC_WINDOW
    out = []
    for w in qs:
        t = w.requested_at or w.created_at
        if t is not None and lo <= t <= hi:
            out.append(w)
    return out


def _create_heuristic(event: BrokerEvent, target_status: str) -> CorrelationResult:
    withdrawal = Withdrawal.objects.create(
        trading_account_id=event.trading_account_id,
        broker_reference_id="",
        amount=event.amount, currency=event.currency or "",
        status=target_status, correlation_method=Withdrawal.CorrelationMethod.HEURISTIC,
        provenance=event.provenance,
        requested_at=event.occurred_at,
        requested_event=event,
    )
    _mark_event(event, BrokerEvent.CorrelationStatus.CORRELATED)
    return CorrelationResult(event.id, OUT_CREATED, withdrawal.id)


def _correlate_by_heuristic(event: BrokerEvent, target_status: str) -> CorrelationResult:
    # An OPENING request STARTS a new record — never heuristically merged into an existing one (two distinct ref-less
    # requests are two withdrawals; a later advancing event then sees >=2 candidates and is AMBIGUOUS, never guessed).
    # Amount may be absent on an opening request (nullable) — a request legitimately starts a record regardless.
    if (event.event_type or "").strip() in _OPENING_EVENTS:
        return _create_heuristic(event, target_status)
    # ADVANCING event (confirmation/confirmed/processing/approved/completed/rejected/cancelled): attach to an EXISTING
    # ref-less withdrawal. Without amount+currency it cannot be matched and is never fabricated into a new record.
    if event.amount is None or not event.currency:
        return CorrelationResult(event.id, OUT_UNRESOLVED)
    cands = _heuristic_candidates(event)
    if len(cands) == 1:
        _apply_event(cands[0], event, target_status, method=Withdrawal.CorrelationMethod.HEURISTIC, created=False)
        return CorrelationResult(event.id, OUT_HEURISTIC, cands[0].id)
    if len(cands) >= 2:
        _mark_event(event, BrokerEvent.CorrelationStatus.AMBIGUOUS)
        return CorrelationResult(event.id, OUT_AMBIGUOUS)
    # zero candidates: an advancing event with no matching request is never fabricated into a record.
    return CorrelationResult(event.id, OUT_UNRESOLVED)


def correlate_event(event: BrokerEvent) -> CorrelationResult:
    """Correlate ONE event into the withdrawal projection. Fail-closed guards first; all mutation inside one atomic
    block. Returns a ``CorrelationResult`` (never raises for a non-correlatable event). Idempotent at the EVENT level:
    the event row is locked and its mutable ``correlation_status`` re-read inside the transaction, so an already
    CORRELATED event is never re-projected — this serializes concurrent passes and makes double-processing of the
    same event (including the ref-less heuristic-opening path, which has no DB uniqueness) a structural no-op, not
    merely a reliance on the ``run_correlation`` queryset filter (a TOCTOU)."""
    if event.transaction_category != TransactionCategory.EXTERNAL_WITHDRAWAL:
        return CorrelationResult(event.id, OUT_SKIP_NOT_EXTERNAL)
    if event.trading_account_id is None:
        return CorrelationResult(event.id, OUT_SKIP_NO_ACCOUNT)
    target_status = _status_for_event(event)
    if target_status is None:
        return CorrelationResult(event.id, OUT_SKIP_NOT_LIFECYCLE)
    with transaction.atomic():
        # Lock the event row and re-read correlation_status (the only mutable field; evidential fields are immutable
        # so the guards above still hold) so a concurrent/duplicate pass cannot re-create or double-advance.
        locked = BrokerEvent.objects.select_for_update().filter(pk=event.pk).first()
        if locked is None:
            return CorrelationResult(event.id, OUT_SKIP_NOT_LIFECYCLE)
        if locked.correlation_status == BrokerEvent.CorrelationStatus.CORRELATED:
            return CorrelationResult(event.id, OUT_ALREADY)
        event = locked
        if event.broker_reference_id:
            return _correlate_by_reference(event, target_status)
        return _correlate_by_heuristic(event, target_status)


def run_correlation(*, limit: Optional[int] = None) -> dict:
    """Correlate all not-yet-resolved EXTERNAL_WITHDRAWAL events in chronological (append-only) order. Re-entrant and
    idempotent: CORRELATED events are skipped; UNRESOLVED/AMBIGUOUS are re-evaluated (ambiguity may resolve as more
    evidence accrues). Returns a secret-free summary counting each outcome. NO flag check here — the caller (the DARK
    worker/command) gates the live run; this stays pure/testable."""
    qs = (BrokerEvent.objects
          .filter(transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL,
                  correlation_status__in=[BrokerEvent.CorrelationStatus.UNRESOLVED,
                                          BrokerEvent.CorrelationStatus.AMBIGUOUS])
          .order_by("id"))
    if limit is not None:
        qs = qs[:limit]
    summary: dict = {"processed": 0}
    for event in qs:
        res = correlate_event(event)
        summary["processed"] += 1
        summary[res.outcome] = summary.get(res.outcome, 0) + 1
    return summary
