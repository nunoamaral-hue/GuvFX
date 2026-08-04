# ADR-0029 — Broker-Validation Execution Gate (WP1B/WP2)

- **Status:** Approved (execution-gating policy approved by the Sponsor 2026-08-04)
- **Date:** 2026-08-04
- **Programme:** Broker Connectivity Capability – Trusted-Beta Integration (WP1B/WP2)
- **Builds on:** ADR-0027 (validation primitive), ADR-0028 (WP1A account lifecycle + `validation_status`)

## Context
Trusted Beta must not let a customer's automation execute against an unvalidated broker account. The gate
must be authoritative (backend, never frontend-only), fail-closed, and inert until explicitly armed.

## Decision
1. **Central decision service — `execution/broker_gate.py`.** One deterministic, non-secret, side-effect-free
   `evaluate_execution_gate(account) -> GateDecision{allowed, reason_code}`; funnels call
   `require_execution_gate(...)` (raises `ExecutionGateRefused`, audited) or `evaluate_execution_gate(...)`.
   No condition logic is duplicated in views/tasks/workers.
2. **Flag `BROKER_CONNECTIVITY_EXECUTION_GATE` (default OFF).** OFF ⇒ the gate is **transparent**
   (`allowed=True`, reason `gate_disabled`), so existing production execution behaviour is unchanged.
3. **Fail-closed policy (flag ON).** Execution is allowed only when `validation_status = VALIDATED` **and**
   the account is eligible. It refuses — with a stable reason code — for: missing account
   (`account_missing`), inactive (`account_inactive`), disconnected/tombstoned (`account_disconnected`),
   missing/destroyed credential (`credential_missing`), and `validation_status` NEVER / CONNECTION_FAILED /
   TECHNICAL_ERROR / unknown (`not_validated_*` / `validation_state_unknown`). Ambiguous account selection is
   refused at the entry point (the gate takes exactly one resolved account).
4. **Authoritative entry points.** The gate is enforced at execution funnels, not the frontend.

## Entry-point inventory (COMPLETE — enforced at the model boundary)
The release invariant is enforced at the **single authoritative boundary — `ExecutionJob.save()`** (mirroring
the existing kill-switch): on INSERT of a `BROKER_GATE_BLOCKED_JOB_TYPES` job (OPEN_TRADE / PLACE_ORDER /
PLACE_TEST_ORDER) with the flag ON, `require_execution_gate(self.account)` refuses an ineligible account.
**No creation path can bypass it** — direct ORM create, services, promotion, schedulers,
`create_place_order_job`, PLACE_TEST_ORDER, retry/recovery, and any future site all pass through `save()`.

Classification of every `ExecutionJob.objects.create` site (verified by adversarial review):
- **Gated at the model boundary (all exposure-opening):** `services.create_open_trade_job` (OPEN_TRADE),
  `signal_promotion` (PLACE_ORDER/SHADOW), `signal_engine.create_place_order_job` (PLACE_ORDER),
  `run_h1/m5/h4_scheduler` direct PLACE_ORDER, `views.py` PLACE_TEST_ORDER.
- **Earlier-refusal + graceful handling wired here:** `create_open_trade_job` and
  `signal_promotion._validate` refuse *before* building the payload (their own audit trail); the h1/m5
  schedulers, the strategy deploy view, the admin job-retry endpoint and the dev `CreateOpenTradeJobView`
  now catch `ExecutionGateRefused` (skip / clean 503, alongside the kill switch); PLACE_TEST_ORDER
  pre-checks → 503.
- **Durable refusal audit.** The gate audits `EXECUTION_GATE_REFUSED` on refusal. Because the h1/m5
  schedulers wrap creation in `transaction.atomic()` and catch OUTSIDE it, the in-transaction audit would
  roll back — so those catch sites **re-emit a durable audit** (autocommit) to guarantee an armed refusal
  always leaves a record. Non-transactional funnels (services) audit at the gate call directly.
