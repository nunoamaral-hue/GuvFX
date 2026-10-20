"""broker_public — privacy-safe cross-member aggregation (MULTI_ACCOUNT §F).

Computes a PUBLIC WITHDRAWAL_PROCESSING aggregate for ONE broker from ELIGIBLE, CERTIFIED, GENUINE evidence across
ALL members — producing ONLY aggregate statistics, never any member-identifiable datum. Fail-closed on every axis:

* CERTIFIED only: a broker contributes ONLY if it is in ``CERTIFIED_BROKERS`` (empty by default ⇒ nothing is ever
  published in production until a broker is certified through the governance path).
* GENUINE only: ``provenance == REAL`` (synthetic/sanitised fixtures excluded, per the §507 rule).
* THRESHOLDS: below the category's ``min_contributors`` distinct members OR ``min_samples`` observations ⇒
  ``INSUFFICIENT_DATA`` (no stats) — a tiny sample is never published.
* NO DOMINATION: each contributor's observations are capped (winsorised) to ``max_contributor_share_pct`` of the
  sample, so one member or a burst of duplicates cannot move a public statistic.
* TRUTHFUL lifecycle: a broker-processing/COMPLETED notification is reported as BROKER_REPORTED_COMPLETED — NEVER as
  "funds received"; pending items are never counted as completed processing-time observations.

Pure read-model: it reads broker_intelligence.Withdrawal (owner-scoped data aggregated WITHOUT exposing the owner)
and writes only the aggregate row. No execution/credential/financial authority, never a financial action.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict

from django.utils import timezone

from broker_intelligence.external_withdrawal_classifier import CERTIFIED_BROKERS
from broker_intelligence.models import Provenance, Withdrawal

from .flags import broker_public_consent_required, broker_public_minority_suppression_enabled
from .models import BrokerMetricAggregate, BrokerMetricCategory, MetricParticipationConsent

# Map a durable Withdrawal.status to the PUBLIC lifecycle vocabulary (§F). REJECTED/CANCELLED are terminal
# non-completions (never "completed processing"); UNRESOLVED stays visible. RECEIPT_CONFIRMED has no signal today
# (we never infer receipt from a broker-processing notification) so it is always 0 until a receipt source exists.
_PUBLIC_LIFECYCLE = {
    Withdrawal.Status.REQUESTED: "requested",
    Withdrawal.Status.PENDING: "requested",
    Withdrawal.Status.PROCESSING: "processing",
    Withdrawal.Status.COMPLETED: "broker_reported_completed",
    Withdrawal.Status.REJECTED: "terminal_not_completed",
    Withdrawal.Status.CANCELLED: "terminal_not_completed",
    Withdrawal.Status.UNRESOLVED: "unresolved",
}


def _active_category(key: str):
    return (BrokerMetricCategory.objects
            .filter(key=key, status=BrokerMetricCategory.Status.ACTIVE)
            .order_by("-methodology_version").first())


def _eligible_withdrawals(public_broker, *, certified_brokers):
    """Genuine, certified withdrawals for this public broker across ALL members. The broker is identified by the
    SINGLE authoritative internal key ``source_slug`` (the soft link to the internal broker) — NOT the public
    display name or slug, so one public broker can never pick up another broker's withdrawals via a loose name match.
    Returns [] unless ``source_slug`` is set AND certified (empty ``CERTIFIED_BROKERS`` in prod ⇒ nothing eligible)."""
    key = (public_broker.source_slug or "").strip()
    if not key or key not in certified_brokers:
        return []   # fail-closed: no authoritative key, or broker not certified ⇒ no eligible evidence
    key_l = key.lower()
    rows = (Withdrawal.objects.filter(provenance=Provenance.REAL).select_related("trading_account"))
    rows = [w for w in rows
            if (getattr(w.trading_account, "broker_name", "") or "").strip().lower() == key_l]
    if broker_public_consent_required():
        # Participation is OPT-IN and fail-closed: include a member's evidence only while an ACTIVE consent row exists
        # for THIS broker scope (its source_slug) or the all-brokers wildcard. No row ⇒ excluded.
        consenting = set(MetricParticipationConsent.objects
                         .filter(state=MetricParticipationConsent.State.ACTIVE, scope__in=[key, "*"])
                         .values_list("owner_id", flat=True))
        rows = [w for w in rows if getattr(w.trading_account, "user_id", None) in consenting]
    return rows


def _suppress_small_buckets(counts: dict, min_size: int, *, protect=()) -> dict:
    """k-anonymity: collapse any bucket with 0 < value < ``min_size`` into a single ``"suppressed"`` tally, so a rare
    outcome (one member's lone REJECTED, a single-member currency) can never be read as an exact count. Totals are
    CONSERVED (the suppressed tally equals the sum of the removed buckets). Keys in ``protect`` (e.g. the always-0
    receipt sentinel) pass through untouched."""
    out: dict = {}
    suppressed = 0
    for k, v in counts.items():
        if k in protect or v == 0 or v >= min_size:
            out[k] = v
        else:
            suppressed += v
    if suppressed:
        out["suppressed"] = out.get("suppressed", 0) + suppressed
    return out


def _winsorise_counts(raw_counts: dict, max_share_pct: int) -> dict:
    """Iterative water-filling: cap each contributor so NONE exceeds ``max_share_pct`` of the FINAL (post-cap)
    sample — NOT the pre-cap total a dominator could inflate (the flaw a naive single-pass cap has). Each round caps
    every over-share owner to ``(total*share)//100`` and shrinks the total; it converges in a few rounds. A small,
    skewed sample collapses toward INSUFFICIENT_DATA (correct — too dominated to publish)."""
    counts = {o: c for o, c in raw_counts.items() if c > 0}
    for _ in range(100):   # bounded; converges fast
        total = sum(counts.values())
        if total == 0:
            break
        cap = (total * max_share_pct) // 100
        over = [o for o, c in counts.items() if c > cap]
        if not over:
            break
        for o in over:
            counts[o] = cap
    return {o: c for o, c in counts.items() if c > 0}


def compute_withdrawal_processing(public_broker, *, certified_brokers=CERTIFIED_BROKERS, now=None) -> dict:
    """Compute the WITHDRAWAL_PROCESSING aggregate for ``public_broker``. Returns the aggregate fields; never raises
    on no data (returns INSUFFICIENT_DATA). Does not write — call ``upsert_withdrawal_processing`` to persist."""
    now = now or timezone.now()
    category = _active_category(BrokerMetricCategory.Key.WITHDRAWAL_PROCESSING)
    min_contrib = category.min_contributors if category else 5
    min_samples = category.min_samples if category else 20
    max_share = (category.max_contributor_share_pct if category else 40)
    min_completed = category.min_completed_samples if category else 5
    min_bucket = category.min_bucket_size if category else 5
    version = category.methodology_version if category else 1

    rows = _eligible_withdrawals(public_broker, certified_brokers=certified_brokers)

    # Group by owner, then WINSORISE so no single member/duplicate can dominate (bounded against the FINAL sample).
    by_owner = defaultdict(list)
    for w in rows:
        owner_id = getattr(w.trading_account, "user_id", None)   # the member who owns the account (never exposed)
        by_owner[owner_id].append(w)
    counts = _winsorise_counts({o: len(ws) for o, ws in by_owner.items()}, max_share)
    capped = []
    for owner_id, keep in counts.items():
        capped.extend(sorted(by_owner[owner_id], key=lambda w: (w.requested_at or now))[:keep])
    contributor_count = len(counts)
    sample_size = len(capped)

    base = {
        "category_key": BrokerMetricCategory.Key.WITHDRAWAL_PROCESSING,
        "methodology_version": version,
        "contributor_count": contributor_count,
        "sample_size": sample_size,
        "window_start": None,
        "window_end": now,
        "data_freshness_at": None,   # NEVER a raw per-record timestamp; set (coarsened) only on the PUBLISHED path
    }
    if contributor_count < min_contrib or sample_size < min_samples:
        return {**base, "state": BrokerMetricAggregate.State.INSUFFICIENT_DATA, "stats": {}}

    lifecycle = Counter(_PUBLIC_LIFECYCLE.get(w.status, "unresolved") for w in capped)
    lifecycle.setdefault("receipt_confirmed_if_ever_available", 0)   # no receipt source today — always surfaced as 0
    # Processing time ONLY from BROKER_REPORTED_COMPLETED withdrawals with BOTH anchors, AND only once there are
    # >= min_completed such observations, so the published median can never BE a single member's exact duration.
    durs = [w.duration_seconds() for w in capped
            if w.status == Withdrawal.Status.COMPLETED and w.duration_seconds() is not None]
    processing = None
    if len(durs) >= min_completed:
        processing = {"count": len(durs), "median_seconds": int(statistics.median(durs)),
                      "p25_seconds": int(statistics.quantiles(durs, n=4)[0]) if len(durs) >= 4 else None,
                      "p75_seconds": int(statistics.quantiles(durs, n=4)[2]) if len(durs) >= 4 else None}
    # Per-currency COUNTS only (never amounts — amounts could identify a member's transaction).
    ccy_counts = Counter((w.currency or "").upper() for w in capped if w.currency)
    lifecycle_out = dict(lifecycle)
    ccy_out = dict(ccy_counts)
    if broker_public_minority_suppression_enabled():
        # k-anonymity small-bucket collapse — protect the always-0 receipt sentinel (a truthful constant, not a
        # member-identifying count).
        lifecycle_out = _suppress_small_buckets(lifecycle_out, min_bucket,
                                                protect=("receipt_confirmed_if_ever_available",))
        ccy_out = _suppress_small_buckets(ccy_out, min_bucket)
    stats = {
        "lifecycle_counts": lifecycle_out,
        "processing_time": processing,            # None until >= min_completed completed-with-both-anchors samples
        "per_currency_counts": ccy_out,           # counts, not amounts
    }
    # Freshness COARSENED to the date (no sub-day precision — can't pinpoint one member's transaction time) and only
    # on the PUBLISHED path (>= min_contributors + min_samples); INSUFFICIENT_DATA emits no timestamp.
    newest = max((w.requested_at for w in capped if w.requested_at), default=None)
    freshness = newest.replace(hour=0, minute=0, second=0, microsecond=0) if newest else None
    return {**base, "state": BrokerMetricAggregate.State.PUBLISHED, "stats": stats, "data_freshness_at": freshness}


def upsert_withdrawal_processing(public_broker, *, certified_brokers=CERTIFIED_BROKERS, now=None) -> BrokerMetricAggregate:
    """Compute + persist the current WITHDRAWAL_PROCESSING aggregate for ``public_broker`` (idempotent per
    (broker, category))."""
    result = compute_withdrawal_processing(public_broker, certified_brokers=certified_brokers, now=now)
    key = result.pop("category_key")
    obj, _ = BrokerMetricAggregate.objects.update_or_create(
        public_broker=public_broker, category_key=key, defaults=result)
    return obj
