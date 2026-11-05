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

Add four additive, DARK capabilities on top of ADR-0071, each behind its own default-OFF flag (all ANDed with
`COPIER_ENABLED`), none on the execution path.

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

## The one deliberately-deferred integration

The fast EVENT path is **persisted and classified as detection evidence** (`CopierIngestedRevision` +
`change_types`). **Unifying the event path into the authoritative `MasterEvent` sequence** — so a fast-path
change drives the lifecycle directly rather than being corroborating evidence beside the authoritative poll —
is **NOT** done in this packet: it mutates the shared authoritative ordering (sequence allocation + poll/event
dedup into the append-only ledger), which is an Amber architecture-boundary change requiring its own decision.
Until then the lifecycle's authoritative ordering remains the poll path (ADR-0071); the honest capability
marker says "MODELED", not "live fast propagation".

## Flags (all default OFF, fail-closed, ANDed with COPIER_ENABLED)

`COPIER_EVENT_INGESTION_ENABLED`, `COPIER_LIFECYCLE_DISPATCH_ENABLED`, `COPIER_ALERTS_ENABLED`. No new
production schedule is added; the two new management commands (`run_copier_lifecycle_dispatch`,
`run_copier_failure_alerts`) are run on demand only. The observer/replication crons are unchanged.

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
- *L2 (LOW)* — `CopierIngestNonce` has no pruning job (grows after arming). Irrelevant while DARK; a pruning
  command is a pre-arming follow-up (no new production schedule is added in this packet).
- *L3 (informational)* — a single global `COPIER_INGEST_HMAC_KEY` (no per-account key). The signature binds the
  account scope so forgery is not possible; a key compromise could poison the *evidence store* for any configured
  master (no order path; broker-login cross-check mitigates). Per-account keying is a conscious future decision.

## Consequences

The copier now has a signed, account-scoped, replay-safe fast-path ingestion surface; an explicit named change
taxonomy; a provably-safe lifecycle→producer wiring that stays execution-OFF; governed, de-duplicated,
secret-free operator alerting for terminal failures; and recorded capacity measurements — all DARK, additive,
behaviour-preserving for every existing path, with execution still gated solely by the unchanged execute/arm
walls.