- **Out of scope (open no new exposure):** SYNC_POSITIONS, MODIFY_POSITION, CLOSE_TRADE, breakeven,
  PLACE_ORDER_SHADOW (dry-run).

**Remaining for the pause/resume increment (not creation bypasses):** re-evaluation at the final dispatch
boundary (TOCTOU / race); the h4 scheduler's graceful-skip parity (its refusal is currently a clean
transaction-rolled-back skip logged as an error, no crash); and lifecycle transitions (activation / start /
resume / provisioning→exec / recovery) that *enable* rather than *create* trading. Arming remains separately
gated (provisioner rebuild + WP1–WP5 + WP6 + Sponsor).

## Pause / resume (deferred; required before arming)
Runtime lifecycle semantics — validation degradation while running → pause; restored HEALTHY → controlled
resume; credential replacement → previous validation invalidated until re-tested — are a follow-on increment.
Per the arming policy, they must be fully implemented and certified before this gate is armed.

## Consequences
- Reusable, single-source gate; additive; no schema change; no execution-path change while OFF.
- Reason codes are customer-safe and suitable for API/frontend surfacing.
- Refusals are audited (`EXECUTION_GATE_REFUSED`, or the promotion trail) without exposing secrets.

## WP1B/WP2 continuation — final-dispatch safety + credential invalidation (2026-08-04)

### Shared reason-code vocabulary (WP1B/WP2/WP3/WP4/WP5)
Stable, non-secret, customer-safe codes, defined once in `execution/broker_gate.py`. The final-dispatch
gate, credential invalidation, pause and resume all speak these; the older creation-gate codes map onto
them (`_ELIGIBILITY_TO_SHARED`), and WP3 health states map via `_HEALTH_STATE_TO_SHARED`:

`broker_account_missing`, `broker_account_ambiguous`, `broker_account_inactive`,
`broker_account_disconnected`, `broker_account_tombstoned`, `broker_credential_missing`,
`broker_validation_required`, `broker_validation_failed`, `broker_validation_unavailable`,
`broker_health_degraded`, `broker_health_stale`, `broker_health_disconnected`,
`broker_resume_not_eligible`, `broker_health_state_changed`.

### Final-dispatch gate (TOCTOU)
The creation gate proves eligibility when a job is *created*; between enqueue and the live `order_send`
an account can be disconnected, have its credential replaced, or have its broker health degrade. A
second authoritative recheck runs **immediately before the real dispatch** — `evaluate_dispatch_gate`
(and the worker helper `evaluate_job_dispatch`, which resolves the job's account FRESH from the DB and
audits `EXECUTION_DISPATCH_REFUSED`). It:
- re-evaluates eligibility fresh (never the enqueue-time snapshot);
- when `BROKER_CONNECTIVITY_HEALTH_ENABLED` is also on, consumes the latest WP3 contract
  (`broker_health.get_contract`) and refuses an ineligible (adverse or not-yet-healthy) account — a
  contract that exists and is not `eligible` blocks; no contract adds no constraint;
- is **fail-closed**: an eligibility failure, or an *error* reading health, refuses (never opens exposure
  on ambiguity);
- is **transparent** when `BROKER_CONNECTIVITY_EXECUTION_GATE` is OFF (no DB read, existing behaviour).
Wired at the sole live exposure-opening dispatch — `mt5_trade_ingest_worker` PLACE_ORDER/PLACE_TEST_ORDER,
immediately before `agent_order`. Emergency/non-opening operations (SYNC/MODIFY/CLOSE, breakeven) are not
gated (they reduce or reconcile exposure).

