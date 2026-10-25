# ADR-0061 — Withdrawal processing duration is the observed interval between the broker's notification emails

- **Status:** Accepted (Amber). Narrow correlation-anchoring change; additive and under-claim-safe.
- **Decision class:** **Amber** — it changes *which timestamp anchors a withdrawal's `requested_at`* in the correlation
  projection. It does NOT change classification, the external-evidence rule, dedup, the evidence-immutability
  invariant, idempotency, or any Copy Trader / FX / erasure / audit-control state. No schema change.
- **Relates:** WS3 withdrawal-speed analytics (the duration the feature measures); the 2026-10-19 TradersWay #4113783
  completion incident (the duration could not be computed). Builds on ADR-0059/0060.

## Context
WS3 measures the **observed processing time** of a withdrawal, defined on the member page as *"the interval between
the request and completion notification emails"* (not an internal transaction clock, not fund-arrival time).

Two defects prevented that interval from ever being computed for TradersWay (and any broker that does not put a
machine-readable transaction time in the email body):

1. **Correlation anchoring asymmetry.** `_apply_event` set `completed_at = occurred_at OR received_at` (falling back
   to the email's `Date` header) but set `requested_at = occurred_at` only — no `received_at` fallback. TradersWay
   emails carry no in-body transaction time, so `occurred_at` is always `NULL`; the request anchor therefore stayed
   `NULL` and no duration could be derived, even though both emails' `Date` headers are known (09:55:04 → 10:28:05 =
   33m01s for #4113783). The same asymmetry existed in the two withdrawal-create paths.
2. **A WS3 "observed-timing" exclusion built on a wrong premise.** It excluded any completion whose `completed_at`
   came from `received_at` (rather than `occurred_at`), on the assumption that `received_at` was an *ingestion* clock
   that a backfill/outage could inflate. In fact `received_at = parsedate_to_datetime(Date header)` — the broker's
   own email send time — and only falls back to `now()` when an email has **no** `Date` header (rare for broker
   transactional mail). So the exclusion dropped genuine, correctly-timed completions.

## Decision
- The requested anchor falls back to the email `Date` header (`received_at`), **symmetric** with the completion
  anchor, in `_apply_event` and both create paths. `received_at` is a required field, so a non-terminal event always
  supplies a requested anchor. Durations become computable from the two notification emails' broker `Date` headers.
- WS3 treats a completed withdrawal with **both anchors and a non-impossible interval** as a valid speed sample. The
  mistaken "observed-timing" exclusion (and `observed_timing_count`) is removed. The honest framing — *"observed from
  notification emails … not fund arrival"* — is retained, as are the impossible-duration quarantine, the
  missing-anchor (`untimed`) disclosure, the reference-vs-heuristic disclosure, and the ≥3-sample "typical" policy.

## Consequences
- **Positive:** the feature now functions for brokers that state no in-body transaction time; the genuine #4113783
  33m01s is representable; no genuine completion is silently dropped.
- **Trade-off / residual:** an email with **no `Date` header** would anchor on `now()` (fetch time); for a backfill
  that could be inflated. This is rare for broker transactional mail; the impossible-duration quarantine still catches
  completion-before-request, and the "observed from notification emails" label frames the metric honestly. Recording
  the `Date`-header presence to exclude the `now()`-fallback edge is a possible future refinement (would need a field).
- **Safety:** additive (sets an anchor where previously `NULL`; never overwrites a known value), monotonic status
  unchanged, evidence immutability unchanged, owner-scoping/tenant isolation unchanged.
