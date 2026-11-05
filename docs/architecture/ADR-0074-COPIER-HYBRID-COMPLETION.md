# ADR-0074 — Copier hybrid completion: signed event ingestion, SHADOW dispatch, governed alerting

- Status: **Accepted (DARK / repository-only)** — awaiting governed review (GFX-COPIER-HYBRID-COMPLETION-003; base main `2f859cac`)
- Packet: GFX-COPIER-HYBRID-COMPLETION-003
- Date: 2026-11-05
- Builds on: **ADR-0071** (per-master-ticket lifecycle state machine, hybrid detection contract, bounded
  cadence) — additive, nothing in ADR-0071 is replaced. Also ADR-0051/0052/0053/0055/0056 (observer, close,
  SL/TP modify, partial close), ADR-0057 (LIVE exact-pair), and mirrors the signing idiom of
  `hosted_workspace.host_protocol` (ADR-0049 family).

## Context

ADR-0071 shipped the DARK lifecycle *foundation*: a pure per-`(follower, master, ticket)` state machine with
no-late-recovery admissibility, close-supersedes-modify ordering, stale/out-of-order rejection, a hybrid
detection **contract** (`revisions.fold_revisions`) and a bounded reconciliation cadence model. It explicitly
left five gaps as separately-gated next steps. This packet completes them, all still DARK and execution-OFF:

1. a signed, account-scoped **event-ingestion endpoint** (the fast path had a contract but no wire),
2. normalized position-state + explicit **change classification**,
3. **lifecycle → producer-boundary wiring** (without activating execution),
4. durable terminal **failure states + governed alert delivery**,
5. a **measured** capacity profile before any sub-second production cadence.

## Decision

Add the additive, DARK capabilities on top of ADR-0071 that complete the hybrid lifecycle end to end, each
behind its own default-OFF flag (all ANDed with `COPIER_ENABLED`), none on the execution path: (1) signed
fast-path ingestion, (2) normalized state + change classification, (3) lifecycle→producer SHADOW dispatch
(incl. a ledger-only **modeled** partial close), (4) governed terminal-failure alerting **and a default-OFF
delivery outbox**, (5) the repository-side **bridge emitter**, (6) the authoritative **event→`MasterEvent`
projection** (resolving the formerly-deferred integration), and (7) bounded **nonce retention**. Sections 1–4
below describe the first increment; sections 5–7 the completion increment (Sponsor hold response, same packet).

### 1. Signed fast-path event ingestion (`trade_copier.event_ingestion` + `event_ingestion_api`)

A fail-closed DRF endpoint `POST /api/copy-trading/event-ingest/` (gate `COPIER_EVENT_INGESTION_ENABLED`,
default OFF → 503). It mirrors the audited `host_protocol` signing idiom:

- **HMAC-SHA256 over a canonical signed envelope** that binds `account_id`, `operation`, `timestamp`,
  `expiry`, `nonce`, `key_id`, `correlation_id` and a `payload_digest`; `hmac.compare_digest`.
- **Bounded skew (30 s) + expiry window (≤ 600 s)**; **single-use nonce burn** (`CopierIngestNonce`, unique
  insert) checked only *after* the signature verifies, so an unauthenticated request can never consume a nonce.
- **Payload integrity** via `payload_digest` recomputation; **explicit 256 KiB body cap**; dedicated
  per-identity throttle scope `copier_event_ingest`.
- Its OWN dedicated secret via `core.credentials.resolve_secret("COPIER_INGEST_HMAC_KEY", …, min_length=32)` —
  **never** the conflated `GUVFX_AGENT_TOKEN` (security.md RULE 3 / 6). Secrets, signatures and full payloads
  are never logged.
- **Immutable account scope**: the signed `account_id` must resolve to a `CopierMaster`, every revision must
  claim that same account (else `account_scope_violation`), and any broker login/server the emission carries
  is cross-checked against `execution.snapshot_transport.expected_identity` (fail closed on mismatch).
- It **persists observations only** — immutable `CopierIngestedRevision` rows, idempotent via the monotone
  `(master, ticket, revision)` unique (a re-sent emission → 200 `duplicate`). It writes **no** authoritative
  `MasterEvent`, creates **no** `ExecutionJob`, and contacts **no** broker.

Response contract uses sanitised reason codes only: 200 accepted/rejected(permanent)/ , 400 malformed,
401 signature/identity failure, 409 replay, 413 oversize, 503 disabled/key-unset/transient.

### 2. Normalized state + change classification (`revisions.PositionRevision` fields, `change_types`)

