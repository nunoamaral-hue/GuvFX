# ADR-0028 — Broker Connectivity Backend Lifecycle (WP1A)

- **Status:** Proposed (Governance Track — lifecycle owned by the PM/Sponsor)
- **Date:** 2026-08-03
- **Programme:** Broker Connectivity Capability – Trusted-Beta Integration (WP1A)
- **Builds on:** ADR-0026 (capability design), ADR-0027 (certified in-place broker-login validation primitive), P3-D (verified credential destruction)

## Context
The certified validation primitive (ADR-0027) is the accepted foundation. WP1A delivers the customer-facing
broker-account backend on top of it — validation history, test-connection, retry, replace-credentials,
disconnect, status, history — as an **additive, default-OFF, DARK** surface. It must not compromise
production safety or the Customer-Zero-stateless invariant proven during certification.

## Decision
1. **Feature flag `BROKER_CONNECTIVITY_ENABLED` (default OFF).** While off, the entire customer surface is
   dark (endpoints return 404, services refuse). Granular enough that incomplete work merges safely.
2. **Validation history — `BrokerAccountValidationAttempt`.** Append-only, per-account, **secret-safe**: it
   stores only the ADR-0027 `ValidationOutcome` allow-list (status, reason_code, retryable, is_demo, server,
   masked login, correlation id, trigger) — never a password, ciphertext, envelope, HMAC or host path.
3. **`validation_status` persistence — customer-flow only.** `run_broker_validation` reuses the existing
   `TradingAccount.validation_status` {NEVER/VALIDATED/CONNECTION_FAILED/TECHNICAL_ERROR} + `validated_at`,
   mapping HEALTHY→VALIDATED, NEEDS_ATTENTION→CONNECTION_FAILED, UNAVAILABLE→TECHNICAL_ERROR, and stamps
   `validated_at` only on HEALTHY. This runs **only from a customer account flow** (add/edit/test/retry/
   replace). The ADR-0027 **manual certification path does not call it and stays stateless** — the
   Customer-Zero-stateless invariant is preserved by construction.
4. **Disconnect is a TOMBSTONE, never a row delete.** `disconnect_account` performs verified credential
   destruction (P3-D `destroy_customer_credential`) + soft-disconnect (`is_active=False`, new
   `disconnected_at`) + `validation_status=NEVER`. The row, its immutable `Trade`/execution history, and its
   PROTECT relations are all retained — resolving the CASCADE/PROTECT delete hazard (14 FKs) identified in
   the investigation.
5. **Fail-closed + secret-safe throughout.** The validator contract is no-raise; a defensive guard maps any
   unexpected error to a fail-closed UNAVAILABLE attempt. Responses carry only allow-listed fields.
6. **User-scoped.** Every action resolves the account through the user-filtered queryset (staff/superuser
   bypass unchanged), so a user can only act on their own accounts.

## Deferred / out of scope for WP1A
- **One-active-account enforcement** for dedicated-runtime accounts (the existing
  `uniq_active_account_per_instance` covers only `mt5_instance`-based accounts). Deferred to a later slice
  with a data-safe migration; not required by WP1A.
- **Execution gating** (WP1B/WP2) — blocked on the Governance Track execution-gating decision.

## Arming dependency (not a code concern; gated)
The backend is **seal-only**; it does not hold the HMAC signing keyring. In production the customer-flow
endpoints therefore return UNAVAILABLE until the signing channel is provisioned to the backend at **arming
time** (a Sponsor-gated deploy step, with the provisioner-image rebuild as a precondition). While the flag is
OFF this is inert. Tests inject the validator, so the suite needs no keyring/MT5/agent.

## Consequences
- Additive schema only (one nullable field + one new table); no data migration; reversible.
- The customer journey is fully testable and mergeable DARK, decoupled from arming.
- Provides the durable state and history the frontend (WP4) and continuous health (WP3) consume.
