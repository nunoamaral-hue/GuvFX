"""trade_copier.event_projection — promote accepted fast-path revisions into the AUTHORITATIVE ledger (ADR-0074).

This is the single, deterministic bridge the hybrid detector was missing: how an ingested fast-path emission
(``CopierIngestedRevision``) advances the ADR-0071 lifecycle. The design has ONE authoritative object and no
competing event truth:

* **Authoritative object = ``MasterEvent``** (per-master monotone ``sequence``). The lifecycle
  (``trade_copier.lifecycle``) reads ONLY ``MasterEvent`` rows, ordered by that sequence — unchanged.
* The EVENT path does NOT keep a parallel state. It is a *faster producer of the same ``MasterEvent`` rows*:
  this pass folds a master's NEW ingested revisions into a current-state set, **OVERLAYS** that set onto the
  shared poll baseline (``CopierMaster.last_poll``), runs the EXISTING pure ``diff_positions`` engine, and
  appends canonical ``MasterEvent`` rows (``source='EVENT'``) with the SAME per-master row lock + monotone
  sequence the observer uses, advancing the SAME ``last_poll`` baseline. Poll and event therefore share one
  ledger, one sequence space and one baseline, and dedup automatically (the next poll diffs against the
  event-advanced baseline, so it never re-emits an already-promoted change, and vice-versa).

Why an OVERLAY and not a plain snapshot diff: the event stream is a PARTIAL view (only the tickets the bridge
emitted about), not a full account snapshot like a poll. Diffing a folded partial set directly against the
baseline would falsely FULL_CLOSE every untouched baseline ticket. Overlaying the fold winners onto the
baseline (closed → removed, open → inserted/updated, untouched baseline tickets carried verbatim) yields
events ONLY for the event-touched tickets and keeps the resulting baseline as complete as the poll baseline it
extends — a genuinely missed close is still corrected by the next authoritative poll.

No late OPEN: each promoted ``MasterEvent`` carries the emission's true ``broker_event_time``; the UNCHANGED
lifecycle state machine measures the entry-admissibility deadline from it, so a stale/buffered OPEN is ruled
``ENTRY_FAILED_FINAL`` — never entered — exactly as a late poll-sourced OPEN would be. Reconciliation uses the
same state machine. This pass creates NO ``ExecutionJob`` and contacts NO broker; it is gated
(``copier_event_projection_enabled``, default OFF) and runs on demand (no production schedule).
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from trade_copier import diff
from trade_copier.diff import ObservedPosition, diff_positions
from trade_copier.observer import _level_to_decimal, _num_to_decimal, _positions_from_poll
from trade_copier.revisions import PositionRevision, fold_revisions

logger = logging.getLogger(__name__)


def _revision_from_row(row) -> PositionRevision:
    """Rebuild the pure ``PositionRevision`` from a persisted ``CopierIngestedRevision`` (no I/O)."""
    def _f(v):
        return float(v) if v is not None else None
    return PositionRevision(
        account_id=int(row.account_id), ticket=int(row.ticket), revision=int(row.revision),
        observed_time=row.observed_time, broker_event_time=row.broker_event_time,
        closed=bool(row.closed), symbol=row.symbol or "", side=row.side or "",
        volume=float(row.volume or 0.0), sl=_f(row.sl), tp=_f(row.tp),
        price_open=_f(row.price_open), open_time=row.open_time,
        original_volume=_f(row.original_volume), close_reason=row.close_reason or "",
        broker_login=row.broker_login or "", broker_server=row.broker_server or "",
        event_id=row.event_id or "",
    )


def _admissibility_reference(spec, win):
    """The HONEST age reference for a promoted event's admissibility (the lifecycle's ``broker_event_time``).

    For an OPEN the reference is the position's true broker ``open_time`` (a core MT5 position field), so a
    buffered/late OPEN is ruled inadmissible by its real age, never admitted off the projection wall-clock. A
    host-supplied per-change ``broker_event_time`` is the fallback; for non-OPEN events it is the only source
    (the positions feed has no per-change time → None → the lifecycle falls back to ``observed_at``, as the poll
    path does). Returns None when nothing better than the projection time is known."""
    if win is None:
        return None
    if spec.event_type == diff.OPEN:
        return win.open_time or win.broker_event_time
    return win.broker_event_time


def project_master_events(master, *, now=None) -> dict:
    """Promote one master's NEW ingested revisions into authoritative ``MasterEvent`` rows (idempotent).

    Runs inside a transaction with the master row locked (sequence allocation + baseline advance race-free vs
    the observer). Returns a sanitised summary. Creates NO ExecutionJob and contacts NO broker."""
    from trade_copier.models import (CopierIngestedRevision, CopierMaster, MasterEvent,
                                      MasterObservationPoll, MasterPositionSnapshot)

    now = now or timezone.now()
    master = CopierMaster.objects.select_for_update().get(pk=master.pk)
    watermark = int(master.last_projected_ingest_id or 0)

    new_rows = list(CopierIngestedRevision.objects.filter(master=master, id__gt=watermark).order_by("id"))
    if not new_rows:
        return {"master_id": master.pk, "promoted_events": 0, "watermark": watermark, "advanced": False}

    max_id = max(r.id for r in new_rows)

    # Per-ticket applied-revision high-water marks from ALREADY-projected rows (id <= watermark), so a late or
    # duplicate emission with a revision <= what we already promoted is rejected by the fold (no regression).
    prior_applied: dict[int, int] = {}
    for t, mx in (CopierIngestedRevision.objects
                  .filter(master=master, id__lte=watermark)
                  .values_list("ticket")
                  .annotate(mx=Max("revision"))):
        prior_applied[int(t)] = int(mx)

    fold = fold_revisions((_revision_from_row(r) for r in new_rows), prior_applied=prior_applied)

    # Map each winning ticket to its winning revision (for closed-detection + the true broker_event_time).
    winners: dict[int, PositionRevision] = {}
    for r in new_rows:
        rev = _revision_from_row(r)
        ok, _reason = rev.structurally_valid()
        if not ok:
            continue
        if fold.applied_revision.get(rev.ticket) == rev.revision:
            winners[rev.ticket] = rev

    # OVERLAY the winners onto the baseline (never a bare fold-snapshot diff — the event view is partial).
    baseline = _positions_from_poll(master.last_poll) if master.last_poll_id else []
    by_ticket: dict[int, ObservedPosition] = {int(p.ticket): p for p in baseline}
    for ticket, rev in winners.items():
        if rev.closed:
            by_ticket.pop(int(ticket), None)
        else:
            by_ticket[int(ticket)] = rev.to_observed_position()
    overlay = list(by_ticket.values())

    specs = diff_positions(baseline, overlay)

    # Always create the event-sourced poll (it becomes the new baseline so the next poll dedups against it) and
    # advance the watermark, even when the overlay produced no new events (a duplicate/stale batch).
    identity_login = ""
    identity_server = ""
    for rev in winners.values():
        if rev.broker_login:
            identity_login, identity_server = rev.broker_login, rev.broker_server
            break

    # read_at = now (backend clock, same source as the poll path) so the observer's stale-read backstop stays
    # ACTIVE for an event-advanced baseline: a poll whose network read PREDATES this projection is discarded
    # rather than committing a stale snapshot over it (an event poll with read_at=None would skip that backstop).
    poll = MasterObservationPoll.objects.create(
        master=master, observed_at=now, read_at=now,
        observed_login=identity_login[:64], observed_server=identity_server[:128],
        observed_trade_mode=None, position_count=len(overlay),
        transport_reason="event_projection",
    )
    for p in overlay:
        MasterPositionSnapshot.objects.create(
            poll=poll, position_ticket=int(p.ticket), symbol=p.symbol, side=p.side,
            volume=Decimal(str(p.volume)), sl=_level_to_decimal(p.sl), tp=_level_to_decimal(p.tp),
            price_open=_num_to_decimal(p.price_open), price_current=_num_to_decimal(p.price_current),
            magic=(int(p.magic) if p.magic is not None else None), comment=(p.comment or "")[:128])

    base_seq = MasterEvent.objects.filter(master=master).aggregate(m=Max("sequence"))["m"] or 0
    created = 0
    for i, spec in enumerate(specs, start=1):
        win = winners.get(int(spec.ticket))
        MasterEvent.objects.create(
            master=master, sequence=base_seq + i, from_poll=poll, event_type=spec.event_type,
            position_ticket=spec.ticket, symbol=spec.symbol, side=spec.side,
            volume_before=Decimal(str(spec.volume_before)), volume_after=Decimal(str(spec.volume_after)),
            volume_delta=Decimal(str(spec.volume_delta)),
            prev_sl=_num_to_decimal(spec.prev_sl), new_sl=_num_to_decimal(spec.new_sl),
            prev_tp=_num_to_decimal(spec.prev_tp), new_tp=_num_to_decimal(spec.new_tp),
            source="EVENT",
            broker_event_time=_admissibility_reference(spec, win),
            observed_at=now)
        created += 1

    master.last_poll = poll
    master.last_observed_at = now
    master.last_projected_ingest_id = max_id
    master.save(update_fields=["last_poll", "last_observed_at", "last_projected_ingest_id", "updated_at"])

    return {"master_id": master.pk, "promoted_events": created, "watermark": max_id, "advanced": True,
            "rejected": len(fold.rejected)}


def run_event_projection_pass(*, limit: int = 500) -> dict:
    """One DARK, gated event→authoritative-ledger projection pass over all masters with pending revisions.

    Inert unless ``COPIER_ENABLED`` AND ``COPIER_EVENT_PROJECTION_ENABLED`` are on. Promotes accepted fast-path
    revisions into the authoritative ``MasterEvent`` ledger (shared sequence + baseline, no competing truth);
    creates NO ExecutionJob and contacts NO broker. Returns a secret-free summary."""
    from trade_copier.flags import copier_event_projection_enabled
    if not copier_event_projection_enabled():
        return {"enabled": False, "masters": 0, "promoted_events": 0}

    import uuid
    from trade_copier.models import CopierMaster
    from trade_copier.observer import (_OBSERVATION_LEASE_SECONDS, _claim_observation_lease,
                                       _release_observation_lease)
    masters = (CopierMaster.objects
               .filter(ingested_revisions__isnull=False).distinct().order_by("id")[:limit])
    n = 0
    promoted = 0
    for master in masters:
        n += 1
        # Participate in the SAME single-flight observation lease as the poll observer, so an event projection
        # and a poll can never interleave their shared-baseline advances for one master (a poll holding the
        # lease → this master is skipped this pass, processed on the next).
        token = uuid.uuid4().hex
        if not _claim_observation_lease(master.pk, token, _OBSERVATION_LEASE_SECONDS):
            continue
        try:
            with transaction.atomic():
                out = project_master_events(master)
            promoted += out.get("promoted_events", 0)
        except Exception as e:  # noqa: BLE001 — one master's error must not sink the pass
            logger.exception("copier event projection failed master=%s: %s", master.pk, e)
        finally:
            _release_observation_lease(master.pk, token)
    return {"enabled": True, "masters": n, "promoted_events": promoted}