`PositionRevision` carries the full normalized state (entry price, open time, original vs current volume,
explicit SL/TP absence, close reason, event id, payload digest, broker identity) — all optional, so the pure
fold and the ADR-0071 tests are unchanged. `change_types.classify_position_changes` names each change
(`ENTRY_OPENED`, `STOP_LOSS_ADDED/CHANGED/REMOVED`, `TAKE_PROFIT_*`, `SL_TP_CHANGED_TOGETHER`,
`VOLUME_REDUCED`/`PARTIAL_CLOSE`, `POSITION_CLOSED`) by reusing the authoritative pure `diff` engine — so an
event-sourced snapshot classifies identically to a poll-sourced one, and floating price, floating P&L,
metadata ordering and formatting-only decimal differences never produce a change (they are never diffed).

### 3. Lifecycle → producer SHADOW dispatch (`trade_copier.lifecycle_dispatch`)

A gated READ-ONLY pass (`COPIER_LIFECYCLE_DISPATCH_ENABLED`, default OFF) that, for each actionable lifecycle
decision (OBSERVED_OPEN admissible / MODIFY_PENDING / CLOSE_PENDING), **re-checks the existing producer
admission** (`execution_producer.admit_open/admit_close/admit_modify`, which create nothing and read no broker)
and records a **synthetic, append-only `CopierLifecycleDispatch`** row with the op, the producer's deterministic
idempotency key (`copier:` / `copier-close:` / `copier-modify:` / `copier-pclose:`), the admission verdict and
`would_execute`. It **never calls `produce_*`** — the OPEN producer mints a `PLACE_ORDER_SHADOW` ExecutionJob
even unarmed, so calling it is forbidden here — therefore it creates **no ExecutionJob**. `admit_partial_close`
(which performs a live follower read) is deliberately **deferred** so the DARK pass performs no broker contact.
LINKED (no pending op) is not dispatched. It is separate from — and never ORs past — the execute/arm gates.

### 4. Governed terminal-failure alerting (`trade_copier.failure_alerts`)

An alert-category taxonomy (`ENTRY_FAILED_FINAL`, `MODIFY_FAILED_FINAL`, `CLOSE_FAILED_FINAL`,
`CONTAINED_FAILURE`, `IDENTITY_MISMATCH`, `MAPPING_UNAVAILABLE`, `EVENT_REJECTED`,
`RECONCILIATION_DIVERGENCE`) — **alert categories, not new lifecycle states** (the 8-state machine invariant is
unchanged). Routed through the EXISTING governed `reliability.AlertEvent` channel using the repo's
dedup-guarded create idiom (namespaced `dedup_key`, OPEN→ACK→RESOLVED, secret-free title/body/detail,
retryable-vs-terminal flag), surfaced on the read-only operations summary (new `_copier_block`) and the alerts
API. A new `COPY_TRADING` reliability component is added (choices-only migration; **not** critical, **not**
terminal-scoped — it can never gate `can_trade`). Gated by `COPIER_ALERTS_ENABLED` (default OFF): when OFF the
failure is still durably recorded as append-only `CopierLifecycleTransition` evidence; the flag only governs
whether an operator `AlertEvent` is ALSO raised. No new Telegram/email path is created and nothing is pushed
outward; alerting never recovers a trade.

### No-late-recovery evidence (Phase 6)

The admissibility deadline, terminal `ENTRY_FAILED_FINAL`, and terminal absorption of later events were
already enforced by ADR-0071. This packet adds, at the projection layer only, an append-only **evidence**
transition (`post_terminal_<category>_ignored`) when a terminal lifecycle absorbs a later material event — so a
post-failure SL/TP/close change is durably recorded as non-executable evidence with a clear reason, without any
state change and without creating an entry. The pure-engine semantics are unchanged.

### Capacity (Phase 10)

The cadence default floor stays 1000 ms; sub-second remains refused without a real measured `CapacityProfile`
(ADR-0071). Synthetic latency/throughput/query-count measurements are recorded as evidence; production cadence
is NOT changed and no sub-second loop is enabled.

## 5. Bridge emitter (`trade_copier.bridge_emitter`) — the fast path's producer

