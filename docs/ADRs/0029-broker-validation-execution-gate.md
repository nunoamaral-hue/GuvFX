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

## Entry-point inventory
- **Wired in this increment (PR):** `execution.services.create_open_trade_job` (OPEN_TRADE service funnel);
  `execution.signal_promotion._validate` (auto-execution PLACE_ORDER promotion funnel — via
  `PromotionRejected("broker_gate_<reason>")`, on the existing audit trail).
- **To wire in follow-on increments (same service, no new logic) — precise inventory of the remaining
  NEW-order funnels (verified by adversarial review):**
  - `strategies/signal_engine.create_place_order_job` (→ PLACE_ORDER) — the **strategy auto-trade
    scheduler** path (run_h1/m5/h4 schedulers). This is a distinct live auto-execution funnel from the
    `signal_promotion` one wired above; it is **not** covered by "auto-execution promotion funnel".
  - `execution/views.py` PLACE_TEST_ORDER — the demo **test-order API endpoint** (entitlement- + daily-
    limit-gated today, not yet broker-gated).
  - Plus: strategy activation; runtime start/resume; provisioning→execution transition; automated
    recovery/restart.
  Until all are wired, full "no route bypasses" coverage is **not yet claimed** — but the flag is OFF so
  there is no production exposure, and arming is separately gated (WP6 + Sponsor). Trade-management jobs
  (SYNC / MODIFY / CLOSE / breakeven) are intentionally out of scope (they never open a new position).

## Pause / resume (deferred; required before arming)
Runtime lifecycle semantics — validation degradation while running → pause; restored HEALTHY → controlled
resume; credential replacement → previous validation invalidated until re-tested — are a follow-on increment.
Per the arming policy, they must be fully implemented and certified before this gate is armed.

## Consequences
- Reusable, single-source gate; additive; no schema change; no execution-path change while OFF.
- Reason codes are customer-safe and suitable for API/frontend surfacing.
- Refusals are audited (`EXECUTION_GATE_REFUSED`, or the promotion trail) without exposing secrets.
