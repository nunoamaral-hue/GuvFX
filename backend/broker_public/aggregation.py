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

from .models import BrokerMetricAggregate, BrokerMetricCategory

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
    """Genuine, certified withdrawals for this public broker across ALL members. Broker match is by the internal
    source slug / display name against the account's broker_name (case-insensitive). Returns [] when the broker is
    not certified (the production default), so nothing is ever aggregated from uncertified/ungenuine data."""
    names = {s for s in (public_broker.source_slug, public_broker.display_name, public_broker.slug) if s}
    if not any((n or "").strip() in certified_brokers for n in names):
        return []   # fail-closed: broker not certified ⇒ no eligible evidence
    lowered = {(n or "").strip().lower() for n in names}
    rows = (Withdrawal.objects.filter(provenance=Provenance.REAL)
            .select_related("trading_account"))
    return [w for w in rows
            if (getattr(w.trading_account, "broker_name", "") or "").strip().lower() in lowered]


def compute_withdrawal_processing(public_broker, *, certified_brokers=CERTIFIED_BROKERS, now=None) -> dict:
    """Compute the WITHDRAWAL_PROCESSING aggregate for ``public_broker``. Returns the aggregate fields; never raises
    on no data (returns INSUFFICIENT_DATA). Does not write — call ``upsert_withdrawal_processing`` to persist."""
    now = now or timezone.now()
    category = _active_category(BrokerMetricCategory.Key.WITHDRAWAL_PROCESSING)
    min_contrib = category.min_contributors if category else 5
    min_samples = category.min_samples if category else 20
    max_share = (category.max_contributor_share_pct if category else 40)
    version = category.methodology_version if category else 1

    rows = _eligible_withdrawals(public_broker, certified_brokers=certified_brokers)

    # Per-contributor cap (winsorisation): no single owner may provide more than max_share% of the sample, so one
    # member/duplicate can't dominate. Group by owner (via the account's user), cap each group.
    by_owner = defaultdict(list)
    for w in rows:
        owner_id = getattr(w.trading_account, "user_id", None)   # the member who owns the account (never exposed)
        by_owner[owner_id].append(w)
    total = sum(len(v) for v in by_owner.values())
    cap = max(1, (total * max_share) // 100) if total else 0
    capped = []
    for owner_id, ws in by_owner.items():
        capped.extend(sorted(ws, key=lambda w: (w.requested_at or now))[:cap] if cap else [])
    contributor_count = len([o for o in by_owner if by_owner[o]])
    sample_size = len(capped)

    freshness = max((w.requested_at for w in capped if w.requested_at), default=None)
    base = {
        "category_key": BrokerMetricCategory.Key.WITHDRAWAL_PROCESSING,
        "methodology_version": version,
        "contributor_count": contributor_count,
        "sample_size": sample_size,
        "window_start": None,
        "window_end": now,
        "data_freshness_at": freshness,
    }
    if contributor_count < min_contrib or sample_size < min_samples:
        return {**base, "state": BrokerMetricAggregate.State.INSUFFICIENT_DATA, "stats": {}}

    lifecycle = Counter(_PUBLIC_LIFECYCLE.get(w.status, "unresolved") for w in capped)
    lifecycle.setdefault("receipt_confirmed_if_ever_available", 0)   # no receipt source today — always surfaced as 0
    # Processing time ONLY from BROKER_REPORTED_COMPLETED withdrawals carrying BOTH anchors (never fabricated).
    durs = [w.duration_seconds() for w in capped
            if w.status == Withdrawal.Status.COMPLETED and w.duration_seconds() is not None]
    processing = None
    if durs:
        processing = {"count": len(durs), "median_seconds": int(statistics.median(durs)),
                      "p25_seconds": int(statistics.quantiles(durs, n=4)[0]) if len(durs) >= 4 else None,
                      "p75_seconds": int(statistics.quantiles(durs, n=4)[2]) if len(durs) >= 4 else None}
    # Per-currency COUNTS only (never amounts — amounts could identify a member's transaction).
    ccy_counts = Counter((w.currency or "").upper() for w in capped if w.currency)
    stats = {
        "lifecycle_counts": dict(lifecycle),
        "processing_time": processing,            # None until there is a completed-with-anchors sample
        "per_currency_counts": dict(ccy_counts),  # counts, not amounts
    }
    return {**base, "state": BrokerMetricAggregate.State.PUBLISHED, "stats": stats}


def upsert_withdrawal_processing(public_broker, *, certified_brokers=CERTIFIED_BROKERS, now=None) -> BrokerMetricAggregate:
    """Compute + persist the current WITHDRAWAL_PROCESSING aggregate for ``public_broker`` (idempotent per
    (broker, category))."""
    result = compute_withdrawal_processing(public_broker, certified_brokers=certified_brokers, now=now)
    key = result.pop("category_key")
    obj, _ = BrokerMetricAggregate.objects.update_or_create(
        public_broker=public_broker, category_key=key, defaults=result)
    return obj
