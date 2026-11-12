# ADR-0094 — Copy Trading hybrid REAL-TIME replication (immediate push primary; slow observer reconciliation-only)

- **Status:** Accepted (repository-only / DARK, default-OFF). Deployment, flag enablement, the account-scoped
  read-only follower-positions enablement, synthetic host acceptance, and any Sponsor-present live demo are
  **separate Sponsor gates**. No deploy, no host contact, no flag enablement, no broker order is part of this
  packet.
- **Packet:** GFX-COPIER-REALTIME-HYBRID-REPLICATION-067.
- **Date:** 2026-11-12.
- **ADR number:** 0094 — the next genuinely free number (0092 is the highest merged; 0093 is claimed by the
  open broker_intelligence PR #641 and deliberately left to it).
- **Builds on / realises:** ADR-0074 (the signed fast-path ingestion endpoint, `CopierIngestedRevision`, and the
  `project_master_events` promotion onto the ONE authoritative `MasterEvent` ledger + shared baseline),
  ADR-0057 (the LIVE OPEN producer + per-pair admission), ADR-0083 (LIVE MODIFY/CLOSE lifecycle),
  ADR-0086 (the MODIFY/CLOSE mutation dispatcher), ADR-0072 (the `MT5_LIVE_READONLY_POSITIONS_ENABLED` read
  gate). Nothing in those ADRs is modified; this ADR only adds a new *trigger* and a route-scoped read binding.

## Context — the gap this closes (measured)

The 2026-11-12 Sponsor-present demo proved the copier observes and reconciles correctly but **not** in real
time: the slow observation/reconciliation path detected the master OPEN (ticket …6486) roughly **84 seconds**
after the MT5 open (poll at 07:22:05Z saw the account flat; the next poll at 07:24:04Z saw the position). Even a
few seconds materially changes a follower's entry price, so the ~2-minute observer cadence is unacceptable as
the **primary** copy path.

The ADR-0074 fast path was built as isolated, DARK pieces and left **unwired**: the signed ingestion endpoint
(`SignedEventIngestView.post`) accepts a signed emission, commits an immutable `CopierIngestedRevision`,
returns `200`, and **triggers nothing**. Promotion to `MasterEvent` (`run_event_projection_pass`) and the whole
replication→execute→dispatch chain are **separate, on-demand/scheduled management commands**. So an ingested
master event sits as inert evidence until the next scheduled worker runs.

## Decision — a synchronous, gated, fail-safe real-time trigger

A new DARK flag `COPIER_IMMEDIATE_DISPATCH_ENABLED` (`copier_immediate_dispatch_enabled`, ANDed with
`copier_enabled`) turns the signed ingestion endpoint into the **primary low-latency trigger**. When armed and
a batch is accepted with `accepted > 0` and `identity_ok`, the view calls
`trade_copier.immediate_dispatch.dispatch_master_realtime(master_pk, source="EVENT_PUSH", …)`, which runs the
**existing governed passes in-line for just that master**:

1. `project_master_events` (under the SAME single-flight observation lease the poll observer uses) — promote the
   ingested revision(s) to authoritative `MasterEvent` rows on the shared `last_poll` baseline;
2. `replicate_pending` — one `CopierReplication` intent per new event × follower;
3. `produce_pending` — the governed LIVE `PLACE_ORDER` (OPEN) producer; `admit_open_live` + the one-order
   allowance are re-checked under `select_for_update`;
4. `run_lifecycle_pass` → `produce_pending_modifies` / `produce_pending_closes` — the per-op MODIFY/CLOSE
   producers (per-op arm + authority re-checked under lock);
5. `run_mutation_dispatch_pass` — one-shot MODIFY/CLOSE send to the bound follower bridge;
6. a **bounded immediate reconcile** (a few fast attempts, NOT the ~2-min schedule) to link the OPEN
   `follower_ticket` so MODIFY/CLOSE can target it.

**This changes only WHEN the governed passes run (now, on the event), never WHAT they may do.** Every sub-pass
re-reads its own gate (`copier_event_projection_enabled` / `copier_replication_enabled` / `copier_execute_enabled`
/ `copier_live_execute_enabled` + the per-op arms / `copier_mutation_dispatch_enabled` /
`copier_reconcile_enabled`) and the full per-pair admission (`admit_open_live` / the ADR-0083 lifecycle
preamble) exactly as the scheduled workers do. The orchestrator grants NO authority and ORs past nothing.

### Hybrid operating model

- **PRIMARY — immediate push.** The signed emission drives the follower order synchronously (target ≤1s,
  ceiling ≤2s source→submission).
- **FALLBACK — fast bounded poll.** The host emitter (ADR-0077, a fast poll detector) delivers through the SAME
  signed ingestion endpoint when an immediate event is unavailable; the backend path is identical.
- **RECONCILIATION — slow observer.** The ~2-min poll observer stays a RECOVERY / audit / restart-safety /
  missed-event path only. Once the immediate path is armed it is **never** the normal OPEN/MODIFY/CLOSE trigger.

### Dedup contract (no trade copied twice)

Push and poll converge on **one** `MasterEvent` ledger and **one** `CopierMaster.last_poll` baseline. The
projection advances that baseline under the shared single-flight observation lease, so a later poll of the same
ticket diffs against the event-advanced baseline and emits no second event (and vice-versa). The OPEN producer
consumes the single authorization allowance under `select_for_update`, so a retry/replay can never place a
second order. Idempotency: `CopierMaster.last_projected_ingest_id` + the per-(master, ticket) monotone revision
contract reject a duplicate/stale/lower-revision emission in the fold. A restart replays nothing (an accepted
event is already promoted/consumed; the allowance is durable).

### Ordering contract

Per ticket, `OPEN.revision < MODIFY.revision < CLOSE.revision` is enforced by the monotone revision fold and the
ADR-0071 lifecycle state machine (a MODIFY/CLOSE acts only on an already-LINKED follower ticket; an out-of-order
or pre-OPEN mutation is deferred/rejected, never applied to an unknown ticket). A CLOSE after an UNKNOWN OPEN
outcome fails closed (the mutation dispatcher leaves an UNKNOWN job RUNNING for the authoritative reconciler and
never infers success); CLOSE remains possible after a successful OPEN even if the optional MODIFY is skipped.

### Latency evidence (`CopierRealtimeDispatch`)

One row per new master event records the honest timestamp chain — `event_detected_at` (broker event/open time),
`emitted_at` (emitter observation), `backend_received_at` (ingestion), `accepted_at` (ledger promotion),
`dispatch_started_at`, `follower_submitted_at` (job created / command sent), and `broker_acknowledged_at` /
`completed_at` when known. **GuvFX source→submission excludes broker ack/fill**, measured separately. A missing
timestamp is `NULL` (never a fabricated zero) and fails the acceptance assertion. **Acceptance:** healthy
synthetic OPEN/MODIFY/CLOSE source→submission ≤ **2000 ms** (`is_late` otherwise), target ≤ **1000 ms**;
reconciliation-only recovery is reported separately and never counts as real-time success.

### Governed-close prerequisite — route-scoped read-only follower positions

Reconciliation (the only linker of an OPEN's `follower_ticket`) reads the follower's live positions, which a
LIVE bridge refuses (`account_not_demo`) unless rendered with `MT5_LIVE_READONLY_POSITIONS_ENABLED`. ADR-0094
adds a **route-scoped** path in `execution.bridge_config.readonly_positions_for_account`: the read flag is
rendered when the account is LIVE AND a new default-OFF authority `COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED`
is armed AND the account is the **follower of a live, non-revoked, unexpired `CopierLiveAuthorization`** (ACTIVE
or CONSUMED). It is a READ gate only — it can never place, size, modify, close or approve an order, is **not** a
global relaxation (no authorization, or any other account, ⇒ False), and auto-expires with the authorization.
`follower_positions_read_readiness` reports `FOLLOWER_POSITIONS_READ_OK | NOT_AUTHORIZED | UNAVAILABLE` for the
preflight. **Production enablement of that flag (and any backend recreation) is a separate Sponsor-gated deploy
packet.**

## Consequences

- With `COPIER_IMMEDIATE_DISPATCH_ENABLED` OFF (the default), the ingestion endpoint is **byte-identical** to
  ADR-0074 (persist-only) and `CopierRealtimeDispatch` stays empty — nothing new runs.
- Live follower execution remains **human-gated**: the immediate path still requires the full arm stack + a
  human-created `CopierLiveAuthorization` + the signed host admission; this ADR never lowers that bar.
- The synchronous call is **fail-safe**: any fault is contained + logged and reported as `CONTAINED`; it never
  breaks the ingestion HTTP response or the immutable evidence already committed, and the slow reconciler
  remains the safety net. No ambiguous financial mutation is retried.

## Known limitations (documented; reviewed — not safety gaps)

- The producer passes the orchestrator invokes (`replicate_pending` / `produce_pending` / `run_lifecycle_pass` /
  `produce_pending_modifies` / `produce_pending_closes` / `run_mutation_dispatch_pass`) are the EXISTING
  governed, idempotent, single-flight **global** sweeps bounded by `limit=50`. The triggering master's new event
  is processed immediately, but strict per-master isolation of those sweeps under multi-master backlog is not
  provided — each produced job is still independently gated + idempotent, so this is a scoping limitation, never
  a wrong/duplicate order. Per-master-scoped producers are a follow-up.
- **Follower-ticket linkage is NOT on the synchronous request path.** A just-minted `PLACE_ORDER` is still
  PENDING, so an inline bridge read could not link it, and blocking broker I/O in the ingestion handler is an
  availability risk (adversarial review HIGH-A). Linkage happens through `reconcile` **after** the bridge fills
  the OPEN; a fast, post-fill, event-driven reconcile (so MODIFY/CLOSE readiness is also sub-second) is a
  follow-up. The OPEN **submission** latency (≤2s) is unaffected — submission is the governed job mint, before
  any reconcile.
- The orchestrator drives OPEN/MODIFY/FULL-CLOSE; `PARTIAL_CLOSE` keeps its dedicated worker path and is not
  driven on the fast path (an exposure-reducing op — a latency gap, not a safety gap).

## Separately gated steps (NOT in this packet)

1. Merge. 2. DARK deployment. 3. Account-scoped read-only-positions enablement (+ backend recreate).
4. Synthetic host acceptance. 5. Sponsor-present bounded live demo.