### Credential-replacement invalidation
`trading.broker_connectivity.replace_credentials` now invalidates prior eligibility **atomically** with the
rotation: in one transaction it re-encrypts the credential, sets `validation_status = NEVER`, clears
`validated_at`, and (when the health engine is on) resets WP3 health to UNKNOWN
(`invalidate_for_credential_replacement`: non-eligible, `resume_eligible` cleared, counters + last-success
reset, version bumped, `BROKER_HEALTH_CREDENTIAL_INVALIDATED` audited). The append-only validation-attempt
history is preserved. No resume is possible until a *fresh* successful validation. A health-engine error
does not abort the rotation (the gate already fails closed on `validation_status = NEVER`; the error is
audited `BROKER_HEALTH_INVALIDATION_ERROR`). A failure in the atomic block rolls back completely — no
partial invalidation.

### Health convergence on the validation flow
`run_broker_validation` now folds its outcome into the WP3 engine (`record_validation_outcome`, no-op
when health is DARK, fail-open) so a freshly-validated account converges to HEALTHY *immediately* on the
customer flow — not only on the next (inert) scheduler cycle. Without this, arming BOTH flags would
dispatch-refuse a just-validated account (`broker_validation_required`) until the scheduler ran (the
safe direction, but an operational hazard). Arming-runbook note: the two broker-connectivity flags now
share one tolerant parser (`1/true/yes/on`); still, arm the health engine only once it converges rows
promptly, and arm the execution gate and health engine together deliberately.

### Health degradation → runtime pause (WP2-owned, ADR-0029)
The execution layer owns pause. `execution.BrokerRuntimePause` (OneToOne per account, additive, distinct
from the provisioning `AccountRuntime.state` so the two lifecycles never overload one field) is the
durable, secret-free pause record: `paused`, `reason_code`, `source_state_version`,
`last_processed_version`, `paused_at`, `resume_eligible`, `resumed_at`. `execution/runtime_pause.py`
provides:
- **`process_broker_health_pause(account)`** — reconciles the record with the latest WP3 contract,
  **idempotently keyed on `state_version`**: a version is processed at most once, a smaller version is
  ignored (`BROKER_HEALTH_STALE_PAUSE_VERSION_IGNORED`), a larger one supersedes — an older decision can
  never reverse a newer one. On `pause_required` it persists a pause (`BROKER_RUNTIME_PAUSED` on the
  edge); on recovery it records `resume_eligible` (`BROKER_RECOVERY_DETECTED`) **without resuming** —
  only the controlled resume service (Workstream D) may clear `paused`. The durable `resume_eligible` is
  keyed on the **live contract's `eligible` (HEALTHY)**, not WP3's `resume_eligible` edge, so a recovery
  via a broken edge (credential replace → re-validate → HEALTHY, which WP3 marks `resume_eligible=False`)
  still marks the paused runtime resumable; the resume service re-checks the live contract, so the flag
  never authorises a resume alone. Serialised with `select_for_update`. Pause NEVER deletes/tombstones
  the runtime, touches credentials, or creates an
  order/job. Inert unless BOTH flags are on.