The repository-side, host bridge EMITTER that the ingestion endpoint was missing a counterpart for. It OBSERVES
a master's normalised open-position state (a snapshot handed in by the host — it never attaches, orders,
modifies or closes), diffs it with the EXISTING pure `diff`, assigns a **monotonic per-ticket revision**, and
emits SIGNED account-scoped envelopes (the SAME pure signing construction as the verifier — an emitted envelope
verifies there byte-for-byte). It preserves explicit missing SL/TP (`None` == no level), covers open / modify /
partial close / full close (one revision per changed ticket carrying the complete current state), and uses a
**bounded, durable spool with bounded retry → dead-letter** and at-least-once delivery (the backend dedups by
the unique `(master, ticket, revision)` + the single-use nonce). A revision's `broker_event_time` is stamped
ONCE and is IMMUTABLE across retries: a retry re-signs only the transport envelope (fresh nonce + timestamp so
the endpoint's skew/expiry pass) and NEVER rewrites the true change time — so a late re-send can never extend an
entry deadline. The HMAC key is supplied by an injected provider (a synthetic key in tests; the host's own
fail-closed env resolver in production — NEVER `GUVFX_AGENT_TOKEN`); the secret, signature and full payload are
never logged. It is **default OFF** and **NOT installed on Windows** in this packet.

## 6. Authoritative event→`MasterEvent` wiring (`trade_copier.event_projection`) — the deferred integration, resolved

The previously-deferred unification is **completed** here, under the Sponsor's explicit, documented instruction
(this is the Amber architecture decision). There is ONE authoritative object and no competing event truth:

- **Authoritative object = `MasterEvent`** (per-master monotone `sequence`), which the lifecycle reads
  unchanged. The EVENT path does not keep a parallel state; it is a *faster producer of the same `MasterEvent`
  rows*.
- A gated (`COPIER_EVENT_PROJECTION_ENABLED`, default OFF), on-demand projection folds a master's NEW
  `CopierIngestedRevision` rows, **OVERLAYS** the fold winners onto the shared poll baseline
  (`CopierMaster.last_poll`) — closed → removed, open → inserted/updated, untouched baseline tickets carried
  verbatim — runs the EXISTING `diff_positions`, and appends `MasterEvent` rows (`source='EVENT'`, carrying the
  true `broker_event_time`) under the SAME per-master row lock + monotone sequence the observer uses, advancing
  the SAME `last_poll` baseline. Poll and event therefore share one ledger, one sequence space and one baseline
  and dedup automatically.
- **Why an overlay, not a bare fold-snapshot diff:** the event stream is a PARTIAL view (only the tickets the
  bridge emitted about); diffing it directly against the baseline would falsely FULL_CLOSE every untouched
  baseline ticket. The overlay emits events ONLY for event-touched tickets and keeps the resulting baseline as
  complete as the poll baseline it extends — a genuinely missed close is still corrected by the next poll.
- **No late OPEN / reconciliation uses the same machine:** each promoted event carries the emission's true
  `broker_event_time`; the lifecycle (`_classify` now threads it) measures the admissibility deadline from it,
  so a stale/buffered OPEN is ruled `ENTRY_FAILED_FINAL` — never entered — exactly as a late poll-sourced OPEN
  would be. Idempotent via `CopierMaster.last_projected_ingest_id`; out-of-order/duplicate emissions are
  rejected by the fold's monotone per-ticket high-water marks (no regression). Creates NO `ExecutionJob`,
  contacts NO broker.

The honest capability marker reads `..._MODELED`: the full detection→lifecycle→SHADOW-dispatch chain is
implemented and proven, with **execution OFF** (no `ExecutionJob`, no broker) — "MODELED" denotes SHADOW, not
absent propagation.

## 7. Governed alert DELIVERY outbox (`trade_copier.alert_delivery`) — completes Phase 9

Raising a copier alert now ALSO enqueues ONE durable, idempotent `CopierAlertDelivery` outbox row (dedup via the
alert's `dedup_key`), when `COPIER_ALERTS_ENABLED` is on. A SEPARATE, default-OFF dispatcher
(`COPIER_ALERT_DISPATCH_ENABLED`) atomically claims PENDING/FAILED rows, sends through an INJECTED transport that
DEFAULTS to an inert dry-run with **no real destination**, writes an immutable per-attempt
`CopierAlertDeliveryAttempt` audit, and advances the durable state + attempt counter (FAILED retried until
`max_attempts` → terminal DEAD). Mirrors the audited `execution.notifications` outbox/dispatcher/transport
pattern. When the dispatcher is OFF, deliveries sit PENDING — an operator-visible backlog surfaced (with
FAILED/DEAD counts) on the read-only operations summary. No new ad-hoc Telegram/email path, no real destination
configured, nothing recovers a trade.

## Partial close (`execution_producer.model_partial_close`) — completed, not deferred

The DARK partial-close SHADOW dispatch is **completed**: a ledger-only admission MODELS the proportional
reduction against the durable opened follower volume (the matched OPEN replication's `target_volume`) using the
SAME pure `_partial_reduction` + the SAME durable `BrokerInstrument` constraints as the live admission. It
performs NO live broker read and creates NO `ExecutionJob`; the lifecycle dispatch records the modeled
reduction/remaining with `requires_live_volume_at_arm`. The live follower read is **not** an architectural
blocker to the SHADOW dispatch — it is required only to derive the authoritative execution-time lot on the
gated/armed path (`admit_partial_close`), which is unchanged and still gated.

## Nonce retention (`event_ingestion.prune_ingest_nonces`) — completed

A bounded, unscheduled, dry-run-by-default pruning command (`prune_copier_ingest_nonces`) deletes ONLY
fully-expired `CopierIngestNonce` rows (`expires_at < now - margin`), never weakening in-window replay
protection; counts are secret-free. (This closes accepted gap *L2* below.)

## Flags (all default OFF, fail-closed, ANDed with COPIER_ENABLED)

`COPIER_EVENT_INGESTION_ENABLED`, `COPIER_EVENT_PROJECTION_ENABLED`, `COPIER_LIFECYCLE_DISPATCH_ENABLED`,
`COPIER_ALERTS_ENABLED`, `COPIER_ALERT_DISPATCH_ENABLED`. No new production schedule is added; the management
commands (`run_copier_event_projection`, `run_copier_lifecycle_dispatch`, `run_copier_failure_alerts`,
`run_copier_alert_dispatch`, `prune_copier_ingest_nonces`) are run on demand only. The observer/replication
crons are unchanged. Migration `trade_copier/0011` is additive (two nullable/defaulted `MasterEvent` columns,
one `CopierMaster` counter, the two delivery tables) — no column is dropped or altered destructively.

## Adversarial review

Tenant isolation (account-scope + identity cross-check), bridge impersonation (HMAC + dedicated key), replay
(nonce burn + monotone revision), duplicate delivery (idempotent persist), out-of-order (revision/sequence
gates), restart/replay (HWM + nonce), deadline extension and late-entry resurrection (terminal absorption +
admissibility), close terminality, mapping/identity confusion (fail-closed rejections → alerts),
execution-gate bypass (dispatch creates no ExecutionJob; execute/arm gates untouched), alert
suppression/flooding (dedup), capacity exhaustion (body cap + throttle + bounded cadence), secret leakage
(no secret/signature/payload in logs or alert detail). Covered by the Phase-12 tests.

An independent adversarial review (Phase 13) confirmed all four security-critical invariants hold (no order
path; signature unbypassable; account-scope isolation; flags fail closed) and found **no CRITICAL/HIGH**.

**Fixed before PR:**
- *M1 (MEDIUM)* — an out-of-range numeric magnitude (e.g. `volume=1e9`, `sl=1e14`) passed structural
  validation and would raise `DataError` on persist *after* the nonce was burned, so a retry got `409 replayed`
  and the batch was silently dropped. Fixed: `PositionRevision.structurally_valid` now magnitude-bounds volume
  and the optional price/volume fields, so an oversize emission is a clean per-row **rejection**, never a late
  `DataError`.
- *L1 (LOW)* — a signed-but-malformed field (over-length nonce / non-integer `account_id`) could yield an
  uncaught 500 outside the sanitised contract. Fixed: the nonce length is bounded in the verifier
  (`nonce_too_long` → 400) and `account_id` is parsed defensively in the view (`bad_account_id` → 400).

**Accepted lower-severity (recorded, not fixed in this packet):**
- *M1 residual (nonce-burn ordering)* — the nonce is burned inside `verify_ingest_request` before the persist,
  so a genuine *infrastructure* persist fault (not a data fault — those are now pre-rejected) could still make a
  re-sent request `409`. Accepted for DARK: the copier has no execution consequence, the authoritative poll
  path is the documented correctness backstop, and triggering it requires the signing key. A later hardening is
  to burn the nonce transactionally with the persist.
- *L2 (LOW)* — **RESOLVED in this packet.** `CopierIngestNonce` now has a bounded, unscheduled, dry-run-by-
  default pruning command (`prune_copier_ingest_nonces` / `event_ingestion.prune_ingest_nonces`) that deletes
  only fully-expired nonces and never weakens in-window replay protection.
- *L3 (informational)* — a single global `COPIER_INGEST_HMAC_KEY` (no per-account key). The signature binds the
  account scope so forgery is not possible; a key compromise could poison the *evidence store* for any configured
  master (no order path; broker-login cross-check mitigates). Per-account keying is a conscious future decision.

## Consequences

The copier now has a signed, account-scoped, replay-safe fast-path ingestion surface; an explicit named change
taxonomy; a provably-safe lifecycle→producer wiring that stays execution-OFF; governed, de-duplicated,
secret-free operator alerting for terminal failures; and recorded capacity measurements — all DARK, additive,
behaviour-preserving for every existing path, with execution still gated solely by the unchanged execute/arm
walls.
