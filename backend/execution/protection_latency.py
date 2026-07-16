"""
GFX-PKT-TP-PROTECTION-OPTIMISATION WS-A — durable, authoritative TP-protection latency instrumentation.

Computes, per ti_signals plan/leg, the protection transition timestamps and the latency segments the
packet requires, from AUTHORITATIVE DURABLE sources only:
  * ``Trade.close_time``          — broker close time (broker-server time, stored as-if-UTC)
  * ``Trade.close_ingested_at``   — server-side (UTC) instant the close was first ingested (WS-A field)
  * ``ExecutionJob.created_at``   — protection MODIFY enqueued (== the sweep's detection instant)
  * ``ExecutionJob.started_at``   — worker claim
  * ``ExecutionJob.finished_at``  — bridge-verified (the bridge re-reads the live SL and returns it)
  * ``ExecutionJob.result``       — prior_sl / verified_sl / requested_sl / retryable
  * ``ProposedOrderLeg``          — protection_stage / breakeven_attempts / breakeven_applied_at

Principles (packet WS-A):
  * READ-ONLY + reproducible — this is a cache over durable rows, never a source of truth.
  * A missing datapoint is **UNKNOWN (None)**, NEVER a fabricated zero.
  * Broker→UTC conversion is **explicit, configurable and tested** — the broker server timezone is
    still UNVERIFIED, so the two broker-anchored segments (A, H) are flagged ``offset_assumed`` and the
    system also reports the offset-INDEPENDENT ingestion→verified latency that needs no conversion.
  * Source/leg specific — never combines TI with Wayond.

Segment map (packet A–H):
  A broker close → ingestion            close_time(→UTC) → close_ingested_at     (offset-dependent)
  B ingestion → protection detection    close_ingested_at → MODIFY.created_at
  C detection → MODIFY enqueue           ~0 (the sweep detects and enqueues in one pass)
  D enqueue → worker claim               MODIFY.created_at → started_at
  E worker claim → bridge request        UNKNOWN (not separately timestamped inside the worker)
  F bridge request → response            UNKNOWN (folded into claim→verified below)
  G response → broker verification       ~0 (finished_at IS the bridge-verified read)
  H broker close → verified protection   close_time(→UTC) → MODIFY.finished_at    (offset-dependent)
  (+) ingestion → verified               close_ingested_at → finished_at          (offset-INDEPENDENT)
  (+) claim → verified                   started_at → finished_at                 (worker+bridge)
"""
from __future__ import annotations

import os
from datetime import timedelta

from execution.models import ExecutionJob, SignalExecutionPlan
from trading.models import Trade

# Observed broker offset (broker server time = UTC+3 in the reconstructed evidence). UNVERIFIED — the
# broker-server-timezone probe is a separate, still-pending item. Env-configurable + tested.
BROKER_UTC_OFFSET_HOURS = float(os.getenv("BROKER_UTC_OFFSET_HOURS", "3") or 3)

STAGE_TRIGGER = {"BREAKEVEN": 1, "TP2_LOCKED": 2}   # which TP's close triggers each protection stage


def broker_to_utc(dt):
    """Convert a broker-server-time timestamp (stored as-if-UTC) to true UTC using the configured
    offset. Explicit + tested. None → None. Any segment using this MUST be labelled offset-dependent
    (the broker timezone is unverified)."""
    if dt is None:
        return None
    return dt - timedelta(hours=BROKER_UTC_OFFSET_HOURS)


def _secs(a, b):
    """(b - a) in seconds, or None (UNKNOWN) if either endpoint is missing. Never returns 0 for a
    missing endpoint — only for a genuinely simultaneous pair."""
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 2)


def _leg_comment(plan_id, leg_index):
    return "WAY%sL%s" % (plan_id, leg_index)


def _leg_trade(account_id, plan_id, leg_index):
    return (Trade.objects.filter(account_id=account_id, comment=_leg_comment(plan_id, leg_index))
            .order_by("-open_time").first())


