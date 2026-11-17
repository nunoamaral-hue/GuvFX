# ADR-0116 — Wayond truthful-metrics foundation (contract + provenance)

- **Status:** Accepted (repository-only; additive; **migration-free**; DARK; default behaviour unchanged;
  no production deploy, no endpoint/flag/timer enablement, no execution/broker/MT5/Copy-Trading action by this packet).
- **Packet:** GFX-WAYOND-TRUTHFUL-METRICS-FOUNDATION-151.
- **Date:** 2026-11-17.
- **Builds on (never replaces):** the signal-intake acquisition ledger (`signal_intake.AcquiredMessage`,
  `PendingSignalApproval`, `SignalAuditEvent`), the execution plan model (`execution.SignalExecutionPlan`),
  `reliability` operations-summary (ADR observability), and the established truthful-aggregate idiom in
  `analytics.portfolio_fx` (OK/PARTIAL/UNAVAILABLE/EMPTY — a measured number only when there genuinely is one).

## Context

Wayond is a Telegram signal source whose lifecycle spans `signal_intake` → (`intelligence` parse) → `execution`
plan/job → `trading` fill. A Phase-1 static source audit (pkt-151) established, truthfully, which timestamps and
reason codes are **persisted** versus **absent**:

- **Measured, persisted:** `AcquiredMessage.telegram_date` (provider/Telegram UTC), `AcquiredMessage.acquired_at`,
  `PendingSignalApproval.created_at` / `reviewed_at`, `SignalExecutionPlan.created_at` (all platform/server UTC),
  outcome/hold/approval reason-code vocabularies.
- **Absent / unmeasurable:** parse start/complete (parse is an unstamped synchronous call — only fail-open stdout
  logs); broker submission ACK and broker FILL timestamps at the submission boundary (MT5 `OrderSendResult` carries
  no time); slippage (no intended price is paired with the executed price); the real-path order_send round-trip
  (only a SHADOW `order_check` is timed, log-only); duplicate RATE (duplicates are rejected at the DB unique
  constraint and leave no durable row); listener liveness in the DB (file heartbeat only, no `record_beat` producer);
  flood-wait / retry counts (in-memory + log-only).
- **Clock domains that must never be silently mixed:** provider/Telegram UTC; platform/server UTC;
  broker wall-time stored as-if-UTC with an **UNVERIFIED** offset (`BROKER_UTC_OFFSET_HOURS`, flagged
  `verified:false`). `trading.Trade.open_time`/`close_time` live in that broker domain.
- **Correlation is incomplete:** `correlation_id` is a column on `PendingSignalApproval` and `SignalExecutionPlan`
  only; `ExecutionJob` (payload JSON only) and `trading.Trade` (never written) have none — so an end-to-end
  source→submission join is structurally impossible from persisted data.
- **No per-row synthetic flag at intake:** fixture-vs-live origin is decided by the invoking command, not stored.
  Downstream, synthetic/shadow is marked by `source_stage=TEST`, `ExecutionJob.job_type=PLACE_ORDER_SHADOW`,
  `signal_source=__ops_probe__` / `comment=OPSPROBE`.

The gap is a **truthful** metrics read-model: measure what genuinely exists, and return explicit N/A for the rest —
never a fabricated 0, success, or invented duration, and never conflating broker-fill latency with internal latency.

## Decision

Add a **pure, aggregate-only, value-blind** Wayond metrics service plus a DARK staff surface, introducing **no
migration** and changing no existing contract. Measure existing evidence first; document instrumentation gaps as
future work rather than adding speculative columns.

1. **Pure service** `reliability/services/wayond_metrics.py` — `build_wayond_metrics(now, window_days, include_synthetic)`
   returns a provenance-rich dict. Aggregate-only: counts, rates, percentiles, provider slug and categorical reason
   codes. It emits **no** message body, Telegram handle, chat id, account number, email, credential or token.
2. **Staff-only DARK endpoint** `GET /api/reliability/wayond-metrics/` (`IsAdminUser` + `WAYOND_METRICS_OPERATOR_ENABLED`
   → 404 when unset). Gate order is permission-then-flag (non-staff 403; staff 404 while dark).
3. **Read-only command** `wayond_metrics` — prints the same sanitized JSON for verification; mutates nothing.
4. **DARK operator UI** `admin/operations/wayond-metrics` — coverage, latency stages (p50/p95/p99 or N/A+reason) and
   the truthful N/A quality block. Backend-only deploy never ships it; backend is the real gate.

