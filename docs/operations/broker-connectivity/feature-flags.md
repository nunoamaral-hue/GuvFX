# Feature-Flag Inventory — Broker Connectivity (WP5.4 Workstream A)

The definitive inventory of every broker-connectivity / operational-event feature flag. Machine-readable
copy: [`feature-flags.json`](feature-flags.json). All six flags are **default OFF/DARK**. No flag carries a
secret; none appears in `docs/SECRET_INVENTORY.md` (correct). The only repo-tracked flag allow-list is
`frontend/parity/env-allowlist.json` (the two `NEXT_PUBLIC_*` flags).

**Reading the "read as" convention:** backend flags are read **live** via a function (not an import-time
constant) using a tolerant parser accepting `1/true/yes/on`, so they toggle **without a process restart**.
The two frontend flags are `NEXT_PUBLIC_*` — Next.js **inlines them at build time**, so arming requires a
**rebuild**. Whether any flag is actually set in a running environment is **HOST-VERIFIED / OUTSIDE
REPOSITORY CONTROL**; the repository proves only the defaults (all OFF) and the effects.

## Consolidated table

| Flag | Owner | Scope | Default | Timing | Depends on | Risk | Sponsor |
|------|-------|-------|---------|--------|-----------|------|---------|
| `BROKER_CONNECTIVITY_ENABLED` | WP1A / ADR-0028 | backend | OFF | runtime | — | AMBER | yes |
| `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` | WP4.2 / ADR-0031 | frontend | OFF | build-time | — | AMBER | yes |
| `OPERATIONS_EVENTS_ENABLED` | WP5.1/5.2 / ADR-0032 | backend | OFF | runtime | — | AMBER | yes |
| `NEXT_PUBLIC_OPERATIONS_ENABLED` | WP5.3 / ADR-0032 | frontend | OFF | build-time | — | AMBER | yes |
| `BROKER_CONNECTIVITY_HEALTH_ENABLED` | WP3 / ADR-0030 | backend | OFF | runtime | (pairs with EXECUTION_GATE) | RED | yes |
| `BROKER_CONNECTIVITY_EXECUTION_GATE` | WP1B/WP2 / ADR-0029 | backend | OFF | runtime | (pairs with HEALTH_ENABLED) | RED | yes |

> **No code-level `depends_on`.** The dependency is *operational*: broker-health-driven **pause/resume and
> dispatch refusal** require **both** `BROKER_CONNECTIVITY_EXECUTION_GATE` **and**
> `BROKER_CONNECTIVITY_HEALTH_ENABLED` ON (`execution/broker_gate.py:88-95`, `execution/runtime_pause.py:51-59`).
> Each flag can be set independently; the two are deliberately built with the *same* tolerant parser so they
> cannot silently disagree.

---

## 1. `BROKER_CONNECTIVITY_ENABLED`

- **Definition / accessor:** `backend/trading/broker_connectivity.py:28-31` (`broker_connectivity_enabled`).
- **Scope / timing:** backend, runtime (live per call). **Default:** OFF.
- **Dependency flags:** none upstream (WP1A master).
- **When ON:** the customer broker-account journey backend is live — test-connection / retry-validation /
  replace-credentials / disconnect endpoints operate; `run_broker_validation` records durable state and
  folds outcomes into WP3 health + WP2 pause reconcile + WP5.2 projection.
- **When OFF:** `_bc_guard()` (`trading/views.py:467-470`) returns **HTTP 404** for the entire WP1A surface.
- **Deploy / rebuild:** none to toggle. Live customer-flow validation additionally needs the **signing
  keyring provisioned to the seal-only backend** at arming (ADR-0028) — a separate Sponsor-gated deploy;
  until then customer-flow endpoints return **UNAVAILABLE**.
- **Verification:** OFF → 404; ON → endpoints operate, one `BrokerAccountValidationAttempt` row per
  validation. Tests: `backend/trading/tests_broker_connectivity.py`.
- **Rollback:** set OFF (unset / `0`); surface returns to 404 immediately, no schema change.
- **Risk:** AMBER (customer-facing but non-executing). **Sponsor approval:** required.

## 2. `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED`