- **Creation-time block** — `require_not_broker_paused` is enforced at the model boundary
  (`ExecutionJob.save`, alongside the eligibility gate) so a degraded-but-still-VALIDATED account cannot
  create a new exposure-opening job (it refuses on the live contract's `pause_required`, immediate).
- Triggers: the customer validation flow (`run_broker_validation` → reconcile) and a refused live
  dispatch both reconcile the durable record; the final-dispatch gate independently refuses on the live
  contract. Pause supported for DEGRADED / STALE / DISCONNECTED / TOMBSTONED; disconnected/tombstoned
  remain permanently ineligible for execution and resume.

### Controlled resume (WP2-owned, ADR-0029)
Recovery makes a runtime *eligible* for resume; it is never *authority* to resume. The single, explicit
`execution.runtime_pause.request_broker_runtime_resume(account)` is the SOLE path that clears a
broker-health pause — **never invoked automatically** (no scheduler, save hook, signal, validation,
credential replacement, provisioning, restart or periodic task calls it; a source-coupling test proves
the only caller is the service itself). Immediately before clearing, in ONE transaction under
`select_for_update` on both the pause row and the account, it reloads and re-verifies: account
eligibility (`evaluate_execution_gate`: exists / active / not-disconnected / credential / VALIDATED) and
the **live WP3 contract** (`get_contract`: exists, `eligible`, not `pause_required`) — the pause row's
`resume_eligible` is **advisory only; the live contract is authoritative**. Idempotency + concurrency are
keyed on `state_version`: a resume whose current version is older than the pause's `source_state_version`
fails closed (`BROKER_HEALTH_STALE_RESUME_VERSION_IGNORED`); a newer degradation refuses; duplicate/replay
callers get an idempotent result (at most one clears the pause — the row lock serialises them); a newer
pause always wins over an older resume. On success it clears **only** `paused`, records
`resumed_at`/`resumed_state_version`/latest `last_processed_version`, and audits `BROKER_RUNTIME_RESUMED`
— it starts no runtime, arms no strategy, creates no job/order, and accesses no credential. Returns a
deterministic, non-secret `ResumeResult` (`resumed`/`idempotent`/`refused`/`reason_code`/
`processed_state_version`/`current_state_version`). Inert (no lock, read, write or audit) unless BOTH
flags are on.

### Still deferred (before arming)
~~Full entry-point re-inventory + h4 scheduler graceful-refusal parity + runtime start/resume/recovery
rechecks — Workstream E.~~ **Completed — see below.** Arming remains separately gated.

---

## Workstream E — Execution-safety closure (2026-08-04)

The final WP1B/WP2 repository increment: a definitive authoritative-route inventory, refusal-handling
parity, and runtime-lifecycle classification. All enforcement stays behind
`BROKER_CONNECTIVITY_EXECUTION_GATE` (+ `_HEALTH_ENABLED`), default OFF; additive; DARK; transparent when
OFF.

### 1. Authoritative route inventory (machine-verifiable)
`backend/execution/execution_entrypoints.json` classifies **every** route that can create exposure /
dispatch / retry / start / resume / recover / activate execution (FULLY_COVERED / NON_OPENING_EXEMPT /
DEAD_UNREACHABLE / TEST_ONLY — **no UNKNOWN, no FIX_REQUIRED**). A drift guard
(`tests_execution_entrypoints.py`) fails CI on an UNKNOWN/FIX_REQUIRED classification, a **new
un-inventoried backend `ExecutionJob` creation site**, or a mismatch between the inventory's
exposure-opening job types and `BROKER_GATE_BLOCKED_JOB_TYPES`.

### 2. The authoritative gate boundaries (and why runtime-start is exempt)
Exposure opens at exactly two boundaries: **creation** (`ExecutionJob.save` → `require_execution_gate` +
`require_not_broker_paused` for `BROKER_GATE_BLOCKED_JOB_TYPES`) and **dispatch**. **Runtime start /
restart / recovery / reclaim are broker-INDEPENDENT and open NO exposure** — a beta runtime reaching
RUNNING is view-only; exposure opens only later via `ExecutionJob.save`. A runtime-start broker gate is
therefore **deliberately not added** (it would conflict with the working broker-independent RUNNING
journey / Customer Zero); the authoritative gate is the exposure-creation + dispatch boundary. The
self-serve "arm" path grants execution *authority* only (orders still pass `ExecutionJob.save`), so it is
exempt; an optional flag-gated activation-time recheck there is future belt-and-braces, not required.

### 3. `next_job` claim-boundary dispatch gate (the central closure)
The final-dispatch gate previously lived only in the ingest worker, so a **direct `next_job` poller (a
host-side bridge) bypassed it**. WSE enforces `evaluate_dispatch_gate` at the authoritative
`ExecutionJobViewSet.next_job` **claim boundary**: an exposure-opening job for a now-ineligible account is
**FAILED under the row lock** (audited `EXECUTION_DISPATCH_REFUSED` + projection, in autocommit outside the
committed claim tx) and never handed out — so **no claimer or transport** (ingest worker, `mt5_signal_
bridge`, `mt5_demo_bridge`, or any future executor) can dispatch an ineligible order. Transparent + zero
extra DB read when the gate flag is OFF.

### 4. Refusal-handling parity
- **h4 scheduler** brought to h1/m5 parity: catches `(ExecutionKillSwitchEngaged, ExecutionGateRefused)`,
  re-emits the durable `EXECUTION_GATE_REFUSED` audit + projection outside the rolled-back atomic
  (`trigger="scheduler_h4"`, `bar_close_iso`-deduped). h1/m5 projections now also pass `bar_close_iso` so
  repeat evaluations of one bar collapse to a single operational event (the WP5.2 dedup intent, now wired).
- **Demo test-order** (`PLACE_TEST_ORDER`, which opens REAL exposure) now uses the enforcing
  `require_execution_gate` + `require_not_broker_paused` (durable audit + projection + a clean 503 for both
  eligibility and pause), closing the pure-evaluator audit gap.
- **Promotion** `_validate` gained an `is_broker_paused` pre-check, so a paused DEMO account is rejected as
  `PromotionRejected("broker_gate_paused")` (which voids the plan and frees the concurrency slot) instead
  of raising at `ExecutionJob.save` and leaking a PLANNED slot.

### 5. Refusal ownership (no duplicate audit/event)
Rule: **the transaction that commits the refusal owns the durable event.** A funnel that creates the job
inside its own `atomic()` and swallows `ExecutionGateRefused` re-emits in its catch (the model-gate emit
is sacrificial/rolled-back); a funnel that refuses in autocommit relies on the gate's `_audit_refusal`; a
funnel with an independent durable trail (promotion) uses `evaluate_*`, never `require_*`. These are
mutually exclusive per logical refusal → exactly one durable event.

### 6. Controlled-resume caller proof (hardened)
`NoAutomaticResumeTests` now: uses an **allowlist** (survives arming when the one sanctioned operator
caller is wired); a **positive control** (asserts the scanner finds the token in its definition file and
walked >200 files — RULE 11); a **split-string red-flag** scan; and a **behavioural** guard that
`process_broker_health_pause` never clears a pause (replacing the brittle source-string assertion). Scope
boundary documented: scans `backend/**/*.py` only (standalone host services + non-`.py` scheduler manifests
are out of scope, governed by the arming runbook). Residual dynamic-reflection gap (`dir()`/suffix)
documented.

### 7. Reason-code vocabulary
`SR_*` (`execution/broker_gate.py`) is the single canonical customer-safe vocabulary; `R_*` (creation),
health `REASON_*`, and the promotion `broker_gate_*` prefix are operator/origin detail carried in
metadata, mapped via the three sanctioned translators (`_ELIGIBILITY_TO_SHARED`, `_HEALTH_STATE_TO_SHARED`,
`_HEALTH_TO_PAUSE_REASON`). The credential-replacement concept is canonicalised on `credential_replaced`
(one spelling). No new codes introduced; `SR_ACCOUNT_AMBIGUOUS` remains reserved.

### 8. Concurrency (verified)
Shipped config is **deadlock-free** (`ATOMIC_REQUESTS` unset → views autocommit; the only pause→account
holder, the resume service, has no production caller). Stale resume can never clear a newer pause (row
lock + live-contract recheck + version floor). **Zero** extra DB reads when the gate flag is OFF; O(1) (not
N+1) when ON; the dispatch recheck remains immediately before the irreversible send. Latent ABBA hazard
(if resume is ever wired to a view holding an account write-lock) recorded for the arming runbook.

### Repository arming-readiness (all satisfied for the gate/pause/resume plane)
No exposure-opening route bypasses the gate; every scheduler handles refusal safely; test-order refusal is
safe + audited; runtime start/recovery are broker-independent (exposure gated at `ExecutionJob.save` +
dispatch); controlled resume is explicit-only; audit/operational-event ownership is deterministic;
flag-OFF behaviour is unchanged; inventory drift fails CI. Arming itself remains Sponsor-gated.