## Metric contract

**State vocabulary.** Counts: `OK` (measured over a non-empty cohort) / `PARTIAL` / `UNAVAILABLE` / `EMPTY`
(nothing in scope). Latency: `MEASURED` (≥ `MIN_SAMPLE` valid samples) / `PARTIAL` (1..MIN_SAMPLE-1, percentiles
withheld) / `N/A` (0 valid) / `UNMEASURABLE` (evidence structurally absent) / `UNKNOWN_FAIL_CLOSED`.

**Per latency metric.** Each stage declares: **numerator** (end − start expression); **denominator** (the cohort of
pairs); **timestamp source** and **clock domain**; **negative/forward-skew handling** — a delta with `end < start`
is EXCLUDED fail-closed and counted (`excluded_skew`), never clamped to 0; **missing-endpoint handling**
(`excluded_missing`); **exclusions** (non-genuine rows excluded from the genuine cohort); **minimum sample size**
`MIN_SAMPLE = 20` before any percentile is published; **percentiles** nearest-rank p50/p95/p99 (deterministic,
dependency-free); **freshness** (age of newest persisted row); **N/A rule** (no valid samples → `N/A` with
`na_reason`; absent evidence → `UNMEASURABLE`).

**Latency catalogue.**

| Stage | Status | Clock domain | Note |
|---|---|---|---|
| `source_to_acquisition` | MEASURED where data | cross (provider→platform) | `acquired_at − telegram_date`; rows with no provider date excluded_missing |
| `acquisition_to_approval_created` | MEASURED where data | platform | joined by `AcquiredMessage.approval` FK |
| `approval_decision_time` | MEASURED where data | platform | `reviewed_at − created_at`; server-COMMIT instant, not client click |
| `parse_start_to_complete` | UNMEASURABLE | none | no parse timestamp persisted |
| `approval_to_submission` | UNMEASURABLE | platform | `ExecutionJob` has no correlation_id column; Wayond un-armed |
| `submission_to_broker_ack` | UNMEASURABLE | broker | MT5 OrderSendResult carries no time |
| `broker_ack_to_fill` | UNMEASURABLE | broker | fill time only later as `Trade.open_time`, unverified broker clock — **separate** |
| `end_to_end_source_to_submission` | UNMEASURABLE | cross | incomplete correlation + absent submission time |

**Quality / reliability.** Measured from durable ledgers: messages observed (genuine `non_genuine=False`), outcome
breakdown + rates (intaken/update/stale/quarantined/actionable), dead-letter counts (QUARANTINED/STALE/
DROPPED_NOT_ARMED), approval rate / rejection rate / pending / approval-time percentiles, plan held/voided rates +
top hold/void reason codes + missing-stop-loss rate (Wayond source; typically EMPTY while un-armed), correlation
completeness (provenance). Explicit `N/A`: duplicate rate (no durable duplicate row), submission success (un-armed +
retcode is unstructured JSON), fill rate / requotes / slippage (absent), listener liveness (file-only), flood-wait /
retry (log-only).

**Broker-fill separation (invariant).** The broker clock is a distinct, offset-UNVERIFIED domain. At the submission
boundary no broker ack/fill timestamp exists, so those stages are `UNMEASURABLE`. The service **never** combines a
broker-domain delta with an internal (platform) submission delta; `boundaries.broker_fill_separate_from_submission`
is asserted `true` and covered by tests.

## Synthetic / genuine cohort policy

There is no single `is_synthetic` flag. At the signal-intake layer the genuine cohort is `non_genuine=False`, with
the explicit caveat that fixture-origin is **not row-distinguishable** (so intake genuineness is PARTIAL by
provenance). The non-genuine cohort (chat-refused / DROPPED_NOT_ARMED) is reported separately and never folded into
duplicate or genuine counts. Downstream synthetic/shadow exclusion (documented for when execution is armed):
`source_stage=TEST`, `job_type=PLACE_ORDER_SHADOW`, `signal_source=__ops_probe__` / `comment=OPSPROBE`. Synthetic
records are never counted in a genuine production cohort.

## Privacy, tenancy and publication boundary

- **Value-blind by construction:** the output is aggregate counts/rates/percentiles + provider slug + categorical
  reason codes. No message body, handle, chat id, account number, email, credential or token is emitted (asserted in
  tests). No per-row identity is returned, so there is no cross-tenant join hazard (correlation_id is shared across a
  fan-out and is never used as a cross-tenant key here).
