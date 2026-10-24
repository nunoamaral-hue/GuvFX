"""WS3 — withdrawal-SPEED analytics (read-only, owner-scoped, never-fabricated).

Builds the member-facing "how fast are my withdrawals processed" view model from the already-correlated
``Withdrawal`` records (projected by ``broker_intelligence.correlation``; EXTERNAL_WITHDRAWAL-only by construction, so
an internal transfer / deposit / UNKNOWN event can never enter any speed figure). This is a PROJECTION on top of the
existing lifecycle+duration model — it adds NO schema, NO ingestion/correlation behaviour, and performs NO mutation.

Truthfulness rules (Sponsor WS3 §3):
* **Speed is measured ONLY from confidently-TIMED COMPLETED withdrawals.** A completed withdrawal enters the headline,
  the chart and the broker aggregates only when it carries BOTH anchors (``requested_at`` + ``completed_at``), is not
  impossible, AND its completion time is broker-STATED (the completion event carried its own ``occurred_at``, not the
  ingestion-receipt fallback — an ingestion backfill/outage lag could otherwise inflate the interval). Everything else
  is DISCLOSED by count and never charted/averaged — missing an anchor (``untimed_count``), completion time only
  observed (``observed_timing_count``), impossible (``quarantined_impossible_count``). A missing duration is NEVER
  coerced to zero. Among the charted samples, reference- vs heuristic-matched counts are disclosed
  (``headline.reference_count`` / ``heuristic_count``): the bounded account+amount+currency+time heuristic is a
  legitimate correlation (never amount-only, single-candidate) but lower-confidence than a deterministic broker
  reference, so its contribution is surfaced rather than weighted silently.
* **Pending is never a failure and never a speed sample.** In-progress (REQUESTED/PROCESSING/PENDING) ages are the
  LIVE elapsed time (``now − requested_at``), returned SEPARATELY and never folded into completed averages.
* **Impossible durations are quarantined, not shown.** A completion observed BEFORE its request (``completed_at <
  requested_at``) is excluded from every speed figure and counted in ``quarantined_impossible_count`` (reason kept).
* **Honest low-data over a misleading metric.** The headline "typical processing time" is presented as *typical* only
  at or above ``MIN_HEADLINE_SAMPLE`` completed samples; below that the raw observation(s) are shown but explicitly
  labelled "not yet typical". A broker shows a median/average only at or above ``MIN_BROKER_SAMPLE``.
* **Currency is preserved, never converted.** Duration is a currency-agnostic interval (seconds); amounts keep their
  own currency and are never summed across currencies and never FX-converted for duration analytics.
* **Genuine-only (prod).** Every figure is over REAL-provenance withdrawals; synthetic/sanitised rows are excluded
  (counted in ``excluded_non_genuine_count`` for transparency, in NO statistic) unless the isolated-demo flag admits
  them (then each row is flagged ``demonstration``).

MINIMUM-SAMPLE POLICY (justification). A median/average over fewer than THREE observations is dominated by a single
value: with n=1 the "median" IS that one observation, and with n=2 it is the midpoint of two — neither is a
*representative* typical time, and presenting it as one would mislead. THREE is the smallest sample where the median
is a genuine middle order-statistic distinct from the extremes. So the headline/broker aggregates require n≥3; below
that the honest low-data state is shown (the individual observations are never hidden — they still appear in the trend
series and history). This is deliberately conservative; it can be revisited with evidence, not lowered for optics.

GENERIC METRIC-SERIES CONTRACT (future-proofing, WS3 §5). ``summarize_durations`` and ``data_quality`` are
metric-agnostic: any future broker-performance dimension (deposit speed, execution speed, support-response time,
slippage, uptime) can reuse them to build the SAME view-model shape (a ``metric`` descriptor + ``headline`` +
``summary`` + ``series`` + per-group ``brokers`` + ``in_progress``). The withdrawal metric is the first realised
dimension; no future dimension is built here (no premature abstraction beyond these two pure helpers + the shape).

Pure + DB-read only. No execution/credential/financial authority; it only reads the withdrawal projection.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Optional

from .metrics import _masked_account, _owner_scoped_q, _row_broker
from .models import Provenance, Withdrawal

# Minimum completed samples before an aggregate is presented as a representative "typical" time (see policy above).
MIN_HEADLINE_SAMPLE = 3
MIN_BROKER_SAMPLE = 3

_PENDING = (Withdrawal.Status.REQUESTED, Withdrawal.Status.PROCESSING, Withdrawal.Status.PENDING)
_FAILED = (Withdrawal.Status.REJECTED, Withdrawal.Status.CANCELLED)


def _human_en(seconds: Optional[int]) -> Optional[str]:
    """A coarse EN-only human string (a convenience; the frontend localises its own EN/JA rendering from the raw
    seconds, so this never drives the UI). None stays None (never a fabricated '0s')."""
    if seconds is None:
        return None
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    mins = round(s / 60)
    if mins < 90:
        return f"{mins} min"
    hours = round(s / 3600)
    if hours < 48:
        return f"{hours} h"
    return f"{round(s / 86400)} d"


def data_quality(n: int, min_sample: int = MIN_BROKER_SAMPLE) -> str:
    """Generic sample-size quality label for ANY metric group: 'insufficient' (<2), 'limited' (2..min_sample-1),
    'sufficient' (>=min_sample). Used to tell the UI whether a per-group aggregate may be shown as representative."""
    if n <= 0:
        return "insufficient"
    if n < 2:
        return "insufficient"
    if n < min_sample:
        return "limited"
    return "sufficient"


def summarize_durations(values, *, min_sample: int = MIN_HEADLINE_SAMPLE) -> dict:
    """Generic duration-sample summary (metric-agnostic). ``values`` is an iterable of non-negative integer seconds
    ALREADY validated (impossible/negative durations excluded by the caller). Returns median/average/min/max + count +
    a ``representative`` flag (count >= min_sample). All None + representative False when the sample is empty — never a
    fabricated zero. Any future metric (deposit/execution/support/…) can reuse this unchanged."""
    vals = [int(v) for v in values if v is not None]
    if not vals:
        return {"count": 0, "median_seconds": None, "average_seconds": None, "fastest_seconds": None,
                "slowest_seconds": None, "representative": False}
    return {
        "count": len(vals),
        "median_seconds": int(statistics.median(vals)),
        "average_seconds": round(statistics.mean(vals)),
        "fastest_seconds": min(vals),
        "slowest_seconds": max(vals),
        "representative": len(vals) >= min_sample,
    }


def _completed_duration(w: Withdrawal):
    """(duration_seconds, impossible) for a COMPLETED withdrawal. duration is None when an anchor is missing (NOT a
    speed sample, never coerced to 0). ``impossible`` is True when completion precedes request (completed_at <
    requested_at) — excluded from every figure and quarantined. A 0-second interval (same observed second) is a valid
    fast completion, not impossible."""
    if w.requested_at is None or w.completed_at is None:
        return None, False
    secs = int((w.completed_at - w.requested_at).total_seconds())
    if secs < 0:
        return None, True
    return secs, False


def _completion_broker_stated(w: Withdrawal) -> bool:
    """True when the COMPLETION time is broker-STATED, i.e. the completion event carried its own ``occurred_at`` so
    ``completed_at`` is the broker's own time — NOT the ingestion-receipt fallback. (The correlation engine sets
    ``completed_at = completed_event.occurred_at or completed_event.received_at``; when occurred_at is absent the
    interval can be inflated by an ingestion backfill/outage lag, so such a completion is disclosed but never charted
    or averaged.) ``requested_at`` is always broker-stated — it is set only from a request event's occurred_at — so
    only the completion anchor needs this check. Fail-safe: a missing completed_event is treated as NOT broker-stated."""
    ev = w.completed_event
    return ev is not None and ev.occurred_at is not None


def _iso(dt):
    return dt.isoformat() if dt else None


def withdrawal_speed(accounts, *, owner=None, now=None, include_demonstration: bool = False) -> dict:
    """The WS3 withdrawal-speed view model for ``accounts`` (the member's OWN accounts) PLUS, when ``owner`` is given,
    the member's OWNER-attributed account-UNRESOLVED withdrawals (ADR-0050). Owner-scoped, read-only, never fabricates.
    ``now`` (injectable for tests) anchors the live in-progress elapsed times; defaults to ``timezone.now()``."""
    if now is None:
        from django.utils import timezone
        now = timezone.now()

    ids = [getattr(a, "id", a) for a in accounts]
    all_rows = list(Withdrawal.objects.filter(_owner_scoped_q(ids, owner))
                    .select_related("trading_account", "requested_event", "completed_event"))
    # GENUINE-only by default (the production leak barrier); the isolated-demo flag ALSO admits synthetic/sanitised
    # rows, each flagged ``demonstration`` so the UI labels them — never mistaken for genuine.
    rows = all_rows if include_demonstration else [w for w in all_rows if w.provenance == Provenance.REAL]
    excluded_non_genuine = len(all_rows) - len(rows)

    pending = [w for w in rows if w.status in _PENDING]
    completed = [w for w in rows if w.status == Withdrawal.Status.COMPLETED]
    failed = [w for w in rows if w.status in _FAILED]
    unresolved = [w for w in rows if w.status == Withdrawal.Status.UNRESOLVED]

    # --- completed speed samples → the trend series + aggregates ---
    # A completed withdrawal feeds the headline / chart / broker aggregates ONLY when it is CONFIDENTLY TIMEABLE:
    # both anchors present, not impossible, AND the completion time is broker-STATED (not the ingestion-receipt
    # fallback — see _completion_broker_stated). Everything else is DISCLOSED by count, never charted/averaged and
    # never coerced to 0:
    #   * impossible (completed before requested) → quarantined_impossible
    #   * missing an anchor                        → untimed
    #   * completion time only OBSERVED (ingestion) → observed_timing (could be inflated by a backfill/outage lag)
    # Among the charted samples we also count REFERENCE vs HEURISTIC matches: the bounded account+amount+currency+time
    # heuristic is a legitimate correlation (never amount-only, single-candidate), but it is lower-confidence than a
    # deterministic broker-reference match, so the UI discloses when it contributes rather than weighting it silently.
    series = []
    by_broker_durations: dict = defaultdict(list)
    all_durations = []
    quarantined_impossible = 0
    observed_timing = 0
    untimed = 0
    reference_samples = 0
    heuristic_samples = 0
    for w in completed:
        secs, impossible = _completed_duration(w)
        if impossible:
            quarantined_impossible += 1
            continue
        if secs is None:
            untimed += 1          # completed but missing an anchor — NOT a sample, never coerced to 0
            continue
        if not _completion_broker_stated(w):
            observed_timing += 1  # completion time only observed (ingestion) — disclosed, never charted/averaged
            continue
        bname = _row_broker(w)
        acct = w.trading_account
        match_basis = ("reference" if w.correlation_method == Withdrawal.CorrelationMethod.REFERENCE_ID
                       else "heuristic")
        if match_basis == "reference":
            reference_samples += 1
        else:
            heuristic_samples += 1
        series.append({
            "withdrawal_id": w.id,
            "broker": bname,
            "account_masked": (_masked_account(acct) if acct is not None else "UNRESOLVED"),
            "account_unresolved": acct is None,
            "amount": str(w.amount) if w.amount is not None else None,   # observed, never summed/fabricated
            "currency": w.currency or "",
            "requested_at": _iso(w.requested_at),
            "completed_at": _iso(w.completed_at),
            "duration_seconds": secs,
            "duration_human": _human_en(secs),
            "match_basis": match_basis,
            "demonstration": w.provenance != Provenance.REAL,
        })
        all_durations.append(secs)
        if bname:
            by_broker_durations[bname].append(secs)
    series.sort(key=lambda r: (r["completed_at"] or "", r["withdrawal_id"]))

    overall = summarize_durations(all_durations, min_sample=MIN_HEADLINE_SAMPLE)

    # --- per-broker comparison (future-proof group shape): median/avg/fastest/slowest + a data-quality label ---
    brokers = []
    for bname, durs in by_broker_durations.items():
        s = summarize_durations(durs, min_sample=MIN_BROKER_SAMPLE)
        brokers.append({
            "broker": bname,
            "sample_count": s["count"],
            "median_seconds": s["median_seconds"],
            "median_human": _human_en(s["median_seconds"]),
            "average_seconds": s["average_seconds"],
            "fastest_seconds": s["fastest_seconds"],
            "slowest_seconds": s["slowest_seconds"],
            "data_quality": data_quality(s["count"], MIN_BROKER_SAMPLE),
            "min_sample": MIN_BROKER_SAMPLE,
        })
    brokers.sort(key=lambda b: (-b["sample_count"], b["broker"]))

    # --- in-progress elapsed ages (LIVE now − requested_at); NEVER mixed into completed averages ---
    in_progress = []
    for w in pending:
        # LIVE elapsed = now − requested_at. A requested_at in the FUTURE (clock skew / mis-parsed broker time) is an
        # anomaly, surfaced as unavailable (None → "N/A") rather than clamped to 0 and shown as a just-made request —
        # the pending-side analogue of the impossible-duration quarantine.
        if w.requested_at is None or w.requested_at > now:
            elapsed = None
        else:
            elapsed = int((now - w.requested_at).total_seconds())
        acct = w.trading_account
        in_progress.append({
            "withdrawal_id": w.id,
            "broker": _row_broker(w),
            "account_masked": (_masked_account(acct) if acct is not None else "UNRESOLVED"),
            "account_unresolved": acct is None,
            "amount": str(w.amount) if w.amount is not None else None,
            "currency": w.currency or "",
            "requested_at": _iso(w.requested_at),
            "elapsed_seconds": elapsed,
            "elapsed_human": _human_en(elapsed),
            "status": w.status,
            "demonstration": w.provenance != Provenance.REAL,
        })
    in_progress.sort(key=lambda r: (r["elapsed_seconds"] is None, -(r["elapsed_seconds"] or 0)))

    # Headline state: empty (no completed sample) / insufficient (1..MIN-1) / ok (>= MIN). The median is the PRIMARY
    # figure, the average SECONDARY; both None until a sample exists. Never a fabricated zero.
    if overall["count"] == 0:
        headline_state = "empty"
    elif overall["count"] < MIN_HEADLINE_SAMPLE:
        headline_state = "insufficient"
    else:
        headline_state = "ok"

    currencies = sorted({(r["currency"] or "") for r in series if r["currency"]})

    return {
        # Metric descriptor — the reusable contract head (future dimensions reuse this shape with a different key/unit).
        "metric": {"key": "withdrawal_processing_time", "unit": "seconds", "domain": "withdrawal",
                   "direction": "lower_is_better", "label": "Withdrawal processing time"},
        "headline": {
            "state": headline_state,
            "median_seconds": overall["median_seconds"],
            "median_human": _human_en(overall["median_seconds"]),
            "average_seconds": overall["average_seconds"],
            "average_human": _human_en(overall["average_seconds"]),
            "sample_count": overall["count"],
            "min_sample": MIN_HEADLINE_SAMPLE,
            "qualifier": "observed_from_notification_emails",
            # Confidence disclosure: how many of the charted/averaged samples were matched deterministically by a broker
            # reference vs by the bounded account+amount+currency+time heuristic (the UI caveats when heuristic > 0).
            "reference_count": reference_samples,
            "heuristic_count": heuristic_samples,
        },
        "summary": {
            "total": len(rows),
            "in_progress_count": len(pending),
            "completed_count": len(completed),              # all completed (with OR without a confidently-timed duration)
            "completed_with_duration_count": overall["count"],   # the charted/averaged speed-sample base (broker-stated)
            "failed_count": len(failed),                    # rejected/cancelled — never pending
            "unresolved_count": len(unresolved),
            "median_seconds": overall["median_seconds"],
            "average_seconds": overall["average_seconds"],
            "fastest_seconds": overall["fastest_seconds"],
            "slowest_seconds": overall["slowest_seconds"],
            "coverage": {"measured": overall["count"], "completed": len(completed)},
            "quarantined_impossible_count": quarantined_impossible,
            # Completed but excluded from the speed sample (each DISCLOSED, never zeroed, never charted):
            "observed_timing_count": observed_timing,   # completion time only observed (ingestion), not broker-stated
            "untimed_count": untimed,                    # completed but missing a request/completion anchor
        },
        "series": series,
        "brokers": brokers,
        "in_progress": in_progress,
        "currencies": currencies,
        "excluded_non_genuine_count": excluded_non_genuine,
        "demonstration": any(w.provenance != Provenance.REAL for w in rows),
        "generated_at": _iso(now),
    }
