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

### Still deferred to the pause/resume increment (before arming)
Runtime pause on confirmed degradation (WP2 owns the pause action; WP3 emits `pause_required`), controlled
resume on `resume_eligible` (never automatic; final recheck; disconnect/tombstone permanently blocks),
state-version idempotency at the pause/resume layer, and h4 scheduler graceful-refusal parity. Arming
remains separately gated.
