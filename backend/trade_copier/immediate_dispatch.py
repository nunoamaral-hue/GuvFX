"""trade_copier.immediate_dispatch — ADR-0094 SYNCHRONOUS real-time dispatch orchestrator.

PRIMARY (low-latency) path. On a signed fast-path emission (an immutable ``CopierIngestedRevision`` already
committed by the ingestion endpoint), this runs the EXISTING governed PRODUCER passes IN-LINE, so the follower
order is SUBMITTED (the governed ``ExecutionJob`` is minted) immediately — target ≤1s, ceiling ≤2s
source→submission — instead of waiting for the ~2-minute scheduled worker cadence. The SLOW observer stays the
RECONCILIATION/recovery path and is never the normal OPEN/MODIFY/CLOSE trigger once this is armed.

Invariants (all enforced here, none relaxed):

* **DARK / default-OFF.** No-op unless ``copier_immediate_dispatch_enabled()`` (ANDed with ``copier_enabled``).
  With it OFF this module does NOTHING, so the ingestion endpoint stays byte-identical to ADR-0074
  (persist-only). It grants NO authority and ORs past nothing.
* **Every producer re-reads its OWN gate** + the full per-pair admission at dispatch time (``admit_open_live`` /
  the ADR-0083 lifecycle preamble), under its OWN row locks — exactly as the scheduled workers do. This
  orchestrator only changes WHEN those governed passes run (now, on the event), never WHAT they may do.
* **Hybrid dedup is inherited.** Projection advances the SHARED ``MasterEvent`` ledger + ``CopierMaster.last_poll``
  baseline under the SAME single-flight observation lease the poll observer uses; the OPEN producer consumes the
  one authorization allowance under ``select_for_update``. A push and a later poll of the same ticket never
  double-copy, and a retry/replay can never place a second order.
* **No blocking broker I/O in the request path.** This path only MINTS governed jobs; it performs NO follower
  bridge read. Follower-ticket LINKAGE (needed for MODIFY/CLOSE targeting) happens through ``reconcile`` AFTER
  the bridge fills the OPEN — a just-minted ``PLACE_ORDER`` is still PENDING here, so an inline read could not
  link it anyway. (A fast, post-fill reconcile cadence is a separate follow-up; see ADR-0094.)
* **Scope.** The producer passes are the existing governed, idempotent, single-flight GLOBAL sweeps (bounded by
  ``limit``); the triggering master's new event is processed immediately. Strict per-master isolation of those
  sweeps under multi-master backlog is a documented limitation, not a safety gap — every produced job is
  independently gated + idempotent.
* **Fail-safe + honest evidence.** Any unexpected error is contained + logged and a ``CONTAINED`` latency row is
  still written for each new event — a real-time fault NEVER breaks the ingestion HTTP response or the immutable
  evidence already committed, and never leaves a produced order without a latency row. No ambiguous financial
  mutation is retried.
"""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 50


def _outcome_for(job):
    from execution.models import ExecutionJob
    from trade_copier.models import CopierRealtimeDispatch as RD
    if job is None:
        return RD.Outcome.NO_ACTION
    return {
        ExecutionJob.JobType.PLACE_ORDER: RD.Outcome.OPEN_SUBMITTED,
        ExecutionJob.JobType.MODIFY_POSITION: RD.Outcome.MODIFY_SUBMITTED,
        ExecutionJob.JobType.CLOSE_TRADE: RD.Outcome.CLOSE_SUBMITTED,
    }.get(getattr(job, "job_type", None), RD.Outcome.NO_ACTION)


def _project_one_master(master, *, now):
    """Promote this master's pending ingested revisions into the authoritative ``MasterEvent`` ledger, under the
    SAME single-flight observation lease the poll observer uses (so a concurrent poll and this projection can
    never interleave their shared-baseline advance for one master). Returns promoted-event count, or None when
    the lease was held by another in-flight pass (then the scheduled path handles it — dedup-safe)."""
    from trade_copier.event_projection import project_master_events
    from trade_copier.observer import (_OBSERVATION_LEASE_SECONDS, _claim_observation_lease,
                                       _release_observation_lease)
    token = uuid.uuid4().hex
    if not _claim_observation_lease(master.pk, token, _OBSERVATION_LEASE_SECONDS):
        return None
    try:
        with transaction.atomic():
            out = project_master_events(master, now=now)
        return int(out.get("promoted_events", 0))
    finally:
        _release_observation_lease(master.pk, token)


def dispatch_master_realtime(master_pk, *, source="EVENT_PUSH", correlation_id="", clock=None,
                             limit=_DEFAULT_LIMIT, mutation_transport_factory=None):
    """Run the governed real-time PRODUCER pipeline for ONE master, synchronously, right after its event was
    ingested. Returns a sanitised summary dict. ``clock`` (→ aware datetime) and ``mutation_transport_factory``
    are injectable for deterministic tests. Never raises to the caller: an internal fault yields a CONTAINED
    summary and still records honest latency evidence for every new event."""
    from trade_copier.flags import (
        copier_immediate_dispatch_enabled, copier_event_projection_enabled, copier_replication_enabled,
        copier_execute_enabled, copier_lifecycle_enabled, copier_mutation_dispatch_enabled)

    clock = clock or timezone.now
    if not copier_immediate_dispatch_enabled():
        # Byte-identical to ADR-0074 persist-only ingestion. Nothing scheduled, nothing recorded.
        return {"enabled": False, "reason": "immediate_dispatch_disabled", "events": 0, "submitted": 0}

    try:
        return _run(master_pk, source=source, correlation_id=correlation_id, clock=clock, limit=limit,
                    mutation_transport_factory=mutation_transport_factory,
                    gates=dict(
                        projection=copier_event_projection_enabled(),
                        replication=copier_replication_enabled(),
                        execute=copier_execute_enabled(),
                        lifecycle=copier_lifecycle_enabled(),
                        mutation_dispatch=copier_mutation_dispatch_enabled(),
                    ))
    except Exception as e:  # noqa: BLE001 — backstop: a real-time fault must never break ingestion; contain + log.
        logger.exception("copier immediate-dispatch contained fault master=%s: %s", master_pk, type(e).__name__)
        return {"enabled": True, "reason": "contained_%s" % type(e).__name__, "events": 0, "submitted": 0,
                "contained": True}


