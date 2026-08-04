# ADR-0030 — Continuous Broker Health Engine (WP3)

- **Status:** Accepted (engineering), ships DARK
- **Date:** 2026-08-04
- **Programme:** Broker Connectivity Capability – Trusted-Beta Integration (WP3)
- **Builds on:** ADR-0027 (validation primitive), ADR-0028 (WP1A account lifecycle +
  `BrokerAccountValidationAttempt`), ADR-0029 (WP1B/WP2 execution gate)

## Context
WP1B's execution gate answers "may this account execute *right now*?" from the account's
`validation_status`. But a Trusted-Beta account's broker connectivity changes over time — a session
drops, a broker goes unreachable, a credential silently stops working, or an account is decommissioned.
There is no single, authoritative, *continuous* answer to "is this account's broker link healthy, and
if not, should its runtime pause / may it resume?". WP1B pause/resume needs exactly that source. WP3
builds it once, deterministically, so WP1B never re-derives health from scattered evidence.

## Decision
1. **One authoritative model — `reliability.BrokerAccountHealth`** (OneToOne per `TradingAccount`).
   It holds the current state, the reason code, the counters, the convergence-contract fields
   (`resume_eligible`, `state_version`, `updated_at`) and scheduler bookkeeping (`next_check_at`).
   It is the *single* place broker health lives.

2. **Six-state deterministic machine** (`reliability/broker_health.py`). Transitions are pure
   functions of `(current state, counters, outcome, thresholds, clock)` — the same evidence always
   yields the same state:

   | State | Meaning | Entered by | Leaves to |
   |-------|---------|-----------|-----------|
   | `UNKNOWN` | no evidence yet | initial | HEALTHY (first success), DEGRADED/DISCONNECTED (failure threshold) |
   | `HEALTHY` | eligible to execute | first success / recovery | DEGRADED, DISCONNECTED, STALE, TOMBSTONED |
   | `DEGRADED` | sustained auth/attention failures | `failure_threshold` × `NEEDS_ATTENTION` | HEALTHY (recovery) |
   | `DISCONNECTED` | sustained broker-unreachable failures | `failure_threshold` × `UNAVAILABLE` | HEALTHY (recovery) |
   | `STALE` | HEALTHY but no recent successful validation | time (`stale_timeout_s`) | HEALTHY (recovery) |
   | `TOMBSTONED` | account decommissioned (WP1A `disconnected_at`) | lifecycle | **terminal** |

   - **Evidence, not opinion.** The engine *consumes* existing `BrokerAccountValidationAttempt`
     history (ADR-0028) via a monotonic id watermark (`last_consumed_attempt_id`) — each attempt is
     folded exactly once; it never duplicates that history into its own attempt store.
   - **Fail-safe classification.** `HEALTHY→success`, `UNAVAILABLE→hard failure`,
     `NEEDS_ATTENTION`/anything unexpected→soft failure. An unknown status is never a success.
   - **Anti-flap latch.** Once adverse, further failures increment the counter but never flip
     `DEGRADED↔DISCONNECTED`; only a recovery to HEALTHY clears the latch. Recovery requires
     `success_threshold` consecutive successes (no single-success bounce).
   - **Net signalling.** A batch fold emits signals for the *net* old→new change, so a transient dip
     already resolved by the time attempts are processed does not raise a spurious alarm.
   - **`state_version`** increments by exactly one per real state change (monotonic; never decreases),
     so WP1B can consume contract changes idempotently.

3. **Convergence contract (consumed by WP1B/WP2).** `BrokerAccountHealth.contract()` returns
   `{account_id, state, eligible, pause_required, resume_eligible, reason_code, state_version,
   updated_at}`. `eligible == (state==HEALTHY)`; `pause_required == state ∈
   {DEGRADED,STALE,DISCONNECTED,TOMBSTONED}`; `resume_eligible` is set only on a recovery edge
   (paused→HEALTHY), so WP1B can distinguish "healthy, was paused → may resume" from "healthy from the
   start → nothing to resume".

4. **WP3 emits signals only — never acts.** It writes `BrokerAccountHealth`, deduplicated
   `AlertEvent` notifications, and `core.audit` events. It **never** pauses/resumes a runtime, places
   or checks an order, logs into a broker, or reads a credential. (Enforced by a source-level coupling
   test.) Consuming the contract to actually pause/resume is WP1B's job, in a later increment.

5. **Deduplicated notifications + audit.** Entering an adverse state opens a single `AlertEvent`
   (dedup_key `BROKER_HEALTH:{account}:{state}`; DISCONNECTED→CRITICAL, DEGRADED/STALE→WARN,
   TOMBSTONED→INFO); recovery resolves the open alert. Audit events —
   `BROKER_HEALTH_{VALIDATED,DEGRADED,DISCONNECTED,STALE_DETECTED,RECOVERED,TOMBSTONED,
   PAUSE_REQUIRED,RESUME_ELIGIBLE}` — carry only the secret-free contract.

6. **Scheduler is a *framework*, inert by default** (`reliability/broker_health_scheduler.py`).
   It provides deterministic cadence/backoff (`base·factor^failures`, clamped), hash-derived
   deterministic jitter (never a random source), a per-cycle quota, and single-flight due-selection
   (`select_for_update(skip_locked=True)`; claim-by-advancing-`next_check_at`). But `run_cycle`:
   - flag OFF → hard no-op (`disabled`, no DB writes);
   - flag ON, no injected `validator` → inert (`no_validator`, no DB writes).

   WP3 performs **no recurring live validation**: it never supplies a validator. Wiring a real
   validator is a separate, Sponsor-gated arming step. Tests inject mocks only.

7. **Feature flag `BROKER_CONNECTIVITY_HEALTH_ENABLED` (default OFF).** Every public entry point
   (`record_validation_outcome`, `sweep_stale`, `get_contract`, `run_cycle`) is a no-op when OFF —
   the whole capability ships DARK. The flag is read live (function, not module constant).

## Consequences
- Additive: one new model + two modules; no change to any existing table, execution path, or WP1B
  behaviour while the flag is OFF. Customer Zero and production are untouched.
- WP1B/WP2 gain a single, deterministic, idempotently-consumable health source. The pause/resume
  increment (ADR-0029 §"Pause / resume (deferred)") consumes `contract()` — it does not re-derive
  health.
- Per-account row-level locking + the id watermark make concurrent folds safe (no double-consume, no
  version clobber). The scheduler's `skip_locked` claim makes concurrent cycles single-flight.
- Reason codes are customer-safe and suitable for API/frontend surfacing (WP4).

## Not in scope (explicitly deferred)
- Actually pausing/resuming runtimes (WP1B).
- Wiring a live validator into the scheduler / any recurring live validation (separate, Sponsor-gated
  arming step).
- Frontend surfacing of health state (WP4); ops runbooks/monitoring (WP5).
