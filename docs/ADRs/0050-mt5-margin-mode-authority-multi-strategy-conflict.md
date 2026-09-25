# ADR-0050 — MT5 Margin-Mode Authority + Multi-Strategy Same-Symbol Conflict Policy (DARK)

- **Status:** Accepted (implementation DARK; conflict policy default OFF)
- **Date:** 2026-09-25
- **Packet:** Strategy Ownership Phase B1 (P0 prerequisite for MAGIC_SEND + the 5-account/multi-strategy product)
- **Relates to:** ADR-0049 (durable strategy ownership + magic numbers), B0 MAGIC_SEND forensic

## Context

B0 established that MT5 **hedging** accounts keep each strategy's same-symbol trade as an independent
position (own `position_id`/`POSITION_MAGIC`), while **netting** accounts merge same-symbol trades into a
single position with a single magic (first-in wins) — destroying per-strategy identity. GuvFX captured no
authoritative margin mode, so it could not tell the two apart and could not safely permit concurrent
independent strategies on the same symbol. MAGIC_SEND must not be enabled until this is closed.

## Decision

1. **Authority location — the `HostedMt5Workspace` projection, not `TradingAccount`.** The observed margin
   mode is a runtime *observation*, so it lives with the other observed MT5 facts (`proj_*`) on the
   workspace, which already carries the freshness anchor (`last_decision_at`). `TradingAccount` remains the
   account-*identity* authority. One authority, no competition. Accounts without a hosted workspace
   (Customer Zero, legacy) therefore have no observed mode → treated as UNKNOWN → fail closed.

2. **Capture is read-only + additive, requires a Windows-host observer redeploy.** `account_info()` already
   returns `margin_mode` natively; the observer reads it via `getattr(acc, "margin_mode", None)`
   (`run_observer.py`, `Invoke-GuvfxObserver.ps1`, ASCII-only per RULE 9) and the backend plumbs it through
   the existing observation pipeline to a new additive nullable column `HostedMt5Workspace.proj_margin_mode`
   (migration `hosted_workspace 0011`). It is a health/capability signal ONLY — it never feeds the identity
   matcher, the `EXECUTION_READY` gate, or lifecycle-state derivation.

3. **Int→label mapping is strict + fail-closed** (`hosted_workspace/margin_mode.py`):
   `RETAIL_NETTING=0 → NETTING`, `EXCHANGE=1 → EXCHANGE`, `RETAIL_HEDGING=2 → HEDGING`; anything else
   (None/bool/str/unknown int) → **UNKNOWN**. Never inferred from broker/server names (RULE 11 — a positive
   control on the host must confirm `margin_mode==2` on a known-hedging account before the policy is armed).

4. **Freshness is mandatory.** A margin observation older than `FRESHNESS_SECONDS` (24h — generous, since
   margin mode ~never changes, but a stale value must never authorize a conflicting order) is treated as
   UNKNOWN. The conflict policy and the capability read-model share one gate (`label_if_fresh`).

5. **DARK conflict policy** (`execution/risk_controls.evaluate_symbol_conflict`, flag
   `STRATEGY_SYMBOL_CONFLICT_POLICY_ENABLED`, default OFF) hooked into the single promotion choke point
   `signal_promotion._validate`. Semantics: **HEDGING** (fresh) → allow independent same-symbol assignments;
   **NETTING / EXCHANGE / UNKNOWN / stale** → allow only when no *different* active assignment already owns
   the symbol (open Trade or PLANNED/PROMOTED plan), else refuse. The promoting owner is excluded, so
   same-assignment multi-leg / repeated signals never trip. NULL-owner (manual / legacy magic=0) same-symbol
   exposure counts as foreign (fail-closed — independence unprovable). Reason codes:
   `multi_strategy_netting_symbol_conflict`, `exchange_mode_multi_strategy_uncertified`,
   `margin_mode_unknown_multi_strategy_conflict`, `symbol_conflict_indeterminate` (on error). Cross-account
   is structurally isolated (queries are account-scoped).

6. **Capability read-model** (`onboarding_read_model.capability_projection`): additive, secret-free
   projection of `margin_mode` / `multi_strategy` (FULL|LIMITED|PENDING) / `same_symbol_concurrent` /
   `capability_fresh` / `capability_observed_at`, derived purely from the fresh margin mode.

## Consequences

- **Compatibility preserved.** Flag OFF ⇒ `_validate` is byte-identical to today (no query issued); the
  1065-test suite is green. The policy only restricts *concurrent independent* same-symbol strategies on
  non-hedging/unknown accounts, and only when armed — existing single-strategy execution is unaffected.
- **Coverage is safety-correct but partial.** Only hosted tenants (support@, beta) get a real margin mode
  once the host observer is redeployed; CZ/legacy/pre-login accounts stay UNKNOWN → the policy fails closed
  for them. This is correct (fail-closed) but does not grant them multi-strategy capability.
- **MAGIC_SEND stays OFF.** This ADR neither enables MAGIC_SEND/READ/ENFORCE nor changes the order payload,
  sizing, comments, routing, or identity pins.

## Amber / out-of-scope (separate decisions)

- **Live Windows-host observer redeploy** (Amber → Red, RULE 8/9, Nuno-gated): the two read-only scripts are
  authored + ASCII-validated; the host redeploy + a RULE-11 positive control are the gated activation step.
- **`DEAL_ENTRY_INOUT` ingest fix** (Phase 8): correct representation of a netting reversal requires touching
  shared Trade keying (synthetic sub-ticket or an additive `position_id` column) — its own ADR, and there is
  no real INOUT deal in the current estate to validate against (RULE 11). Designed, not implemented here.
- **Mandatory identity-pin enforcement for legacy/Provider-A/CZ** (Phase 7): money-path (a login/server
  format drift could false-reject a real close and strand exposure) → design-only STOP. The central
  injection seam already exists for hosted Provider-B; hardening the rest is a Red decision.
