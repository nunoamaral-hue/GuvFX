# Stream D — Real/LIVE MT5 + Broker-Email Identity + Broker-Intelligence Foundation (DESIGN)

**Date:** 2026-10-05 · **Status:** DESIGN CHECKPOINT (pre-implementation; Sponsor review) · **Programme:** post-demo remediation, Stream D
**Depends on:** Streams A/B/C (Model-A lifecycle) — complete + certified. **Constraints:** no real-money order; Cold-Boot V2 paused; 25/35/36 untouched.

This is a design only. No backend behaviour changes until the relevant sub-PRs are built, reviewed, CI-green and Sponsor-approved. Implementation proceeds within the already-approved Gate-2 architecture.

---

## 1. DEMO/LIVE persistence recommendation — **RETAIN `is_demo` + CENTRALIZED POLICY + REQUIRED explicit account-type. Do NOT migrate to a new enum.**

**Finding.** The environment is already persisted twice: `TradingAccount.is_demo` (bool, per-account — the lifecycle-instance truth) and `BrokerServer.environment` (`demo`/`live` enum, per-server), cross-checked by `trading.serializers.classification_error`. The real defect is not a missing column — it is that **~17 sites read these signals ad-hoc**: `is_demo` walls (`execution/readiness.py:144`, `execution/hosted_provisioning.py:57`, `hosted_workspace/producer.py:122`+`matching.py:100`, `hosted_workspace/provisioning.py:402,519`, `hosted_workspace/onboarding_views.py:202`, `strategies/views.py:1494,1623`, `execution/auto_router.py:147,193`, `strategies/signal_engine.py:277`, `session_reconciler.py:248`, `liveness_recovery.py:85,228`, `capability_recovery.py:122`, `supervised_beta.py:115`) **and** `broker_server.environment=='live'` walls (`signal_planning.py:242`, `signal_proposals.py:163`, `signal_promotion.py:173`, `execution/views.py:861`).

**Decision.** A new enum would be *style*, not correctness: `is_demo` already captures per-account environment and the Model-A explicit-type UX (§4) removes the "silent default" gap at the boundary. A column migration would touch every wall + all existing rows for no correctness gain and real migration risk. Instead:
- **Add `trading/account_policy.py` — the single authoritative DEMO/LIVE policy.** `account_environment(account) -> Environment{DEMO,LIVE}` derived from `account.is_demo`, with a **fail-closed consistency assertion** against `broker_server.environment` (never silently pick one; a mismatch raises a sanitised integrity error — the system must not guess the environment of a money-bearing account). Plus `is_live(account)`, `require_demo(account)`, `is_live_execution_authorized(account)` (§3).
- **Migrate all ~17 walls to call the policy** (behaviour-preserving for DEMO; the policy returns DEMO exactly where `is_demo is True` today). No second interpretation of `is_demo`.
- **Keep `is_demo` as stored truth** (backward compatible; existing accounts already classified). `BrokerServer.environment` stays a server attribute + consistency input, not an independent authority.

Rationale: one authoritative read, zero data migration, the explicit-input requirement (§4) closes the default gap, and the walls become auditable LIVE-aware decisions rather than scattered booleans.

## 2. LIVE monitoring state model (environment ≠ execution authorization)

A LIVE account must reach **CONNECTED / MONITORING** without execution authorization. Split the two concepts the policy already conflates:
- **Monitoring eligibility** (observe / identity-match / balances / positions / manual trades / dashboard / analytics / broker-intelligence): gate on *connected + identity-matched + fresh*, **independent of environment**. Today the demo-only walls in the OBSERVE/MATCH/CONFIRM path (`producer.py:122` hardcodes `is_demo=True`; `matching.py:100` `classification_mismatch`; `live_observe.py` trust anchor; `provisioning.py:402` confirm; `provisioning.py:519` bind) block LIVE from even being observed. **Change:** the observer's `ExpectedAccount` carries the account's *actual* environment (from the §1 policy), and the matcher requires the observed terminal's demo/live classification to **equal the expected** (LIVE expects LIVE, DEMO expects DEMO) — a mismatch is still fail-closed, but LIVE is no longer categorically rejected. Monitoring/confirm/bind then succeed for a LIVE account.
- **Canonical states:** `NOT_PROVISIONED → PROVISIONED → CONNECTED → MONITORING` reachable for LIVE. A new display state **`CONNECTED_MONITORING_EXEC_UNAUTHORIZED`** (derived, not stored) distinguishes "LIVE, observed, identity-matched, but automated execution not authorized" from EXECUTION_READY. The readiness `provider` stays `persistent_workspace`.
- **Readiness split:** `evaluate_readiness` returns two orthogonal facts — `monitoring_eligible` (environment-agnostic) and `execution_eligible` (requires §3 for LIVE). The existing `RW_REAL_ACCOUNT_NOT_ENABLED` wall (`readiness.py:144`) moves from "not eligible at all" to "not *execution*-eligible" — monitoring stays eligible.

