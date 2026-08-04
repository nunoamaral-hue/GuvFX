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
