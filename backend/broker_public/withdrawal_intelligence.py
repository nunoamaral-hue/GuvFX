"""broker_public — Stream C PUBLIC broker withdrawal-intelligence aggregate (ADR-0080).

Builds the PUBLIC broker-page contract: privacy-safe, aggregate-only withdrawal-performance evidence for ONE broker,
computed live (read-only, never persisted) from Stream B's explicit withdrawal lifecycle across ALL members. It is
AGGREGATED EVIDENCE, never individual testimony.

It reuses the existing broker_public privacy engine (``_eligible_withdrawals`` = certified ∩ genuine ∩ account-resolved
∩ consenting; per-owner winsorisation) and the central ``disclosure`` layer (text safety, timestamp coarsening, the
stage-interval chokepoint, k-anonymity, reason codes). Fail-closed on every axis; when any floor is not met it returns
an explicit SUPPRESSED / PARTIAL / UNAVAILABLE state with a reason code — NEVER zero, fabricated, or partial-leak data.

PROHIBITED in output (enforced by construction): member identity, owner/user id, account number, withdrawal reference,
message id/header, raw amount, exact (sub-day) timestamp, or any free text that has not passed ``safe_public_text``.
PERMITTED: aggregate counts/rates, coarsened durations and dates, methodology, limitations, and the broker's public
slug + sanitised display name. No persistence, no mutation, no activation, no financial action.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict

from django.utils import timezone

from broker_intelligence.external_withdrawal_classifier import CERTIFIED_BROKERS
from broker_intelligence.models import Withdrawal

from . import disclosure
from .aggregation import _active_category, _eligible_withdrawals
from .disclosure import Reason, State

CATEGORY_KEY = "WITHDRAWAL_INTELLIGENCE"

# Conservative Stream C fallbacks used when no ACTIVE BrokerMetricCategory row is seeded (the packet floor:
# >= 5 completed observations for speed, >= 3 distinct account identities). Seed a category row to override.
_FALLBACK = {
    "min_contributors": 3,
    "min_samples": 5,
    "min_completed_samples": 5,
    "max_contributor_share_pct": 40,
    "min_bucket_size": 5,
    "methodology_version": 1,
}

METHODOLOGY = (
    "Aggregate-only broker withdrawal evidence across all contributing GuvFX members. Durations are whole-second "
    "intervals between the broker's own lifecycle events (request / confirmation / completion), each a valid interval "
    "only when both anchors were independently observed; an impossible (out-of-order) interval is discarded, never "
    "charted. Per-member observations are winsorised so no single account can move a figure, and a broker is reported "
    "only once it clears the distinct-contributor and observation floors. Completion is the broker's REPORTED "
    "completion, never an independent funds-received confirmation."
)

LIMITATIONS = [
    "Evidence is observational, derived from broker notification emails correlated by GuvFX; it is not a broker audit.",
    "'Broker reported completed' means the broker reported completion; it is NOT a confirmation that funds were "
    "received. Request-to-confirmation, confirmation-to-completion and total request-to-completion are reported "
    "separately and are never combined; a stage with insufficient both-anchor evidence is shown as unavailable.",
    "Durations and dates are coarsened and samples are k-anonymised, so no individual withdrawal, member, account or "
    "amount can be identified. Below the disclosure thresholds a figure is withheld (SUPPRESSED/PARTIAL), never zero.",
    "Only genuine, certified, account-resolved, consenting evidence is eligible; an uncertified broker reports "
    "UNAVAILABLE by design.",
]


def _stage_summary(pairs, *, min_count: int, min_contributors: int, max_share: int) -> dict:
    """Summarise one stage from its ``(owner_id, seconds)`` both-anchor pairs. Winsorises per OWNER WITHIN the stage's
    own subset (so a member who dominates this stage — even if the whole cohort is winsorise-safe — cannot move the
    stage median) and publishes count + median (+ p25/p75 at >= 4 samples) ONLY when BOTH there are >= ``min_count``
    capped observations AND >= ``min_contributors`` distinct contributors remain. So a stage figure can never be one
    member's exact durations and ``count`` never reveals a sub-floor cohort. Below either floor it returns a
    reason-coded, number-free stub (UNAVAILABLE when there is no both-anchor evidence at all, else PARTIAL). The three
    stages are summarised independently and never combined; never fabricates."""
    by_owner = defaultdict(list)
    for owner_id, secs in pairs:
        if secs is not None:
            by_owner[owner_id].append(secs)
    if not by_owner:
        return {"state": State.UNAVAILABLE, "reason": Reason.STAGE_ANCHORS_MISSING}
    capped_counts = disclosure.winsorise_counts({o: len(v) for o, v in by_owner.items()}, max_share)
    vals = []
    for owner_id, keep in capped_counts.items():
        vals.extend(sorted(by_owner[owner_id])[:keep])
    if len(capped_counts) < min_contributors or len(vals) < min_count:
        return {"state": State.PARTIAL, "reason": Reason.BELOW_MIN_COMPLETED}
    out = {"state": State.PUBLISHED, "count": len(vals), "median_seconds": int(statistics.median(vals))}
    if len(vals) >= 4:
        q = statistics.quantiles(vals, n=4)
        out["p25_seconds"] = int(q[0])
        out["p75_seconds"] = int(q[2])
    else:
        out["p25_seconds"] = None
        out["p75_seconds"] = None
    return out


def _unavailable(public_broker, version, state, reason) -> dict:
    """Reason-coded, number-free stub for a non-publishable broker. Carries NO contributor/observation count, NO
    window, NO freshness timestamp — so the 'nothing to show' payload itself cannot be differenced across repeated or
    rolling-window queries to de-anonymise a tiny cohort (mirrors the INSUFFICIENT_DATA stub discipline)."""
    return {
        "category": CATEGORY_KEY,
        "methodology_version": version,
        "broker": {"slug": public_broker.slug, "display_name": disclosure.safe_public_text(public_broker.display_name)},
        "evidence_status": {"state": state, "reason": reason},
        "methodology": METHODOLOGY,
        "limitations": LIMITATIONS,
    }


def compute_withdrawal_intelligence(public_broker, *, certified_brokers=CERTIFIED_BROKERS, now=None) -> dict:
    """Compute the live, aggregate-only WITHDRAWAL_INTELLIGENCE page contract for ``public_broker``. Pure read-model;
    never raises on no data, never writes. Returns a reason-coded DTO (see module docstring for the shape)."""
    now = now or timezone.now()
    category = _active_category(CATEGORY_KEY)
    min_contrib = category.min_contributors if category else _FALLBACK["min_contributors"]
    min_samples = category.min_samples if category else _FALLBACK["min_samples"]
    max_share = category.max_contributor_share_pct if category else _FALLBACK["max_contributor_share_pct"]
    min_completed = category.min_completed_samples if category else _FALLBACK["min_completed_samples"]
    min_bucket = category.min_bucket_size if category else _FALLBACK["min_bucket_size"]
    version = category.methodology_version if category else _FALLBACK["methodology_version"]

    # Broker identity must resolve unambiguously to its single authoritative internal key, AND be certified. Both
    # fail closed (an uncertified or unkeyed broker is UNAVAILABLE), independent of how much evidence might exist.
    key = (public_broker.source_slug or "").strip()
    if not key:
        return _unavailable(public_broker, version, State.UNAVAILABLE, Reason.AMBIGUOUS_BROKER_IDENTITY)
    if key not in certified_brokers:
        return _unavailable(public_broker, version, State.UNAVAILABLE, Reason.NOT_CERTIFIED)

    rows = _eligible_withdrawals(public_broker, certified_brokers=certified_brokers)
    if not rows:
        return _unavailable(public_broker, version, State.UNAVAILABLE, Reason.NO_ELIGIBLE_EVIDENCE)

    # Group by owner (never exposed) then winsorise so no single member/duplicate burst can dominate a public figure.
    by_owner = defaultdict(list)
    for w in rows:
        by_owner[getattr(w.trading_account, "user_id", None)].append(w)
    raw_contributors = len(by_owner)
    raw_sample = len(rows)
    counts = disclosure.winsorise_counts({o: len(ws) for o, ws in by_owner.items()}, max_share)
    capped = []
    for owner_id, keep in counts.items():
        capped.extend(sorted(by_owner[owner_id], key=lambda w: (w.requested_at or now))[:keep])
    contributor_count = len(counts)
    sample_size = len(capped)

    state, reason = disclosure.cohort_state(contributor_count, sample_size,
                                            min_contributors=min_contrib, min_samples=min_samples)
    if state != State.PUBLISHED:
        # If the RAW cohort would have cleared both floors but the winsorised cohort does not, the cause is a single
        # account (or a duplicate burst) dominating — report that specific reason rather than a bare 'below threshold'.
        if raw_contributors >= min_contrib and raw_sample >= min_samples:
            reason = Reason.SINGLE_ACCOUNT_DOMINANCE
        return _unavailable(public_broker, version, state, reason)

    # Stream C applies small-bucket k-anonymity UNCONDITIONALLY (independent of the WITHDRAWAL_PROCESSING engine's
    # optional BROKER_PUBLIC_MINORITY_SUPPRESSION_ENABLED flag), so the published disclosure always matches the page's
    # unconditional privacy claim. The per-stage completed floor therefore always clears the k-anon bucket too.
    completed_floor = max(min_completed, min_bucket)

    def _stage_pairs(duration_of):
        rows = []
        for w in capped:
            secs = duration_of(w)
            if secs is not None:
                rows.append((getattr(w.trading_account, "user_id", None), secs))
        return rows

    # --- Withdrawal speed: three SEPARATE stage durations, each over its own both-anchor subset, per-stage winsorised
    # with a distinct-contributor floor so no single member can be the figure; never combined. ---
    speed = {
        "request_to_confirmation": _stage_summary(
            _stage_pairs(lambda w: disclosure.stage_seconds(w.requested_at, w.confirmed_at)),
            min_count=completed_floor, min_contributors=min_contrib, max_share=max_share),
        "confirmation_to_completion": _stage_summary(
            _stage_pairs(lambda w: disclosure.stage_seconds(w.confirmed_at, w.completed_at)),
            min_count=completed_floor, min_contributors=min_contrib, max_share=max_share),
        "total_request_to_completion": _stage_summary(
            _stage_pairs(lambda w: w.duration_seconds()),
            min_count=completed_floor, min_contributors=min_contrib, max_share=max_share),
    }

    # --- Lifecycle outcome counts (complete vocabulary); ALWAYS k-anon suppressed (differenceable: sum to sample). ---
    lifecycle = Counter(disclosure.public_lifecycle_bucket(w.status) for w in capped)
    lifecycle.setdefault("receipt_confirmed_if_ever_available", 0)   # no receipt source today — truthful constant 0
    lifecycle_out = disclosure.suppress_small_buckets(
        dict(lifecycle), min_bucket, differenceable=True, protect=("receipt_confirmed_if_ever_available",))

    # --- Outcome rates over an explicit terminal denominator. The three rate GROUPS are themselves k-anon suppressed
    # (differenceable: they sum to the denominator) BEFORE any percentage is published, so a rounded rate plus the
    # published denominator can never be inverted to recover a rare terminal count — the folded 'suppressed' group is
    # padded to >= min_bucket and so can never isolate a single outcome. ---
    terminal = {
        "completion": sum(1 for w in capped if w.status == Withdrawal.Status.COMPLETED),
        "failed": sum(1 for w in capped if w.status in (Withdrawal.Status.REJECTED, Withdrawal.Status.CANCELLED,
                                                        Withdrawal.Status.FAILED)),
        "expired": sum(1 for w in capped if w.status == Withdrawal.Status.EXPIRED),
    }
    denom = sum(terminal.values())
    if denom >= min_samples:
        rates = {"denominator": denom,
                 "denominator_definition": ("resolved terminal outcomes = completed + rejected + cancelled + expired "
                                            "+ failed (in-flight withdrawals are excluded from the denominator)")}
        kanon_groups = disclosure.suppress_small_buckets({k: v for k, v in terminal.items() if v > 0},
                                                         min_bucket, differenceable=True)
        for k, v in kanon_groups.items():
            rates["suppressed_pct" if k == "suppressed" else "%s_pct" % k] = round(100 * v / denom)
    else:
        rates = {"state": State.PARTIAL, "reason": Reason.BELOW_MIN_SAMPLES}

    # --- Confirmation-required incidence as a k-anonymised two-bucket split; ALWAYS suppressed. ---
    required = sum(1 for w in capped if w.confirmation_required_at is not None)
    confirmation_flow = disclosure.suppress_small_buckets(
        {"required_confirmation_step": required, "no_confirmation_step": sample_size - required},
        min_bucket, differenceable=True)

    # --- Per-currency COUNTS only (never amounts); ALWAYS k-anon suppressed. ---
    ccy = Counter((w.currency or "").upper() for w in capped if w.currency)
    ccy_out = disclosure.suppress_small_buckets(dict(ccy), min_bucket)

    newest = max((w.requested_at for w in capped if w.requested_at), default=None)
    freshness = disclosure.coarsen_to_date(newest)
    window_end = disclosure.coarsen_to_date(now)

    return {
        "category": CATEGORY_KEY,
        "methodology_version": version,
        "broker": {"slug": public_broker.slug, "display_name": disclosure.safe_public_text(public_broker.display_name)},
        "evidence_status": {"state": State.PUBLISHED, "reason": Reason.OK},
        "methodology": METHODOLOGY,
        "limitations": LIMITATIONS,
        "observation_window": {"start": None, "end": window_end.isoformat() if window_end else None},
        "data_freshness": freshness.isoformat() if freshness else None,
        "sample_sufficiency": {
            "contributors": contributor_count,
            "observations": sample_size,
            "min_contributors": min_contrib,
            "min_samples": min_samples,
            "sufficient": True,
        },
        "withdrawal_speed": speed,
        "lifecycle_outcomes": {
            "counts": lifecycle_out,
            "rates": rates,
            "confirmation_required_flow": confirmation_flow,
        },
        "per_currency_counts": ccy_out,
    }