- **Staff-only aggregate access**; the owner/tenant binding first appears at planning (`account→user`), and the
  foundation deliberately reports **provider-scoped aggregates only**, not per-tenant rows.
- **Publication boundary (OUT OF SCOPE for activation):** a future public Wayond/broker page could at most publish
  coarse, k-anon-thresholded QUALITY counts (e.g. actionable rate, approval rate) over a minimum cohort, with
  small-cohort suppression — **never** raw latency percentiles that could re-identify a small provider cohort, and
  never any per-tenant figure. Internal-only: all latency stages, correlation completeness, dead-letter detail. This
  ADR documents the boundary; it activates nothing.

## Consequences

- Operators get a truthful Wayond performance/quality view where genuine evidence exists, and an explicit, reasoned
  `N/A` everywhere it does not — with broker-fill latency kept strictly separate. Nothing is armed: the endpoint and
  UI are DARK; no schedule/flag/production state changes.

## Risks and controls

- *Fabricated speed / false zero* → measured number only ≥ MIN_SAMPLE; rate is `None` (not 0.0) on a zero
  denominator; absent evidence is `UNMEASURABLE`.
- *Clock-domain skew* → domains are declared per stage; cross-domain stages are labelled; negative deltas are
  excluded fail-closed.
- *Broker-fill conflation* → structurally separated; asserted in tests.
- *Synthetic contamination* → genuine cohort = `non_genuine=False` with the documented intake caveat; downstream
  shadow/test exclusion documented.
- *Small-cohort re-identification* → no per-tenant/per-identity rows; publication thresholds documented, not
  activated.
- *Query amplification* → percentile stages pull only two timestamp columns, capped at `MAX_SAMPLE`; counts use
  group-by aggregates.

## Future instrumentation (deferred — would each justify its own additive migration + ADR)

Parse start/complete timestamps; a real-path order_send round-trip timer; an intended-vs-executed price pair for
slippage; `correlation_id` propagation onto `ExecutionJob`/`trading.Trade`; a DB listener heartbeat producer. None is
added here — the foundation measures existing evidence and reports the rest as `UNMEASURABLE`.

## Hardening (Phase-6 adversarial review)

The three-lens adversarial review (truthfulness / security-privacy / correctness) confirmed read-only,
value-blind, DARK and migration-free, with no CRITICAL/HIGH security finding. The following truthfulness
fixes were applied and regression-tested:

- **No lying scope label.** The `include_synthetic` toggle was removed — it only relabelled the cohort
  `all` while every measured body stayed genuine-only. The cohort is always `genuine` (non-genuine rows are
  reported separately under `messages_observed` / `dead_letter`), because fixture origin is not
  row-distinguishable so an honest combined cohort cannot be produced.
- **Provider scoping.** Every `AcquiredMessage`-based figure (and freshness and provider coverage) is scoped
  to the Wayond provider set (`_wayond_provider_ids`), so a second armed provider's data can never be
  published under the Wayond banner. Previously those queries were pipeline-wide.
- **No false zero.** `_rate` keeps a tiny positive rate from rounding to `0.0` (a rare-but-real
  stale/quarantine/miss in a large denominator stays non-zero); a genuine measured zero stays `0.0`; a zero
  denominator stays `None`.
- **Skew-biased cross-domain estimates downgraded.** When the negative-skew exclusion fraction exceeds
  `MAX_SKEW_FRACTION` (0.25) a would-be MEASURED stage becomes PARTIAL (`high_skew_exclusion`), so a
  survivor-biased cross-domain percentile is never presented as clean.
- **Sample-cap disclosure.** A stage computed over the newest `MAX_SAMPLE` rows carries `capped: true`.
- **Reason-code allow-list.** `hold_reason` (an unconstrained CharField) is normalised to an allow-listed
  categorical code before emission (unknown → `other`; a `code:detail` suffix is stripped), so value-blindness
  does not depend on every writer's discipline.

## Evidence / validation

`reliability/tests_wayond_metrics.py` (percentile correctness; measured/partial/N-A; skew & missing exclusion;
clock-domain labelling; synthetic exclusion; outcome/stale/dead-letter; approval rate; broker-fill separation;
value-blindness; staff auth + DARK 404). Migration-free (`makemigrations --check` introduces nothing for the changed
apps; the pre-existing `soaksnapshot`/`providercommand` index-rename auto-naming drift is unrelated and not touched).

## Reversal path

Repository-only and migration-free: revert the PR. Nothing is enabled in production; the DARK flag simply never turns
on.
