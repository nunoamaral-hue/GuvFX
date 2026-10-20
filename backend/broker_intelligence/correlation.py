"""WP5 — Withdrawal correlation engine: project append-only ``BrokerEvent``s into durable ``Withdrawal`` records.

The single, deterministic path from parsed broker events to a member-visible withdrawal. Contract:

* **Only an ``EXTERNAL_WITHDRAWAL`` event may CREATE a Withdrawal.** INTERNAL_TRANSFER / DEPOSIT / UNKNOWN never
  create one — so an internal transfer (e.g. the genuine TradersWay "confirm your withdrawal request" email, which is
  an internal transfer) can never inflate withdrawal statistics. The one exception is ADVANCE-only: a neutral
  COMPLETED-lifecycle event (category UNKNOWN — the genuine payout completion carries no rail/direction) may ADVANCE
  an EXISTING external Withdrawal it matches by the shared authenticated broker reference (``correlate_completion_event``,
  ADR-0050); it NEVER creates one, so externality is still only ever asserted by the request (positive evidence).
* **Owner-attributed, account-optional (ADR-0050).** A Withdrawal carries both ``trading_account`` (the resolved
  GuvFX account, may be NULL) and ``owner`` (the mailbox owner, independent of the account). An event that resolved
  an account is keyed account-scoped; an owner-attributed event whose account is UNRESOLVED (e.g. the genuine pilot
  withdrawal, whose TradersWay account is not registered) is keyed owner-scoped. Only a truly unattributed event
  (no account AND no owner) is skipped.
* **Reference-id first (deterministic).** An event carrying a ``broker_reference_id`` is correlated to the single
  Withdrawal keyed by ``(trading_account, broker_reference_id)`` when an account is resolved, else
  ``(owner, broker_reference_id)`` over account-NULL rows — idempotent via the two partial-unique constraints.
* **Bounded heuristic fallback.** With no reference id, match an EXISTING ref-less Withdrawal on
  account + amount + currency within a bounded time window. **Exactly one** candidate ⇒ correlate; **two or more**
  ⇒ AMBIGUOUS (never guessed); **zero** ⇒ a requested-class event starts a new record, a completion-class event is
  left UNRESOLVED (a completion is never fabricated into a request).
* **Cross-inbox collapse (ref-less openings; DARK).** Before a ref-less OPENING starts a new record, if a SIBLING
  inbox copy of the SAME logical email (same owner + same non-authoritative RFC822 Message-ID hint on the per-inbox
  delivery rows) already opened a Withdrawal for the SAME ``(owner, trading_account)``, the opening ADOPTS it rather
  than creating a second — so one real withdrawal delivered to two inboxes is ONE Withdrawal (duplicate deliveries
  must not inflate statistics), while BOTH authentic events are preserved. Inert unless the delivery table is
  populated (cross-inbox dedup ON); the live pass is gated so it never arms without it.
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

from .models import BrokerEvent, BrokerMessageObservation, TransactionCategory, Withdrawal

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
OUT_CROSS_INBOX = "correlated_cross_inbox"   # a duplicate inbox copy of one logical email adopted the sibling withdrawal
OUT_CREATED = "created"                 # a new withdrawal was started from a requested-class event
OUT_COMPLETION_ADVANCED = "completion_advanced"   # a neutral completion advanced an EXISTING external withdrawal (never created)
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
    """The Withdrawal keyed by reference — account-scoped when the event resolved an account, else owner-scoped over
    account-UNRESOLVED rows (ADR-0050). The two key spaces are DISJOINT (account NOT NULL vs account NULL), matching
    the two partial-unique indexes, so an owner-attributed withdrawal is never confused with an account-resolved one
    and the create-path IntegrityError recovery re-reads the correct winner."""
    if event.trading_account_id is not None:
        return (Withdrawal.objects
                .filter(trading_account_id=event.trading_account_id,
                        broker_reference_id=event.broker_reference_id).first())
    return (Withdrawal.objects
            .filter(owner_id=event.owner_id, trading_account__isnull=True,
                    broker_reference_id=event.broker_reference_id).first())


def _find_withdrawal_for_completion(event: BrokerEvent):
    """ADVANCE-ONLY finder for a neutral completion (ADR-0050): match an EXISTING external Withdrawal by broker
    reference — by (account, ref) when the completion itself resolved an account, else by (owner, ref). This covers
    the common shape where the external REQUEST resolved an account (or stayed account-UNRESOLVED) but the COMPLETION
    email carries no account, only the shared reference. It never creates; a non-match returns None (so a deposit/
    other completion whose reference matches no external withdrawal has no effect)."""
    qs = Withdrawal.objects.filter(broker_reference_id=event.broker_reference_id)
    if event.trading_account_id is not None:
        w = qs.filter(trading_account_id=event.trading_account_id).first()
        if w is not None:
            return w
    if event.owner_id is not None:
        return qs.filter(owner_id=event.owner_id).first()
    return None


def _correlate_by_reference(event: BrokerEvent, target_status: str) -> CorrelationResult:
    """Deterministic: the Withdrawal keyed by (account, reference-id). If none exists yet, ADOPT a single matching
    ref-less heuristic withdrawal (promote it to this ref) so a ref-less REQUESTED followed by a ref-carrying
    COMPLETED resolves to ONE record in the normal (single-worker) path. Idempotent under concurrency: BOTH the adopt
    (promote-save) and the create run in their OWN savepoint, so a lost (account, ref) uniqueness race rolls back ONLY
    that savepoint and the recovery re-reads the winner on a still-usable transaction (the WP1 tombstone-poison
    lesson: never run recovery queries inside an aborted block). NOTE: under that rare race the adopted heuristic row
    stays orphaned (ref-less, its prior status) while the winner is advanced — never double-counted as COMPLETED; a
    later reconciliation of such orphans is out of scope here."""
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
        try:
            # OWN savepoint (parity with the create path below): promoting the ref-less row to carry this ref can
            # race a concurrent (account, ref) create; isolate that IntegrityError so it aborts ONLY this savepoint
            # and leaves the outer transaction usable for recovery (the WP1 tombstone-poison lesson).
            with transaction.atomic():
                _apply_event(adopted, event, target_status,
                             method=Withdrawal.CorrelationMethod.REFERENCE_ID, created=True)
            return CorrelationResult(event.id, OUT_REFERENCE, adopted.id)
        except IntegrityError:
            withdrawal = _find_by_reference(event)   # a concurrent ref row won the promotion -> advance against it
            if withdrawal is None:
                raise
            _apply_event(withdrawal, event, target_status,
                         method=Withdrawal.CorrelationMethod.REFERENCE_ID, created=False)
            return CorrelationResult(event.id, OUT_REFERENCE, withdrawal.id)

    try:
        with transaction.atomic():   # OWN savepoint: a uniqueness race aborts only this, leaving the outer txn usable
            withdrawal = Withdrawal.objects.create(
                trading_account_id=event.trading_account_id,
                owner_id=event.owner_id,                      # owner attribution, independent of account (ADR-0050)
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
    # Account-scoped when the event resolved an account, else owner-scoped over account-UNRESOLVED rows (ADR-0050).
    if event.trading_account_id is not None:
        qs = Withdrawal.objects.filter(trading_account_id=event.trading_account_id, broker_reference_id="",
                                       amount=event.amount, currency=event.currency)
    else:
        qs = Withdrawal.objects.filter(owner_id=event.owner_id, trading_account__isnull=True, broker_reference_id="",
                                       amount=event.amount, currency=event.currency)
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
        owner_id=event.owner_id,                          # owner attribution, independent of account (ADR-0050)
        broker_reference_id="",
        amount=event.amount, currency=event.currency or "",
        status=target_status, correlation_method=Withdrawal.CorrelationMethod.HEURISTIC,
        provenance=event.provenance,
        requested_at=event.occurred_at,
        requested_event=event,
    )
    _mark_event(event, BrokerEvent.CorrelationStatus.CORRELATED)
    return CorrelationResult(event.id, OUT_CREATED, withdrawal.id)


def _refless_sibling_withdrawal(event: BrokerEvent) -> Optional[Withdrawal]:
    """Cross-inbox collapse for a ref-less OPENING event (§D, corrected per the PD review of #511). The SAME logical
    broker email delivered to TWO inboxes is preserved as two owner-scoped ``BrokerEvent``s (neither discarded —
    data.md); without this, each would open its OWN ref-less ``Withdrawal`` and double-count the SINGLE real
    withdrawal (IS6, the certified broker feeding public metrics, is always ref-less). The per-inbox delivery rows
    (``BrokerMessageObservation``) carry the RFC822 Message-ID as a NON-AUTHORITATIVE cross-inbox HINT: if a SIBLING
    delivery of the same logical message (same owner + same non-empty Message-ID, a DIFFERENT delivery row) already
    projected a ``Withdrawal`` for the SAME ``(owner, trading_account)``, this opening ADOPTS it instead of creating a
    second. The hint only ever MERGES two of ONE owner's OWN deliveries of one email — a conservative under-count on a
    forged/reused Message-ID (never inflation, never cross-owner). Returns the Withdrawal to adopt, or None.

    Inert unless the delivery table is populated (cross-inbox dedup OFF ⇒ no rows ⇒ None ⇒ today's behaviour), and the
    live correlation pass is gated so it never arms without cross-inbox dedup (see the management command). Assumes the
    sequential, single-worker correlation loop (``run_correlation``); it is not a cross-event concurrency primitive."""
    # Message-IDs recorded on THIS event's own delivery row(s) — the owner is taken from the delivery row, not trusted
    # from elsewhere. Blank Message-IDs carry no cross-inbox identity and are skipped.
    own = list(BrokerMessageObservation.objects
               .filter(event=event, rfc822_message_id__gt="")
               .values_list("owner_id", "rfc822_message_id"))
    for owner_id, mid in own:
        if owner_id is None:
            continue
        sibling_event_ids = [e for e in (BrokerMessageObservation.objects
                                         .filter(owner_id=owner_id, rfc822_message_id=mid)
                                         .exclude(event=event)
                                         .values_list("event_id", flat=True)) if e is not None]
        if not sibling_event_ids:
            continue
        w = (Withdrawal.objects
             .filter(requested_event_id__in=sibling_event_ids, trading_account_id=event.trading_account_id)
             .order_by("id").first())
        if w is not None:
            return w
    return None


def _correlate_by_heuristic(event: BrokerEvent, target_status: str) -> CorrelationResult:
    # An OPENING request STARTS a new record — never heuristically merged into an existing one (two distinct ref-less
    # requests are two withdrawals; a later advancing event then sees >=2 candidates and is AMBIGUOUS, never guessed).
    # Amount may be absent on an opening request (nullable) — a request legitimately starts a record regardless.
    if (event.event_type or "").strip() in _OPENING_EVENTS:
        # Cross-inbox collapse FIRST (DARK; inert unless delivery rows carry Message-ID hints): a duplicate inbox copy
        # of the SAME logical email adopts the sibling's withdrawal so duplicate deliveries do not inflate statistics.
        sibling = _refless_sibling_withdrawal(event)
        if sibling is not None:
            _mark_event(event, BrokerEvent.CorrelationStatus.CORRELATED)
            return CorrelationResult(event.id, OUT_CROSS_INBOX, sibling.id)
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
    if event.trading_account_id is None and event.owner_id is None:
        # Neither a resolved account NOR an owner — unattributable, so never projected (ADR-0050: an owner-attributed,
        # account-UNRESOLVED event IS projected, keyed on owner; only a truly unattributed event is skipped).
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


def correlate_completion_event(event: BrokerEvent) -> CorrelationResult:
    """ADVANCE-ONLY completion-by-reference (ADR-0050). A COMPLETED-lifecycle event that is NOT itself an
    ``EXTERNAL_WITHDRAWAL`` (the genuine TradersWay payout COMPLETION is category UNKNOWN — it carries no rail and no
    direction wording, so its externality cannot be asserted from its own text) but DOES carry a broker reference
    ADVANCES an EXISTING external ``Withdrawal`` — matched by the shared authenticated reference, by (account, ref) if
    the completion resolved an account else by (owner, ref) — to COMPLETED.

    It NEVER creates a Withdrawal: externality is only ever asserted by the REQUEST (positive external evidence), so a
    completion with no matching external withdrawal is left UNRESOLVED and a deposit/other completion whose reference
    matches nothing has NO effect — metric inflation from a completion is therefore structurally impossible. Fail-
    closed + idempotent exactly like ``correlate_event`` (event row locked, mutable ``correlation_status`` re-read)."""
    if _status_for_event(event) != Withdrawal.Status.COMPLETED:
        return CorrelationResult(event.id, OUT_SKIP_NOT_LIFECYCLE)
    if not event.broker_reference_id:
        return CorrelationResult(event.id, OUT_UNRESOLVED)       # no reference -> cannot link; never fabricated
    if event.trading_account_id is None and event.owner_id is None:
        return CorrelationResult(event.id, OUT_SKIP_NO_ACCOUNT)
    with transaction.atomic():
        locked = BrokerEvent.objects.select_for_update().filter(pk=event.pk).first()
        if locked is None:
            return CorrelationResult(event.id, OUT_SKIP_NOT_LIFECYCLE)
        if locked.correlation_status == BrokerEvent.CorrelationStatus.CORRELATED:
            return CorrelationResult(event.id, OUT_ALREADY)
        event = locked
        withdrawal = _find_withdrawal_for_completion(event)
        if withdrawal is None:
            return CorrelationResult(event.id, OUT_UNRESOLVED)   # NEVER create a withdrawal from a completion
        _apply_event(withdrawal, event, Withdrawal.Status.COMPLETED,
                     method=withdrawal.correlation_method or Withdrawal.CorrelationMethod.REFERENCE_ID, created=False)
        return CorrelationResult(event.id, OUT_COMPLETION_ADVANCED, withdrawal.id)


def run_correlation(*, limit: Optional[int] = None) -> dict:
    """Correlate all not-yet-resolved events in chronological (append-only) order, in TWO passes. Re-entrant and
    idempotent: CORRELATED events are skipped; UNRESOLVED/AMBIGUOUS are re-evaluated (ambiguity/missing-request may
    resolve as more evidence accrues). Returns a secret-free summary counting each outcome. NO flag check here — the
    caller (the DARK worker/command) gates the live run; this stays pure/testable.

    Pass 1 — ``EXTERNAL_WITHDRAWAL`` events (create + advance). Pass 2 — neutral COMPLETED-lifecycle events that are
    NOT external (category UNKNOWN) and carry a reference: ADVANCE-ONLY by reference (``correlate_completion_event``),
    run AFTER pass 1 so the request has already created the withdrawal the completion advances. A completion can only
    ever complete an externally-evidenced withdrawal; it can never create one, so this pass cannot inflate metrics."""
    summary: dict = {"processed": 0}

    def _drain(qs, fn):
        if limit is not None:
            qs = qs[:limit]
        for event in qs:
            res = fn(event)
            summary["processed"] += 1
            summary[res.outcome] = summary.get(res.outcome, 0) + 1

    # Pass 1: external-withdrawal lifecycle events.
    _drain((BrokerEvent.objects
            .filter(transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL,
                    correlation_status__in=[BrokerEvent.CorrelationStatus.UNRESOLVED,
                                            BrokerEvent.CorrelationStatus.AMBIGUOUS])
            .order_by("id")), correlate_event)
    # Pass 2: neutral (non-external) completion events carrying a reference — advance-only, never create.
    _drain((BrokerEvent.objects
            .filter(event_type__in=list(_COMPLETED_EVENTS), broker_reference_id__gt="",
                    correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)
            .exclude(transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL)
            .order_by("id")), correlate_completion_event)
    return summary