## 3. LIVE execution-authorization model (durable, human-gated, versioned)

Automated LIVE execution is a **separate durable authorization**, never the arm bit and never system-derivable.
- **New model `execution.LiveExecutionAuthorization`:** `trading_account` (FK — the Model-A lifecycle instance; a new instance needs a new authorization), `user`, `broker_identity_snapshot` (login+server at authorization time), `strategy_assignment` (or a snapshot of the selected strategy), `sizing_snapshot` (risk/lot config reviewed), `authorization_version` (int; the acknowledgement-copy version), `acknowledgement_text_hash`, `created_at`, `created_by`, `revoked_at`/`revoked_by` (revocable), and an `is_active` partial-unique (at most one active per account instance). Immutable once written (revoke, never edit) — mirrors the Model-A immutability ethos.
- **Gate:** `is_live_execution_authorized(account)` = an active, non-revoked authorization whose `broker_identity_snapshot` still matches the currently observed login/server (identity drift ⇒ fail closed). For a **DEMO** account this is vacuously satisfied (demo keeps today's behaviour). For a **LIVE** account the arm gate (`hosted_provisioning.arm_hosted_workspace_execution`) and the strategy-arm/planning/promotion/routing walls additionally require it.
- **Ceremony (server-enforced order):** identity verified (observer match) → strategy selected → sizing/risk reviewed → explicit LIVE acknowledgement ("This is a live trading account using real funds. GuvFX strategies may place, modify and close real orders.") → write `LiveExecutionAuthorization` (durable audit) → Start Trading → normal readiness/capability gates. Each step fail-closed; the authorization write is the point of no silent progression.
- **Revocation / Stop Trading:** Stop Trading disarms (execution_enabled=False) but does NOT revoke the authorization; an explicit "disable live trading" revokes it. Remove (Model-A tombstone) implicitly ends it (new instance ⇒ new authorization required).

## 4. Explicit Add-Account DEMO/LIVE UX/API contract

- **API (server-side invariant):** the create contract (`trading/account_service.create_customer_account` via `TradingAccountSerializer`) requires an explicit **`account_type` ∈ {`demo`,`live`}** on NEW creation. Missing / null / unknown ⇒ **400 ValidationError**, never a silent default. It maps to `is_demo` (`demo`→True, `live`→False) and is cross-checked by the existing `classification_error` against `broker_server.environment` (if a server FK is supplied). Existing accounts are unaffected (classification already stored); the required field applies to creation only. This is the ONLY new environment interpretation — it funnels into the §1 policy.
- **UX:** replace `[x] This is a demo account` with a REQUIRED selector "Account type *" (placeholder "Select account type", **no default**); options "Demo account — Virtual funds" / "Live account — Real funds"; Add disabled until chosen; dynamic copy per the packet; LIVE rendered with a real-money visual treatment (badge/warning colour). New component registered in `frontend/parity/components.json` (ADR-0031 guard).
- **Tests:** no type preselected; Add disabled; DEMO maps `is_demo=True`; LIVE maps `is_demo=False`; missing `account_type` → 400; invalid → 400; existing accounts keep classification.

## 5. Per-runtime `MT5_ALLOW_LIVE` design

- **Today:** the bridge has a latent live path (`scripts/mt5_signal_bridge.py:238 _bridge_allow_live()` reading env `MT5_ALLOW_LIVE`) kept off only because `execution/bridge_config.py:28` never sets it. Make it an **intentional, per-runtime, derived** boundary — never a global switch.
- **Design:** `bridge_config` sets `MT5_ALLOW_LIVE=1` in a per-tenant bridge env **only when** the §1 policy says the account is LIVE **AND** `is_live_execution_authorized(account)` is true **AND** identity/readiness currently hold. Otherwise unset. The bridge **fail-closed** refuses a live order unless (a) the account is authoritatively LIVE and identity-matched at order time and (b) `MT5_ALLOW_LIVE` is set for that runtime. Two independent layers (backend never routes an unauthorized LIVE order to a bridge; the bridge itself refuses) — defence in depth. DEMO bridges never set it. Certified with **unit/integration/simulated-bridge tests only** (no live order).

## 6. Recovery semantics for LIVE

Recovery (liveness/session/capability + the paused Cold-Boot) may restore a LIVE account's Windows session, `/portable` MT5, broker connection and observer — **but runtime restoration never creates `LiveExecutionAuthorization`**. After recovery, automated execution resumes only if the pre-existing durable authorization is still active AND identity + capability + readiness gates pass. The recovery candidate predicates (`session_reconciler.py:248`, `liveness_recovery.py:85,228`, `capability_recovery.py:122`) change from `is_demo=True` to `monitoring_eligible` (environment-agnostic restore), with execution re-arm still gated on §3. UNKNOWN/error stays fail-closed; duplicate/bare terminals remain a failure (ref the P2 decommission-respawn finding).

## 7. Broker-email alias architecture

- **One opaque alias per BrokerAccount lifecycle instance:** `ba-<opaque>@accounts.guvfx.com`, where `<opaque>` is a non-sequential, non-enumerable token (e.g. a per-account random 128-bit base32, stored on a new `BrokerEmailAlias` model: `trading_account` FK, `alias_local`, `created_at`, `status`). **Never** expose the sequential BrokerAccount id in the address.
- **Model-A lifecycle:** alias A ↔ Account A; on tombstone the alias is retired (status=RETIRED) and stays bound to the historical account + its correspondence/evidence. A re-add (new instance B) mints a **new** alias B. An old alias is **never reassigned**.
- **Generation/uniqueness:** minted at account creation (or first provisioning); DB-unique on `alias_local`; the opaque token guarantees no enumeration. Routing: the broker-intelligence ingestion service (see §9) maps inbound `To:` alias → BrokerAccount instance.
- **Security:** GuvFX-controlled infrastructure; see §9 — decoupled from trading workers, MT5 bridge, execution authorization, and broker passwords.

## 8. BrokerEvent / Withdrawal data model

Normalized, extensible (not withdrawal-hardcoded).
- **`BrokerEvent`** (append-only evidence): `trading_account` (lifecycle instance), `broker`, `source` (enum, `EMAIL` first), `event_type` (open vocabulary: `WITHDRAWAL_REQUESTED|PROCESSING|APPROVED|COMPLETED|REJECTED|CANCELLED`, extensible to deposits/KYC/support/security/leverage/margin/outage/restriction/promotion — a string + a registry, not a closed enum that blocks expansion), `occurred_at`/`received_at` (distinct — broker time vs ingest time, per the UTC+3 lesson), `broker_reference_id` (nullable), `amount`/`currency` (nullable — never fabricated), `evidence_ref` (pointer to the stored raw message, NOT inline body), `parser_name`/`parser_version`, `confidence`, `evidence_hash` (integrity), `correlation_status` (`UNRESOLVED|CORRELATED|AMBIGUOUS`). Raw evidence is immutable (data rule); corrections are new rows.
- **`Withdrawal`** (durable lifecycle record correlated from events): `trading_account`, `broker_reference_id` (nullable), `amount`/`currency` (when reliable), `requested_at`, `completed_at` (nullable), `status` (`REQUESTED|PROCESSING|COMPLETED|REJECTED|CANCELLED|PENDING|UNRESOLVED`), `requested_event`/`completed_event` FKs, `correlation_method` (`reference_id|heuristic`), `sample_provenance`.

## 9. Email-ingestion architecture

**Isolated broker-intelligence service**, least privilege, fully decoupled from trading.
- A standalone ingestion worker/service owns the mailbox credential (secret store; never in normal app code, never in the MT5 bridge or trading workers). It pulls inbound mail for `*@accounts.guvfx.com`, stores the raw message to an evidence store (write-once; hash recorded), resolves `To:`-alias → BrokerAccount, runs a **deterministic per-broker classifier/parser** (versioned), emits normalized `BrokerEvent` rows, and never blocks or is blocked by trading. **Trading availability never depends on email availability** and vice-versa. No mailbox credential reaches Django request code; the service writes `BrokerEvent`/evidence via a narrow internal API or shared DB with least-privilege.

## 10. Withdrawal correlation rules

- **Preferred:** broker `withdrawal/reference id` → deterministic correlation into one `Withdrawal`.
- **Fallback (bounded heuristic):** `(broker_account, amount, currency, temporal proximity window)`. If the match is ambiguous (multiple candidates, or missing amount/ref) → leave **`UNRESOLVED`**, never guess.
- **No completion email ≠ failure:** absence of a COMPLETED event means `PENDING` / completion-not-observed, not REJECTED. Status only advances on positive evidence.

## 11. Private-vs-public aggregation boundary

- **Private customer evidence** (per BrokerAccount, exact): "Your last withdrawal took 2h 18m" — derived directly from that account's `Withdrawal` records. Shown in the account's Broker-Intelligence UX.
- **Public / WAYOND broker rating:** requires aggregation across **multiple broker accounts** with sample-size + privacy controls (min contributing accounts, k-anonymity-style floor) before any public number. One customer's withdrawal must **never** directly determine a public broker score. This packet designs the data to *support* later aggregation (e.g. "Median 3h 12m · 42 verified withdrawals · 11 accounts") but **does NOT define the WAYOND Verify scoring formula** — that stays a separate governed methodology.

## 12. Withdrawal metrics V1 (per BrokerAccount)

Define timestamps precisely: **REQUESTED** = `WITHDRAWAL_REQUESTED.occurred_at` (broker time); **COMPLETED** = `WITHDRAWAL_COMPLETED.occurred_at`. Duration = COMPLETED − REQUESTED. Metrics: last / average / median / fastest / slowest processing time; completed count; requested count; completion rate; pending count; age of oldest current pending; sample size; evidence provenance. Amount/currency retained when reliable. A read-only projection (not computed in a request hot path).

## 13. Migration / deployment sequencing

Governed sub-PRs, each tests → adversarial review → CI → controlled deploy; smallest-blast-radius first:
1. **D1 — centralized DEMO/LIVE policy** (`account_policy.py`) + migrate the ~17 walls to it (behaviour-preserving; DEMO unchanged). No schema change. Pure refactor-to-policy, heavily tested against current behaviour.
2. **D2 — explicit Add-Account type** (API required `account_type` + serializer + UX). Backward compatible.
3. **D3 — LIVE monitoring** (observer/matcher/confirm/bind environment-aware; `monitoring_eligible` split). LIVE can connect+monitor; execution still blocked by absence of §3.
4. **D4 — `LiveExecutionAuthorization`** model + ceremony + arm/planning/promotion/routing gates + `MT5_ALLOW_LIVE` per-runtime derivation + LIVE recovery semantics. Simulated-bridge tests only.
5. **E — broker catalogue waves** (FortressFX + DominionMarkets, then Wayond list) — may proceed in parallel where independent.
6. **F — discovery UX hardening.**
7. **Broker-Email identity** (`BrokerEmailAlias` + minting) → **BrokerEvent/Withdrawal models** → **isolated ingestion service** → **Withdrawal Intelligence V1** → UX. Sequenced after D so the account/LIVE/email models don't conflict.

Each LIVE-affecting PR stays DARK/flag-gated until its gates certify; no real-money order at any point.

---

## New Sponsor decisions genuinely required (item 13)

1. **DEMO/LIVE persistence:** confirm **retain `is_demo` + centralized policy + required explicit account-type** (no enum migration)?
2. **LIVE-execution-authorization as a new durable model** (`LiveExecutionAuthorization`, revocable, per-lifecycle-instance) — confirm the model + the ceremony order?
3. **Email alias public format** `ba-<opaque>@accounts.guvfx.com` — confirm the domain `accounts.guvfx.com` is/will be GuvFX-controlled, and the opaque-token (non-enumerable) choice? (DNS/MX + mailbox provisioning is infrastructure outside this repo — Sponsor/infra action.)
4. **Broker-intelligence ingestion isolation:** confirm a standalone service/worker (separate credentials, separate deploy) rather than a Django app sharing the trading DB — and where it runs.
5. **First go-live scope:** which account becomes the first real LIVE monitoring test (no execution), and when — noting no real-money order is authorized for certification.
