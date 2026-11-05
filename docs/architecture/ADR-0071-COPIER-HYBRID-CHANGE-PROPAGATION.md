# ADR-0071 — Copier hybrid change detection & propagation (per-master-ticket lifecycle)

- Status: **Accepted (DARK / repository-only)** — awaiting governed review (PR nunoamaral-hue/GuvFX#605; reconciled onto main d19e80e0)
- Packet: GFX-COPIER-HYBRID-CHANGE-001
- Date: 2026-11-03
- Supersedes: nothing. **Additive** on top of ADR-0051 (observation/ordering), ADR-0052 (close +
  follower-ticket reconcile), ADR-0053 (SL/TP modify), ADR-0055/0056 (partial-close crash-safety),
  ADR-0057 (LIVE exact-pair), ADR-0049/0049.1 (bridge provisioning).

## Context

The copier already **detects** every master change (the pure `diff` engine emits OPEN, FULL_CLOSE,
PARTIAL_CLOSE, VOLUME_INCREASE and SL/TP set·change·remove as monotone-`sequence` `MasterEvent` rows) and
already has DEMO/SHADOW **producers** for OPEN, close, SL/TP modify and partial-close, each position-keyed to a
*verified* follower ticket, idempotent, crash-safe and fail-closed. What it lacked was the connective tissue
the Sponsor's product decisions require:

1. A master position may open **without** SL/TP and must still be copied promptly.
2. Later changes (add/change/remove SL·TP, partial close, full early close) must be detected and propagated.
3. A **hybrid** design: a fast event path plus a rapid reconciliation poll as the correctness backstop.
4. A **delayed entry is unacceptable** — only positions linked during their original admissible entry window
   may receive later modify/close, and a failed initial copy is **never** recovered by a later trade.
5. **Close has priority** over SL/TP modifications.
6. Execution remains fail-closed and separately authorised.

None of this existed as a first-class model: lifecycle was implicit, scattered across per-event
`CopierReplication` rows and `reconciliation_status`; there was no entry-admissibility deadline that is
*terminal*, no immutable per-ticket link, no ordering contract, no event-path revision, and no bounded rapid
cadence with latency telemetry.

## Decision

Add a **DARK, additive, non-executing lifecycle layer** on top of the existing ledger. Three pure modules and
one gated projection, behind a new flag `COPIER_LIFECYCLE_ENABLED` (default OFF, ANDed with `COPIER_ENABLED`).
It **records decisions and lifecycle state only** — it creates **no** `ExecutionJob` and contacts **no**
broker. The execute/arm gates (`COPIER_EXECUTE_ENABLED` / `COPIER_EXECUTE_DEMO_ARMED` /
`COPIER_LIVE_EXECUTE_ENABLED`) remain the sole, independent authority for any order.

### State machine (`trade_copier/lifecycle.py`, pure)

Per `(follower, master, master_position_ticket)`:

```
OBSERVED_OPEN  →  ENTRY_PENDING  →  LINKED  ⇄  MODIFY_PENDING
      │                │             │                 │
      │                │             └→ CLOSE_PENDING → CLOSED
      ▼                ▼             (close supersedes a queued modify)
 ENTRY_FAILED_FINAL ◄──┘        CONTAINED_FAILURE (divergence / ambiguity)
```

- **OBSERVED_OPEN** — a master OPEN was detected **and is admissible**; shadow intent only, no follower
  attempt (the DARK resting state). An OPEN **with or without** SL/TP is admitted here (product decision 1/2).
- **ENTRY_PENDING** — an armed follower entry was attempted; awaiting the confirmed link.
- **LINKED** — the entry is confirmed and bound to an **immutable** follower ticket (set once, never
  rewritten). Only a LINKED position receives a modify/close.
- **MODIFY_PENDING** / **CLOSE_PENDING** — a queued SL/TP modify / close. A **close supersedes a queued
  modify**, and a modify arriving after a close is dropped (`superseded_by_close`).
- **ENTRY_FAILED_FINAL** (terminal) — the entry was inadmissible (detected/attempted/linked past the
  deadline) or its attempt failed. **Never re-entered**; later snapshots are absorbed (no late recovery).
- **CLOSED** (terminal) — a full close was confirmed on the follower.
- **CONTAINED_FAILURE** (terminal) — a divergence/ambiguity (reconcile AMBIGUOUS/DIVERGENT, a close before a
  confirmed link, a rejected close with residual exposure). Never auto-retried; surfaced for an operator.

Guarantees (certified by `tests_lifecycle.py`): terminal states absorb all later events; stale/out-of-order/
duplicate events (sequence `<=` the per-ticket high-water mark) are rejected; a delayed entry is never placed;
modify/close only from LINKED; the engine is deterministic (replay-safe).

### Admissibility deadline (bounded, configurable)

`LifecyclePolicy(entry_admissibility_seconds=120, max_modification_age_seconds=120, max_event_age_seconds=300)`
— conservative defaults, never hard-coded at a call site. `entry_admissibility_seconds` bounds the **whole**
observe→place→link path: an OPEN detected later, an attempt later, or a link confirmed later than the window
is `ENTRY_FAILED_FINAL`. Closes are intentionally **never** aged out (a close must always propagate and
outranks any modify). SL/TP modifies older than `max_modification_age_seconds` are dropped (never a late
entry).

### Hybrid detection + monotonic revision (`trade_copier/revisions.py`, pure)

Two ordering keys, each monotone **within its own source**, never compared across sources:

- **POLL path** — the existing `MasterEvent.sequence` (authoritative; the lifecycle's ordering).
- **EVENT path** — a per-`(account, ticket)` `revision` carried by each bridge-emitted `PositionRevision`.
  `fold_revisions` reduces an unordered emission stream to a current open-position set (highest valid revision
  per ticket wins; a replay/older emission is rejected), which the **existing** `diff` engine then turns into
  canonical `MasterEvent` rows. The event path is therefore a *faster producer of poll-equivalent snapshots*,
  not a second event namespace; the poll remains able to correct it.

`broker_event_time` (EVENT path only) is the single honest detection-latency reference; the POLL path exposes
no per-change broker timestamp and does not invent one.

### Reconciliation cadence + latency (`trade_copier/cadence.py`, pure)

`ReconciliationCadence` is a **bounded** `[min, max]` band with a base interval, jitter and exponential
backoff. `plan_next_delay` keeps every value inside the band; consecutive failures only **lengthen** the
interval (safe degradation, no request storm); jitter de-correlates co-resident tenants. **Overlap is not
re-implemented** — it is the observer's existing single-flight observation lease. **Sub-second cadence is
refused without a REAL measured `CapacityProfile`** (no-assumption rule): the default floor is 1000 ms, and a
sub-second floor is admissible only against a measurement that shows ≥2× read-latency headroom.

### Durable projection (`CopierPositionLifecycle` + append-only `CopierLifecycleTransition`, migration 0009)

`run_copier_lifecycle` (and `run_lifecycle_pass`) folds the authoritative `MasterEvent` stream + the
reconcile-derived link facts through the pure engine and materialises the lifecycle row + an append-only
transition audit. Idempotent via the per-ticket `applied_sequence` high-water mark. READ-ONLY; no job, no
broker.

## Adversarial review (Phase 4)

| Vector | Defence |
|---|---|
| Duplicate / late entries | `stale_sequence` + `duplicate_open` guards; `entry_deadline_exceeded`/`attempt_after_deadline`/`linked_after_deadline` → terminal |
| Resurrection after failure | terminal states absorb every later event (`terminal_absorbed`), tested |
| Cross-tenant ticket collision | lifecycle keyed + uniquely constrained on `(follower, master, ticket)`; projection is per-follower (tested, multi-tenant) |
| Stale update overwriting a newer stop | forward-only read (`sequence__gt=hwm`) + `stale_sequence` reject |
| Close / update races | close-family processed-then-supersedes; a post-close modify is dropped (`superseded_by_close`) |
| Partial-close arithmetic | **deferred** to the existing crash-safe ADR-0055/0056 partial-close path; the lifecycle tracks `CLOSE_PENDING` only |
| SL/TP zero-vs-absent | same `diff._to_level`/`_has_level` rule on both paths; `modify_weakens` mirrors the producer's fail-closed-on-weaken |
| Restart / replay | projection idempotent via `applied_sequence` HWM (tested) |
| Degraded bridge / polling storm | cadence backoff lengthens-and-caps; overlap via the observer lease |
| Failure-alert suppression | failure reason + append-only transitions recorded on every ENTRY_FAILED_FINAL / CONTAINED_FAILURE |
| Execution reachability while OFF | projection creates **no** `ExecutionJob` (asserted in test) and imports no order path |

Known, accepted bound: a *concurrent* first-creation of the same lifecycle row can raise `IntegrityError`
(the unique constraint); the per-ticket try/except contains it and the next pass completes it. Acceptable for
the single-master DARK pilot; the passes run single-threaded under cron.

## What is NOT in this packet (next governed gates)

> **Update (ADR-0074, DARK):** the signed ingestion endpoint + bridge-contract persistence, the lifecycle→
> producer SHADOW wiring, governed terminal-failure alerting, and synthetic capacity measurement below are now
> implemented additively and DARK by **ADR-0074** (GFX-COPIER-HYBRID-COMPLETION-003). Still deferred as its own
> gate: unifying the EVENT path into the authoritative `MasterEvent` sequence (it mutates shared ordering).


- **No bridge emitter and no ingestion endpoint** for the EVENT path. The contract and the pure fold exist;
  wiring a *signed, account-scoped* ingestion endpoint + a bridge emitter touches the API/host surface and
  requires its own decision + ADR. Until then the lifecycle is fed by the authoritative poll path only.
- **No wiring to execution.** The lifecycle records the admissible decision; it does not drive the producers.
- **No alert delivery.** Failure/latency *signals* are recorded; routing them to Telegram/ops is a follow-up.
- **No measured capacity profile.** Sub-second cadence stays refused until a real measurement is recorded.

## Consequences

The copier now has an explicit, testable per-ticket lifecycle with a terminal no-late-recovery guarantee, a
close-over-modify ordering contract, a hybrid-ready detection contract with monotonic-revision stale
rejection, and a bounded rapid-cadence/latency model — all DARK, execution OFF, additive, behaviour-preserving
for every existing path.