- **Definition / accessor:** `frontend/src/lib/flags.ts:9-15` (`brokerConnectivityEnabled`).
- **Scope / timing:** frontend, **build-time** (inlined). **Default:** OFF.
- **When ON:** the customer Broker Accounts routes render; nav entry appears (`AppShell.tsx:102`).
- **When OFF:** `/broker-accounts` routes call `notFound()` (404) **before any data fetch**; no nav entry;
  no API call; existing UI byte-identical.
- **Deploy / rebuild:** **frontend rebuild** with the flag set. Registered in `parity/env-allowlist.json`.
- **Verification:** OFF → routes 404, no nav; verify a flag-ON render only in a **non-production** build.
  Tests: `frontend/src/app/(app)/broker-accounts/flag-gate.test.tsx`.
- **Rollback:** redeploy the DARK image built with the flag unset (cannot toggle at runtime).
- **Risk:** AMBER. **Sponsor approval:** required.

## 3. `OPERATIONS_EVENTS_ENABLED`

- **Definition / accessor:** `backend/operational_events/constants.py:12-19` (`operations_events_enabled`).
- **Scope / timing:** backend, runtime. **Default:** OFF.
- **When ON:** the operational-event read model records/projects events (projection-only, fail-open,
  `transaction.on_commit`) and `GET /api/operations/account-events/` serves them, **owner-scoped**.
- **When OFF:** `record_event` no-ops, all projections early-return, `mark_resolved` returns 0, API **404**.
  Every projection call site is additionally **fail-open** so it can never break the authoritative path.
- **Deploy / rebuild:** none to toggle. **Independent** of the broker-connectivity flags. The operator UI
  additionally needs `NEXT_PUBLIC_OPERATIONS_ENABLED`.
- **Verification:** OFF → API 404, no rows; ON → events project on commit; API returns `{summary, timeline}`.
  Watch logger **`guvfx.operational_events`** for recorder failures (fail-open ⇒ silent to callers).
- **Rollback:** set OFF; recording + API return to DARK. The read model is a **rebuildable projection**
  (a cache) and may be truncated/rebuilt without affecting authoritative state.
- **Risk:** AMBER (read-only projection). **Sponsor approval:** required.

## 4. `NEXT_PUBLIC_OPERATIONS_ENABLED`

- **Definition / accessor:** `frontend/src/lib/flags.ts:17-25` (`operationsEnabled`).
- **Scope / timing:** frontend, **build-time**. **Default:** OFF.
- **When ON:** the internal Operations & Support viewer routes render + **admin-only** nav entry
  (`AppShell.tsx:114`). Data appears **only** when backend `OPERATIONS_EVENTS_ENABLED` is also ON; the
  surface is additionally operator-gated (`useAdminRole`) and read-only.
- **When OFF:** operations routes `notFound()` (404) before any hook/API call; no nav; no fetch.
- **Deploy / rebuild:** frontend rebuild. **Separate** from `OPERATIONS_EVENTS_ENABLED` — **both** must be
  armed for the viewer to show data. Registered in `parity/env-allowlist.json`.
- **Verification:** OFF → 404, no nav, zero API calls (`operations/accounts/flag-gate.test.tsx`).
- **Rollback:** redeploy the DARK image built with the flag unset.
- **Risk:** AMBER (internal, read-only). **Sponsor approval:** required.

## 5. `BROKER_CONNECTIVITY_HEALTH_ENABLED`

- **Definition / accessor:** `backend/reliability/constants.py:211-214` (`broker_health_enabled`).
- **Scope / timing:** backend, runtime. **Default:** OFF. Same tolerant parser as the execution gate.
- **When ON:** the WP3 health state machine is active — folds validation-attempt evidence + staleness +
  lifecycle into `BrokerAccountHealth`, emits audit + deduplicated alerts, exposes the convergence contract.
  **The scheduler remains an inert framework** (no validator is ever wired in WP3); health converges on the
  **customer validation flow** (`broker_connectivity.py:96-98`), not a background poller.
- **When OFF:** every entry point no-ops (`get_contract`, `record_validation_outcome`, `sweep_stale`,
  `run_cycle` → `{ran:false, reason:disabled}`); no row mutated, no signal.
- **Dependency:** health-driven **pause/resume + dispatch refusal** additionally require the execution gate.
- **Behaviour tuning:** `broker_health_config()` env vars (`reliability/constants.py:233-250`) — thresholds
  and cadence, **not** arming gates; inert while this flag is OFF. Listed in `feature-flags.json`
  (`related_tuning_toggles`).