def _run(master_pk, *, source, correlation_id, clock, limit, mutation_transport_factory, gates):
    from trade_copier.models import (CopierIngestedRevision, CopierMaster, CopierRealtimeDispatch,
                                     CopierReplication, MasterEvent)
    from trade_copier.replication import replicate_pending
    from trade_copier.execution_producer import (produce_pending, produce_pending_closes, produce_pending_modifies)
    from trade_copier.lifecycle import run_lifecycle_pass
    from trade_copier.mutation_dispatch import run_mutation_dispatch_pass
    from trade_copier.mutation_transport import mutation_transport

    master = CopierMaster.objects.filter(pk=master_pk).first()
    if master is None:
        return {"enabled": True, "reason": "master_absent", "events": 0, "submitted": 0}

    started = clock()
    pre_seq = MasterEvent.objects.filter(master=master).aggregate(m=Max("sequence"))["m"] or 0
    promoted = None
    contained = None
    try:
        # 1) PROJECT — promote the ingested revision(s) to authoritative MasterEvent(s) on the shared baseline.
        if gates["projection"]:
            promoted = _project_one_master(master, now=started)
        # 2) REPLICATE — one CopierReplication intent per new event × follower (idempotent, unique per pair).
        if gates["replication"]:
            replicate_pending(limit=limit)
        # 3) OPEN execute — the governed LIVE/DEMO producer (admit_open_live + allowance re-checked in-lock).
        if gates["execute"]:
            produce_pending(limit=limit)
        # 4) LIFECYCLE — project per-ticket MODIFY/CLOSE intents from the new events.
        if gates["lifecycle"]:
            run_lifecycle_pass(limit=limit)
        # 5/6) MODIFY/CLOSE produce — gated on the SAME master execute gate as OPEN (symmetric); each producer
        #      re-reads its own per-op LIVE arm + authority (or the DEMO arm) under its lock and fails closed.
        if gates["execute"]:
            produce_pending_modifies(limit=limit)
            produce_pending_closes(limit=limit)
        # 7) MUTATION DISPATCH — send the MODIFY/CLOSE commands to the bound follower bridge (one-shot, no retry).
        if gates["mutation_dispatch"]:
            transport = (mutation_transport_factory or mutation_transport)()
            run_mutation_dispatch_pass(transport=transport, limit=limit)
    except Exception as e:  # noqa: BLE001 — contain, but STILL record evidence below (never a produced order with
        # no latency row). The slow reconciler remains the safety net; nothing is retried.
        logger.exception("copier immediate-dispatch pass fault master=%s: %s", master.pk, type(e).__name__)
        contained = type(e).__name__

    # The follower jobs/commands are now created/sent; this is the GuvFX follower-submission instant.
    submitted_at = clock()

    # 8) LATENCY EVIDENCE — one honest row per new master event × follower. ALWAYS written (even on a contained
    #    fault), so a produced order can never lack evidence. A missing timestamp stays NULL (never a zero).
    new_events = list(MasterEvent.objects.filter(master=master, sequence__gt=pre_seq).order_by("sequence"))
    submitted = 0
    rows = []
    for ev in new_events:
        rev = (CopierIngestedRevision.objects.filter(master=master, ticket=ev.position_ticket)
               .order_by("-revision").first())
        reps = list(CopierReplication.objects.filter(master_event=ev).select_related("execution_job", "follower"))
        # No replication yet (gated/not-yet-replicated) → still record one NO_ACTION/CONTAINED row for the event.
        for rep in (reps or [None]):
            job = getattr(rep, "execution_job", None) if rep else None
            if job is not None:
                outcome = _outcome_for(job)
            elif contained:
                outcome = CopierRealtimeDispatch.Outcome.CONTAINED
            else:
                outcome = CopierRealtimeDispatch.Outcome.NO_ACTION
            row = CopierRealtimeDispatch(
                master=master, master_event=ev, replication=rep,
                follower_account_id=(rep.follower.follower_account_id if rep else None),
                master_ticket=ev.position_ticket, event_type=ev.event_type,
                dispatch_source=source, outcome=outcome, correlation_id=str(correlation_id)[:40],
                reason=(contained or "")[:64],
                event_detected_at=ev.broker_event_time,
                emitted_at=(rev.observed_time if rev else None),
                backend_received_at=(rev.ingested_at if rev else None),
                accepted_at=ev.observed_at,
                dispatch_started_at=started,
                follower_submitted_at=(job.created_at if job is not None else None))
            row.recompute_latencies()
            rows.append(row)
            if outcome != CopierRealtimeDispatch.Outcome.NO_ACTION and job is not None:
                submitted += 1
    if rows:
        CopierRealtimeDispatch.objects.bulk_create(rows)

    return {
        "enabled": True, "reason": contained or "ok", "contained": bool(contained), "master_id": master.pk,
        "source": source, "promoted": promoted, "events": len(new_events), "submitted": submitted,
        "dispatch_started_at": started.isoformat(), "submitted_at": submitted_at.isoformat(),
        "late": sum(1 for r in rows if r.is_late and r.outcome not in (
            CopierRealtimeDispatch.Outcome.NO_ACTION, CopierRealtimeDispatch.Outcome.CONTAINED)),
    }