def _final_modify(plan_id, leg_index, stage):
    """The bridge-VERIFIED MODIFY for (plan, leg, stage) — the SUCCESS that actually moved the SL
    (not an ``already_closed`` no-op) — and the stage's FIRST attempt (detection/enqueue instant)."""
    jobs = list(ExecutionJob.objects.filter(
        job_type=ExecutionJob.JobType.MODIFY_POSITION, payload__plan_id=plan_id,
        payload__leg_index=leg_index, payload__protection_stage=stage).order_by("created_at"))
    if not jobs:
        return None, None, 0
    first = jobs[0]
    verified = next((j for j in jobs if j.status == ExecutionJob.Status.SUCCESS
                     and not (j.result or {}).get("already_closed")), None)
    # ``or ""`` guards a stored result whose ``error`` key is present but JSON-null (the bridge may
    # emit a uniform ``{"ok":true,...,"error":null}`` envelope) — ``.get("error","")`` returns None in
    # that case, and ``None + str`` would raise, blanking the whole ops section. Mirrors floor-stats.
    defers = sum(1 for j in jobs if "sl_within_stops_level" in
                 (((j.result or {}).get("error", "") or "") + (j.error_message or "")))
    return first, verified, defers


def leg_protection_latency(plan, leg_index) -> dict:
    """Per-leg protection-latency record (transition timestamps + segments). UNKNOWN where a durable
    datapoint is unavailable. ``final_stage`` is the leg's persisted protection_stage."""
    leg = plan.legs.filter(leg_index=leg_index).first()
    stage = (leg.protection_stage if leg else None) or "INITIAL"
    trig_idx = STAGE_TRIGGER.get(stage)
    trig_trade = _leg_trade(plan.account_id, plan.id, trig_idx) if trig_idx else None

    first_job, verified_job, defers = (None, None, 0)
    if stage in STAGE_TRIGGER:
        first_job, verified_job, defers = _final_modify(plan.id, leg_index, stage)

    enqueue = first_job.created_at if first_job else None
    claim = verified_job.started_at if verified_job else None
    verified = verified_job.finished_at if verified_job else None
    res = (verified_job.result or {}) if verified_job else {}
    trig_close = trig_trade.close_time if trig_trade else None            # broker time
    trig_ingested = trig_trade.close_ingested_at if trig_trade else None  # UTC (authoritative)

    return {
        "plan_id": plan.id, "source": plan.source, "leg_index": leg_index,
        "direction": plan.direction, "symbol": plan.symbol,
        "final_stage": stage, "defer_count": defers,
        "retry_count": (leg.breakeven_attempts if leg else None),
        "trigger_tp": trig_idx,
        # transition timestamps (ISO or None=UNKNOWN)
        "trigger_broker_close_at": trig_close.isoformat() if trig_close else None,
        "close_ingested_at": trig_ingested.isoformat() if trig_ingested else None,
        "enqueue_at": enqueue.isoformat() if enqueue else None,
        "claim_at": claim.isoformat() if claim else None,
        "verified_at": verified.isoformat() if verified else None,
        "prior_sl": res.get("prior_sl"), "verified_sl": res.get("verified_sl"),
        "requested_sl": res.get("requested_sl"),
        # segments (seconds; None=UNKNOWN)
        "segments": {
            "A_broker_close_to_ingestion": _secs(broker_to_utc(trig_close), trig_ingested),
            "B_ingestion_to_detection": _secs(trig_ingested, enqueue),
            "C_detection_to_enqueue": 0 if enqueue else None,   # same-pass in this system
            "D_enqueue_to_claim": _secs(enqueue, claim),
            "E_claim_to_bridge_request": None,                  # not separately timestamped
            "F_bridge_request_to_response": None,               # folded into claim→verified
            "G_response_to_verification": 0 if verified else None,
            "H_broker_close_to_verified": _secs(broker_to_utc(trig_close), verified),
            "system_ingestion_to_verified": _secs(trig_ingested, verified),  # offset-INDEPENDENT
            "worker_claim_to_verified": _secs(claim, verified),
        },
        "offset_assumed": {"segments": ["A_broker_close_to_ingestion", "H_broker_close_to_verified"],
                           "broker_utc_offset_hours": BROKER_UTC_OFFSET_HOURS, "verified": False},
    }


def plan_protection_latency(plan) -> list:
    """Latency records for a plan's protection-target legs (2 and 3)."""
    return [leg_protection_latency(plan, i) for i in (2, 3)]


def _pctl(sorted_vals, p):
    if not sorted_vals:
        return None
    idx = max(0, min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 1)


