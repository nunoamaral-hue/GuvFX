"""
Wayond truthful-metrics foundation (GFX-WAYOND-TRUTHFUL-METRICS-FOUNDATION-151, ADR-0116).

READ-ONLY, value-blind, DARK. Aggregates Wayond signal-lifecycle *performance* (latency) and
*quality/reliability* metrics from PERSISTED evidence only, with explicit provenance on every
figure. The governing rule is TRUTHFULNESS:

  * A latency stage is reported as a measured percentile set ONLY when a genuine, same-or-declared
    clock-domain timestamp pair is persisted for it, with at least ``MIN_SAMPLE`` valid samples AND
    an acceptable skew-exclusion fraction.
  * A stage whose timestamp is genuinely ABSENT returns ``N/A`` / ``UNMEASURABLE`` with a reason —
    never ``0``, never "success", never an invented duration.
  * Negative or forward-skew deltas (end < start) are EXCLUDED fail-closed and counted, never
    clamped to 0 and never summed. A cross-domain stage whose skew-exclusion fraction is high is
    downgraded MEASURED -> PARTIAL so a survivor-biased estimate is never presented as clean.
  * A positive rate never renders as a false ``0.0`` (``_rate`` keeps tiny positives); a genuine
    measured zero (numerator 0) stays ``0.0``; a zero denominator is ``None``, never ``0.0``.
  * Broker-fill latency is NEVER combined with internal submission latency. The broker clock
    (MT5 ``deal.time``, offset UNVERIFIED) is a distinct domain; at the submission boundary no
    broker ack/fill timestamp exists at all, so those stages are ABSENT.

The signal-intake platform is multi-provider; every AcquiredMessage-based figure here is SCOPED to
the Wayond provider set (``_wayond_provider_ids``) so a second armed provider's data can never be
published under the Wayond banner.

Output is AGGREGATE-ONLY: counts, rates, percentiles, a provider slug and ALLOW-LISTED categorical
reason codes. It emits NO message bodies, Telegram handles, chat ids, account numbers, emails,
credentials or tokens — so it is value-blind by construction (asserted in tests).
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from django.utils import timezone

# --- Wayond identity -------------------------------------------------------------------------
WAYOND_SOURCE = "WAYOND_TELEGRAM"  # PendingSignalApproval.Source / SignalExecutionPlan.source
WAYOND_PROVIDER_MARKER = "wayond"  # matched (case-insensitive) against provider slug / parser slug

# --- status / provenance vocabulary ----------------------------------------------------------
OK = "OK"                          # a trustworthy measured count/rate over a non-empty cohort
MEASURED = "MEASURED"              # a trustworthy measured number over >= MIN_SAMPLE valid samples
PARTIAL = "PARTIAL"                # measured over a subset; some evidence excluded/absent
NA = "N/A"                         # no honest number can be produced
UNMEASURABLE = "UNMEASURABLE"      # the required evidence is structurally absent
EMPTY = "EMPTY"                    # no items at all in scope (genuinely nothing to report)
UNKNOWN_FAIL_CLOSED = "UNKNOWN_FAIL_CLOSED"

# clock domains (declared, never silently mixed)
CLK_PROVIDER = "provider_telegram_utc"
CLK_PLATFORM = "platform_server_utc"
CLK_CROSS = "cross_domain_provider_to_platform"
CLK_BROKER = "broker_wall_as_if_utc_offset_unverified"

MIN_SAMPLE = 20            # minimum valid samples before a percentile is published
MAX_SAMPLE = 50000         # hard cap on rows pulled for a percentile stage (query amplification guard)
MAX_SKEW_FRACTION = 0.25   # above this skew-exclusion fraction a stage is downgraded to PARTIAL
DEFAULT_WINDOW_DAYS = 30

# hold/void reason codes we will emit verbatim; anything else is normalised to ``other`` so the
# output can never leak free text written into the unconstrained hold_reason CharField.
_KNOWN_HOLD_REASONS = frozenset({
    "missing_stop_loss", "missing_take_profit", "volume_split_invalid", "stale_signal",
    "promotion_rejected", "approval_not_approved", "source_not_enabled", "account_not_demo",
    "not_tradeable", "daily_limit_exceeded", "concurrent_limit_exceeded", "plan_integrity_error",
    "execution_globally_disabled", "kill_switch_engaged",
})


def _norm_hold_reason(raw: str) -> str:
    """Map a stored hold_reason to an allow-listed categorical code (unknown -> 'other').

    Strips any ``code:detail`` suffix (keeps the prefix) so e.g. ``promotion_rejected:XYZ`` becomes
    ``promotion_rejected``; a prefix not on the allowlist becomes ``other``. Guarantees value-blind
    output even though hold_reason has no DB ``choices`` constraint."""
    prefix = (raw or "").split(":", 1)[0].strip()
    return prefix if prefix in _KNOWN_HOLD_REASONS else "other"


# =============================================================================================
# pure numeric helpers
# =============================================================================================
def _percentile(sorted_ms: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile over an already-sorted ascending list. ``q`` in [0,1]."""
    n = len(sorted_ms)
    if n == 0:
        return None
    if n == 1:
        return round(float(sorted_ms[0]), 3)
    rank = max(1, math.ceil(q * n))
    return round(float(sorted_ms[min(rank, n) - 1]), 3)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A proportion in [0,1], or None when the denominator is 0 (never 0.0).

    A genuine measured zero (numerator 0) is 0.0. A positive-but-tiny rate that would round to 0.0
    at 4dp keeps more significant figures so it never renders as a FALSE zero."""
    if denominator <= 0:
        return None
    if numerator <= 0:
        return 0.0
    r = numerator / denominator
    rounded = round(r, 4)
    return rounded if rounded > 0.0 else round(r, 10)


def latency_from_pairs(
    stage: str,
    pairs: Sequence[Tuple[object, object]],
    *,
    clock_domain: str,
    numerator: str,
    denominator: str,
    note: str = "",
    min_sample: int = MIN_SAMPLE,
    max_skew_fraction: float = MAX_SKEW_FRACTION,
    capped: bool = False,
) -> Dict:
    """Compute a latency stage from (start, end) datetime pairs.

    * Missing endpoints (either side None) -> EXCLUDED, counted ``excluded_missing``.
    * Negative / forward-skew deltas (end < start) -> EXCLUDED fail-closed, counted ``excluded_skew``
      (NEVER clamped to 0).
    * Percentiles published only when ``valid_samples >= min_sample`` -> MEASURED; 1..min_sample-1
      valid -> PARTIAL (percentiles withheld); 0 valid -> N/A.
    * If the skew-exclusion fraction exceeds ``max_skew_fraction`` the sample is survivor-biased, so
      a would-be MEASURED stage is downgraded to PARTIAL (``high_skew_exclusion``).
    * ``capped`` discloses that the sample is the newest ``MAX_SAMPLE`` rows (recency-biased).
    """
    deltas: List[float] = []
    excluded_missing = 0
    excluded_skew = 0
    for start, end in pairs:
        if start is None or end is None:
            excluded_missing += 1
            continue
        ms = (end - start).total_seconds() * 1000.0
        if ms < 0:
            excluded_skew += 1
            continue
        deltas.append(ms)
    deltas.sort()
    valid = len(deltas)
    considered = valid + excluded_missing + excluded_skew
    out = {
        "stage": stage,
        "clock_domain": clock_domain,
        "numerator": numerator,
        "denominator": denominator,
        "valid_samples": valid,
        "excluded_missing": excluded_missing,
        "excluded_skew": excluded_skew,
        "min_sample": min_sample,
        "capped": bool(capped),
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "note": note,
    }
    if valid == 0:
        out["status"] = NA
        out["na_reason"] = "no_valid_samples"
        return out
    if valid < min_sample:
        out["status"] = PARTIAL
        out["na_reason"] = "insufficient_sample"
        return out
    if considered > 0 and (excluded_skew / considered) > max_skew_fraction:
        out["status"] = PARTIAL
        out["na_reason"] = "high_skew_exclusion"  # survivor-biased: percentiles withheld
        return out
    out["status"] = MEASURED
    out["p50_ms"] = _percentile(deltas, 0.50)
    out["p95_ms"] = _percentile(deltas, 0.95)
    out["p99_ms"] = _percentile(deltas, 0.99)
    if capped:
        out["note"] = (note + " | percentiles over the newest %d rows (recency-biased)." % MAX_SAMPLE).strip()
    return out


def absent_stage(stage: str, *, reason: str, clock_domain: str = "n/a", note: str = "") -> Dict:
    """A lifecycle stage whose timestamp evidence is genuinely ABSENT. Never a measured number."""
    return {
        "stage": stage,
        "status": UNMEASURABLE,
        "clock_domain": clock_domain,
        "valid_samples": 0,
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "na_reason": reason,
        "note": note,
    }


def count_block(status: str, **fields) -> Dict:
    out = {"status": status}
    out.update(fields)
    return out


# =============================================================================================
# aggregation (reads persisted evidence only; no mutation)
# =============================================================================================
def _since(now, window_days: int):
    return now - timedelta(days=max(1, int(window_days)))


def _wayond_provider_ids() -> List[int]:
    """Provider ids that belong to Wayond (slug or parser slug contains 'wayond'). AcquiredMessage
    has no source column, so this is how its figures are scoped to Wayond (mirrors the
    source=WAYOND_SOURCE scoping on approvals/plans). Empty -> no Wayond providers -> EMPTY metrics."""
    from django.db.models import Q
    from signal_intake.models import SignalProvider
    return list(
        SignalProvider.objects.filter(
            Q(slug__icontains=WAYOND_PROVIDER_MARKER)
            | Q(parser_profile__slug__icontains=WAYOND_PROVIDER_MARKER)
        ).values_list("id", flat=True)
    )


def _latency_stages(now, since, provider_ids: List[int]) -> List[Dict]:
    """The latency catalogue. MEASURED where persisted; ABSENT/UNMEASURABLE where not."""
    from signal_intake.models import AcquiredMessage, PendingSignalApproval

    stages: List[Dict] = []

    # (A) source -> acquisition : provider Telegram clock -> platform clock (CROSS-DOMAIN).
    rows = list(
        AcquiredMessage.objects.filter(
            provider_id__in=provider_ids, non_genuine=False,
            acquired_at__gte=since, telegram_date__isnull=False,
        )
        .order_by("-acquired_at")
        .values_list("telegram_date", "acquired_at")[:MAX_SAMPLE]
    )
    stages.append(latency_from_pairs(
        "source_to_acquisition", rows, clock_domain=CLK_CROSS, capped=(len(rows) >= MAX_SAMPLE),
        numerator="AcquiredMessage.acquired_at - AcquiredMessage.telegram_date",
        denominator="genuine Wayond AcquiredMessage rows with a provider timestamp",
        note="CROSS-DOMAIN: Telegram provider clock (tz-aware UTC) minus platform UTC; rows with no "
             "provider date are excluded_missing; a high skew-exclusion fraction downgrades to PARTIAL.",
    ))

    # (B) acquisition -> approval : platform clock on both ends, joined by the approval FK.
    rows = list(
        AcquiredMessage.objects.filter(
            provider_id__in=provider_ids, non_genuine=False,
            acquired_at__gte=since, approval__isnull=False,
        )
        .order_by("-acquired_at")
        .values_list("acquired_at", "approval__created_at")[:MAX_SAMPLE]
    )
    stages.append(latency_from_pairs(
        "acquisition_to_approval_created", rows, clock_domain=CLK_PLATFORM, capped=(len(rows) >= MAX_SAMPLE),
        numerator="PendingSignalApproval.created_at - AcquiredMessage.acquired_at",
        denominator="genuine Wayond AcquiredMessage rows linked to an approval (INTAKEN)",
        note="Same platform clock; joined by AcquiredMessage.approval FK.",
    ))

    # (C) approval decision time : created_at -> reviewed_at (platform clock, single row).
    rows = list(
        PendingSignalApproval.objects.filter(
            source=WAYOND_SOURCE, created_at__gte=since, reviewed_at__isnull=False,
        )
        .order_by("-created_at")
        .values_list("created_at", "reviewed_at")[:MAX_SAMPLE]
    )
    stages.append(latency_from_pairs(
        "approval_decision_time", rows, clock_domain=CLK_PLATFORM, capped=(len(rows) >= MAX_SAMPLE),
        numerator="PendingSignalApproval.reviewed_at - PendingSignalApproval.created_at",
        denominator="reviewed (approved or rejected) Wayond approvals",
        note="Server-COMMIT instant of the decision, not the reviewer's client click time.",
    ))

    # ABSENT / UNMEASURABLE stages — reported truthfully, never a number.
    stages.append(absent_stage(
        "parse_start_to_complete", reason="no_parse_timestamp_persisted", clock_domain="none",
        note="Parse is a pure synchronous unstamped call; only fail-open stdout logs exist.",
    ))
    stages.append(absent_stage(
        "approval_to_submission", reason="correlation_breaks_at_execution_job", clock_domain=CLK_PLATFORM,
        note="ExecutionJob has no correlation_id column (payload JSON only); Wayond execution is un-armed.",
    ))
    stages.append(absent_stage(
        "submission_to_broker_ack", reason="broker_ack_timestamp_absent", clock_domain=CLK_BROKER,
        note="MT5 OrderSendResult carries no time; no ack timestamp is recorded on the real path.",
    ))
    stages.append(absent_stage(
        "broker_ack_to_fill", reason="broker_fill_timestamp_absent_at_submission", clock_domain=CLK_BROKER,
        note="Fill time exists only later as trading.Trade.open_time in the UNVERIFIED broker clock; a "
             "SEPARATE domain, never combined with internal submission latency.",
    ))
    stages.append(absent_stage(
        "end_to_end_source_to_submission", reason="incomplete_correlation_and_absent_submission_time",
        clock_domain=CLK_CROSS,
        note="Requires a single correlation across source..submission; correlation_id is absent on "
             "ExecutionJob/Trade, so this end-to-end stage is structurally unmeasurable.",
    ))
    return stages


def _quality(now, since, provider_ids: List[int]) -> Dict:
    from django.db.models import Count
    from signal_intake.models import AcquiredMessage, PendingSignalApproval

    genuine_qs = AcquiredMessage.objects.filter(
        provider_id__in=provider_ids, non_genuine=False, acquired_at__gte=since)
    nongenuine_qs = AcquiredMessage.objects.filter(
        provider_id__in=provider_ids, non_genuine=True, acquired_at__gte=since)
    genuine_total = genuine_qs.count()
    outcome_counts = {
        row["outcome"]: row["n"] for row in genuine_qs.values("outcome").annotate(n=Count("id"))
    }
    nongenuine_total = nongenuine_qs.count()

    def _rate_of(key):
        return _rate(outcome_counts.get(key, 0), genuine_total)

    messages = count_block(
        OK if genuine_total else EMPTY,
        genuine_observed=genuine_total,
        non_genuine_observed=nongenuine_total,
        note="non_genuine rows (chat-refused / dropped-not-armed) are a SEPARATELY-LABELLED cohort, "
             "never folded into genuine or duplicate counts.",
    )
    outcomes = count_block(
        OK if genuine_total else EMPTY,
        counts=outcome_counts,
        intaken_rate=_rate_of("INTAKEN"),
        update_rate=_rate_of("UPDATE"),
        stale_rate=_rate_of("STALE"),
        quarantined_rate=_rate_of("QUARANTINED"),
        actionable_rate=_rate(outcome_counts.get("INTAKEN", 0) + outcome_counts.get("UPDATE", 0), genuine_total),
    )

    duplicate_rate = count_block(
        NA, value=None, reason="duplicates_rejected_at_unique_constraint_leave_no_row")

    dropped = nongenuine_qs.filter(outcome="DROPPED_NOT_ARMED").count()
    dead_letter = count_block(
        OK if (outcome_counts.get("QUARANTINED", 0) + outcome_counts.get("STALE", 0) + dropped) else EMPTY,
        quarantined=outcome_counts.get("QUARANTINED", 0),
        stale=outcome_counts.get("STALE", 0),
        dropped_not_armed=dropped,
        note="AcquiredMessage is the append-only durable dead-letter/quarantine audit; there is no "
             "separate DLQ and no retry counter (flood-wait/retry are log-only -> N/A).",
    )

    appr_qs = PendingSignalApproval.objects.filter(source=WAYOND_SOURCE, created_at__gte=since)
    appr_total = appr_qs.count()
    appr_status = {row["status"]: row["n"] for row in appr_qs.values("status").annotate(n=Count("id"))}
    approved = appr_status.get("APPROVED", 0)
    rejected = appr_status.get("REJECTED", 0)
    appr_with_corr = appr_qs.exclude(correlation_id="").count()
    approvals = count_block(
        OK if appr_total else EMPTY,
        counts=appr_status,
        approval_rate=_rate(approved, approved + rejected),
        rejection_rate=_rate(rejected, approved + rejected),
        pending=appr_status.get("PENDING_APPROVAL", 0),
        with_correlation_id=appr_with_corr,
        note="EXPIRED status is declared but unimplemented; not modelled as a lifecycle transition.",
    )

    plans = _plan_quality(since)

    plan_total = plans.get("total", 0)
    correlation = count_block(
        OK if (appr_total or plan_total) else EMPTY,
        approval_correlation_rate=_rate(appr_with_corr, appr_total),
        plan_correlation_rate=plans.get("correlation_rate"),
        note="correlation_id is carried on approvals and plans only; ABSENT as a column on "
             "ExecutionJob and trading.Trade, so end-to-end source->trade correlation is incomplete.",
    )

    submission_success = count_block(
        NA, value=None,
        reason="wayond_execution_un_armed_and_executionjob_retcode_is_unstructured_json")
    broker_fill = count_block(
        NA, value=None, reason="no_broker_fill_or_ack_timestamp_at_submission_boundary",
        note="Separate broker clock domain; never conflated with internal submission latency.")
    slippage = count_block(
        NA, value=None, reason="no_intended_price_recorded_to_pair_with_executed_price")
    requotes = count_block(NA, value=None, reason="not_instrumented")
    listener_liveness = count_block(
        NA, value=None, reason="file_heartbeat_only_no_reliability_record_beat_producer",
        note="Wayond listener liveness never reaches the DB; operations-summary reports it UNKNOWN.")
    flood_wait = count_block(NA, value=None, reason="in_memory_and_log_only_not_persisted")

    return {
        "messages_observed": messages,
        "outcomes": outcomes,
        "duplicate_rate": duplicate_rate,
        "dead_letter": dead_letter,
        "approvals": approvals,
        "plans": plans,
        "submission_success": submission_success,
        "broker_fill": broker_fill,
        "slippage": slippage,
        "requotes": requotes,
        "listener_liveness": listener_liveness,
        "flood_wait": flood_wait,
        "correlation_completeness": correlation,
    }


def _plan_quality(since) -> Dict:
    """SignalExecutionPlan quality for the Wayond source. Un-armed in prod, so typically EMPTY —
    reported truthfully. ``is_demo`` splits demo vs live (NOT a synthetic discriminator). Hold/void
    reason codes are allow-list-normalised before emission (value-blind)."""
    from django.db.models import Count
    try:
        from execution.models import SignalExecutionPlan
    except Exception:  # pragma: no cover - execution app always present, defensive only
        return count_block(UNKNOWN_FAIL_CLOSED, total=0, reason="execution_models_unavailable")

    qs = SignalExecutionPlan.objects.filter(source=WAYOND_SOURCE, created_at__gte=since)
    total = qs.count()
    if total == 0:
        return count_block(
            EMPTY, total=0, correlation_rate=None,
            note="No Wayond plans in window (provider un-armed -> no plans/jobs/trades produced).")
    status_counts = {row["status"]: row["n"] for row in qs.values("status").annotate(n=Count("id"))}
    held = status_counts.get("HELD", 0)
    voided = status_counts.get("VOIDED", 0)
    reasons: Dict[str, int] = {}
    for row in qs.exclude(hold_reason="").values("hold_reason").annotate(n=Count("id")):
        reasons[_norm_hold_reason(row["hold_reason"])] = reasons.get(_norm_hold_reason(row["hold_reason"]), 0) + row["n"]
    missing_sl = reasons.get("missing_stop_loss", 0)
    demo = qs.filter(is_demo=True).count()
    return count_block(
        OK, total=total, status_counts=status_counts,
        held_rate=_rate(held, total), voided_rate=_rate(voided, total),
        top_hold_void_reasons=dict(sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:10]),
        missing_stop_loss_rate=_rate(missing_sl, total),
        demo_count=demo, live_count=total - demo,
        correlation_rate=_rate(qs.exclude(correlation_id="").count(), total),
    )


def _freshness(now, provider_ids: List[int]) -> Dict:
    from signal_intake.models import AcquiredMessage, PendingSignalApproval

    def _age(dt):
        return None if dt is None else round((now - dt).total_seconds(), 1)

    newest_acq = (
        AcquiredMessage.objects.filter(provider_id__in=provider_ids, non_genuine=False)
        .order_by("-acquired_at").values_list("acquired_at", flat=True).first()
    )
    newest_appr = (
        PendingSignalApproval.objects.filter(source=WAYOND_SOURCE).order_by("-created_at")
        .values_list("created_at", flat=True).first()
    )
    return {
        "newest_acquisition_age_s": _age(newest_acq),
        "newest_approval_age_s": _age(newest_appr),
        "note": "Freshness is from persisted platform timestamps; listener process liveness is NOT a "
                "DB signal (file heartbeat only -> N/A).",
    }


def _provider_coverage(provider_ids: List[int]) -> Dict:
    from signal_intake.models import SignalProvider

    rows = list(
        SignalProvider.objects.filter(id__in=provider_ids).values_list("slug", "status")
    )
    providers = [{"slug": slug, "status": status} for slug, status in rows]
    armed = [p for p in providers if p["status"] == "ARMED"]
    return {
        "providers": providers,
        "armed_count": len(armed),
        "note": "Wayond-scoped providers only. Wayond MUST remain un-armed in production; an un-armed "
                "provider produces no downstream plans/jobs/trades, so execution-side metrics read EMPTY.",
    }


def build_wayond_metrics(*, now=None, window_days: int = DEFAULT_WINDOW_DAYS) -> Dict:
    """Top-level truthful Wayond metrics aggregate. Pure read; no mutation, no identities emitted.

    The measured cohort is always the GENUINE Wayond cohort (``non_genuine=False``); the non-genuine
    rows (chat-refused / dropped-not-armed) are reported as a separately-labelled set under
    ``quality.messages_observed`` and ``quality.dead_letter`` — never folded in. There is deliberately
    no "all"/synthetic toggle: fixture-vs-live origin is NOT row-distinguishable at intake, so an
    honest combined cohort cannot be produced, and the scope label must never claim one."""
    now = now or timezone.now()
    since = _since(now, window_days)
    provider_ids = _wayond_provider_ids()
    return {
        "schema": "wayond_truthful_metrics.v1",
        "generated_at": now.isoformat(),
        "window_days": int(window_days),
        "cohort": "genuine",  # always the non_genuine=False cohort; non-genuine reported separately
        "value_blind": True,
        "provenance_note": (
            "Aggregate-only, value-blind, Wayond-provider-scoped. Every latency stage and quality figure "
            "carries a status and clock domain. Absent evidence is N/A / UNMEASURABLE (never 0 / success / "
            "invented). A positive rate never renders as a false 0.0. Broker fill latency is a separate, "
            "unverified-offset domain, never combined with internal submission latency. Synthetic/fixture "
            "origin is NOT row-distinguishable at the signal-intake layer, so the measured cohort is the "
            "genuine set (non_genuine=False) and the non-genuine set is reported separately."
        ),
        "freshness": _freshness(now, provider_ids),
        "provider_coverage": _provider_coverage(provider_ids),
        "latency_stages": _latency_stages(now, since, provider_ids),
        "quality": _quality(now, since, provider_ids),
        "boundaries": {
            "broker_fill_separate_from_submission": True,
            "clock_domains": {"provider": CLK_PROVIDER, "platform": CLK_PLATFORM, "broker": CLK_BROKER},
            "min_sample": MIN_SAMPLE,
            "max_skew_fraction": MAX_SKEW_FRACTION,
        },
    }