- **Verification:** OFF → `get_contract` returns `None`; ON → health converges on customer evidence.
  Tests: `reliability/tests_broker_health.py`.
- **Rollback:** set OFF (instant, engine no-ops). Optional schema rollback: `migrate reliability 0003`
  drops the additive `BrokerAccountHealth` table (safe while OFF).
- **Risk:** RED (feeds execution eligibility when paired with the gate). **Sponsor approval:** required.

## 6. `BROKER_CONNECTIVITY_EXECUTION_GATE`

- **Definition / accessor:** `backend/execution/broker_gate.py:83-85` (`execution_gate_enabled`).
- **Scope / timing:** backend, runtime. **Default:** OFF.
- **When ON:** execution is refused unless the account is `VALIDATED` and eligible, enforced at **three
  points**: (1) `ExecutionJob.save()` creation (`execution/models.py:248-255`); (2) claim in
  `next_job` under a row lock (`execution/views.py:322-364`); (3) **final dispatch** re-evaluated FRESH
  immediately before `order_send` in the worker (`mt5_trade_ingest_worker.py:807-819`). Fail-closed on any
  ambiguity; refusals audited (`EXECUTION_GATE_REFUSED` / `EXECUTION_DISPATCH_REFUSED`) and projected.
- **When OFF:** the gate is **transparent** — `GateDecision(True, "gate_disabled")` short-circuits before
  any DB read; existing production execution behaviour is unchanged.
- **Dependency:** health-driven dispatch refusal + pause/resume require `BROKER_CONNECTIVITY_HEALTH_ENABLED`
  too. When only the gate is on, refusal is driven by validation eligibility alone.
- **Deploy / rebuild:** none to toggle. **Arming is gated on WP6 certification** (ADR-0029).
- **Verification:** OFF → no execution-path change, no extra DB read; ON → an ineligible account's
  exposure-opening job is blocked at creation, failed at claim, refused at dispatch. Tests:
  `execution/tests_broker_gate.py`, `tests_dispatch_gate.py`, `tests_wse.py`, `tests_execution_entrypoints.py`,
  `tests_runtime_pause.py`.
- **Rollback:** set OFF (gate transparent instantly); if paired, set `BROKER_CONNECTIVITY_HEALTH_ENABLED`
  OFF too. Additive/transparent when OFF; **no dedicated code-revert procedure exists or is required.**
- **Risk:** **RED** — the only flag that changes whether an order is placed. **Sponsor approval:** required;
  **WP6 PASS is a precondition.**

---

## Adjacent toggles operators must not confuse with arming flags

Present in the repo, broker-adjacent, but **not** part of this arming domain (`feature-flags.json` →
`adjacent_flags_not_in_scope`). Values shown are **repo defaults**:

- `EXECUTION_HEALTH_ENABLED` — default **ON**; existing execution-health sweep, unrelated to broker
  connectivity (`execution/execution_health.py:79-80`).
- `RELIABILITY_CORE_ENABLED` — default OFF; RX-2 reliability supervisor (`reliability/constants.py:15`).
- `BROKER_SYMBOL_REGISTRY_STRICT` — default OFF; broker/account-aware symbol gating.
- `NOTIFICATION_DISPATCH_ENABLED` — default OFF; alert/notification delivery sink (relevant to monitoring —
  see [monitoring-spec.md](monitoring-spec.md)).

Also present but out of this domain entirely (listed so they are not conflated): `BREAKEVEN_ENABLED`,
`PROVIDER_COMMANDS_ENABLED`, `TP_WATCHER_ENABLED`, `MULTI_ACCOUNT_ROUTING_ENABLED`, `RISK_MARGIN_GUARD_ENABLED`,
`RISK_REQUIRE_TERMINAL_NODE`, `BETA_SELF_SERVE_ARM_ENABLED`, `BETA_RUNTIMES_ENABLED`, `BETA_ONBOARDING_ENABLED`,
`PROVISIONING_REQUIRE_BROKER_LOGIN`.

## Coverage guarantee

`backend/operational_events/tests_wp54_readiness.py` scans the flag definition files and **fails CI if a
broker-connectivity arming flag appears in source without an inventory entry**, if any flag defaults ON in
code, if a dependency references a non-existent flag, or if a dependency cycle exists.