def protection_floor_stats(*, source="ti_signals", days=7) -> dict:
    """WS-E — the BROKER-imposed soft-deferral floor, quantified from history. Groups protection MODIFY
    jobs by (plan, leg, stage); for each group that hit ``sl_within_stops_level`` it measures the
    deferral window (first attempt → bridge-verified success) and whether it RESOLVED naturally or the
    leg CLOSED before protection could verify. Separates reducible (system) from irreducible (broker)
    latency. Read-only. Returns distribution stats overall + by stage + by direction."""
    import statistics
    from collections import defaultdict
    from django.utils import timezone
    since = timezone.now() - timedelta(days=days)
    jobs = list(ExecutionJob.objects.filter(
        job_type=ExecutionJob.JobType.MODIFY_POSITION, created_at__gte=since,
        payload__signal_source=source).order_by("created_at"))
    groups = defaultdict(lambda: {"first": None, "verified": None, "defers": 0,
                                  "stage": None, "dir": None, "plan": None, "leg": None})
    for j in jobs:
        pl, rs = j.payload or {}, j.result or {}
        key = (pl.get("plan_id"), pl.get("leg_index"), pl.get("protection_stage"))
        g = groups[key]
        g["stage"], g["plan"], g["leg"] = pl.get("protection_stage"), pl.get("plan_id"), pl.get("leg_index")
        if g["first"] is None:
            g["first"] = j.created_at
        if "sl_within_stops_level" in ((rs.get("error", "") or "") + (j.error_message or "")):
            g["defers"] += 1
        if j.status == ExecutionJob.Status.SUCCESS and not rs.get("already_closed"):
            g["verified"] = j.finished_at

    # Pre-fetch (direction, account_id) for all deferred plans in ONE query — avoids an N+1 (plus a
    # second redundant lookup) inside the loop.
    deferred_plan_ids = {pid for (pid, _leg, _stage), g in groups.items() if g["defers"]}
    plan_meta = {row[0]: (row[1], row[2]) for row in SignalExecutionPlan.objects.filter(
        id__in=deferred_plan_ids).values_list("id", "direction", "account_id")}

    windows = []           # resolved deferral windows (seconds)
    by_stage = defaultdict(list)
    by_dir = defaultdict(list)
    resolved = closed_first = deferred_groups = 0
    for (pid, leg, stage), g in groups.items():
        if not g["defers"]:
            continue
        deferred_groups += 1
        direction, account_id = plan_meta.get(pid, (None, None))
        if g["verified"] and g["first"]:
            w = round((g["verified"] - g["first"]).total_seconds(), 1)
            windows.append(w); by_stage[stage].append(w); by_dir[direction or "?"].append(w)
            resolved += 1
        else:
            # deferred but never verified → did the leg close first?
            tr = _leg_trade(account_id, pid, leg) if account_id else None
            if tr and tr.close_time is not None:
                closed_first += 1

    def _dist(vals):
        s = sorted(vals)
        return {"n": len(s), "avg_s": round(statistics.mean(s), 1) if s else None,
                "median_s": round(statistics.median(s), 1) if s else None,
                "p95_s": _pctl(s, 0.95), "max_s": max(s) if s else None}

    return {
        "window_days": days, "source": source,
        "deferred_groups": deferred_groups, "resolved_naturally": resolved,
        "closed_before_protection": closed_first,
        "deferral_window": _dist(windows),
        "by_stage": {k: _dist(v) for k, v in by_stage.items()},
        "by_direction": {k: _dist(v) for k, v in by_dir.items()},
        "note": ("sl_within_stops_level is a SOFT, retryable deferral (broker stops/freeze band); it "
                 "is the IRREDUCIBLE floor and never counts toward hard-failure retry exhaustion."),
    }


def recent_protection_latency(*, source="ti_signals", limit=20) -> list:
    """Latency records for the most recent ti_signals plans that reached PROMOTED/CLOSED. Read-only."""
    plans = (SignalExecutionPlan.objects.filter(source=source,
             status__in=(SignalExecutionPlan.Status.PROMOTED, SignalExecutionPlan.Status.CLOSED))
             .order_by("-id")[:limit])
    out = []
    for p in plans:
        out.extend(plan_protection_latency(p))
    return out
