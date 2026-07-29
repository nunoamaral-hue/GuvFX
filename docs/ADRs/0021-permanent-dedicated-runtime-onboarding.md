# ADR-0021 — Trusted Beta onboarding becomes the permanent GuvFX onboarding (dedicated-runtime by default)

- **Status:** **APPROVED IN PRINCIPLE — Sponsor (Nuno), 2026-07-29.** Emerged from Customer Zero
  certification: the beta onboarding journey was not fully wired, and the Sponsor decided to make the
  Trusted Beta flow the single permanent onboarding rather than maintain a separate beta journey.
  Repository implementation authorised through the normal governance pipeline (tests + adversarial
  review + make check + CI + merge). **Production deployment remains a Sponsor gate** — it must pass the
  Golden Execution Reference STOP-check (routing, assignments, execution controls, runtime unchanged)
  before and after deploy.
- **Date:** 2026-07-29 · **Programme:** Customer Zero certification → permanent onboarding.
- **Supersedes/absorbs:** the per-user beta-admission gate (`is_admitted_beta_tester`) and the
  `beta_onboarding_open()` global gate as *eligibility* mechanisms. Retains ADR-0016/0017 (runtime
  isolation + on-demand tasks), ADR-0020 (multi-account routing), ADR-0019 (credential storage).
- **Relates to:** `backend/onboarding/services.py`, `backend/trading/views.py` (`perform_create`,
  `_maybe_enqueue_beta_provisioning`), `backend/terminal_provisioning/beta_capacity.py`,
  `beta_activation.py`, `backend/strategies/views.py` (arm), `backend/billing/beta.py`,
  `frontend/.../onboarding/*`, `.claude/rules/architecture.md` ("no silent architecture replacement").

## Context

Admission (`is_admitted_beta_tester`) was serving two distinct jobs: an **eligibility gate** (who may
onboard/arm/provision) and a **cohort selector** (admitted → dedicated owned-runtime path; everyone else
→ legacy shared-instance path). Combined with `beta_onboarding_open()` (default CLOSED), a normal
customer could not complete "Connect broker" (`OnboardingStepError: "Beta onboarding is not open yet."`).
Customer Zero (`beta.guvfx01@gmail.com`, genuine email verification confirmed) hit exactly this.

## Decision (programme decisions, Sponsor 2026-07-29)

1. **Trusted Beta is an operational state, not an architectural concept.** There is ONE onboarding path.
2. **Dedicated runtime provisioning is the default customer execution model.** The legacy shared-instance
   path is removed for customers (staff/Nuno path preserved).
3. **Broker validation occurs during runtime provisioning by establishing a genuine MT5 session**
   (`PROVISIONING_REQUIRE_BROKER_LOGIN=1`). The runtime's real broker login *is* the validation.
4. **Customer-visible progress is state-driven, not operation-driven** — the UI derives the current step
   from durable platform state (onboarding flags + `AccountRuntime.state`) and advances automatically.
5. **Duplicate broker submissions and provisioning requests are idempotent at BOTH the application and
   database layers** — a repeat returns the existing account/runtime, never a duplicate.
6. **Backend reason codes remain structured** (`{ok, reason, detail}`); the frontend owns wording.
7. **Customer-facing wording is owned by the frontend.**
8. **Eligibility = STAGE-SPECIFIC operational predicates, not an allowlist and NOT one universal gate.**
   Capacity / registration-open / provisioning-availability must **never** block a customer who already
   owns a runtime — those apply only to allocating a *new* runtime. Each transition has its own named
   predicate (see "Operational gate model" below).

## Onboarding state model

The onboarding state is **derived** from durable records, never stored as a single mutable "step":
`UserOnboardingState` (email_verified, risk_accepted, plan_selected, strategy_assigned, onboarding_completed)
+ the customer's `TradingAccount` + its `AccountRuntime.state`. The frontend polls
`/api/onboarding/account-status/` and renders the derived state.

```mermaid
stateDiagram-v2
    [*] --> Registered: register (registration_enabled)
    Registered --> EmailVerified: enter emailed code (genuine SMTP)
    EmailVerified --> TermsAccepted: accept risk + plan
    TermsAccepted --> BrokerAdded: submit MT5 details (idempotent create)

    BrokerAdded --> Provisioning: can_reserve_new_runtime() ✓ (auto-enqueue reserve_beta_slot)
    BrokerAdded --> CapacityBlocked: can_reserve_new_runtime() ✗ (provisioning off / unhealthy / host unreachable / capacity full)

    CapacityBlocked --> Provisioning: capacity frees (reconcile re-enqueue — runs regardless of new-reservation availability)

    Provisioning --> RuntimeReady: runtime RUNNING + MT5 broker session established (validated) [STATE-DRIVEN]
    Provisioning --> ProvisioningFailed: materialise/launch error
    Provisioning --> ValidationFailed: MT5 broker login rejected

    ValidationFailed --> BrokerAdded: customer re-enters credentials (idempotent update)
    ProvisioningFailed --> Provisioning: operator/auto retry (bounded)

    RuntimeReady --> StrategySelected: customer selects strategy [STATE-DRIVEN — no capacity/registration]
    StrategySelected --> TradingEnabled: Enable Trading (arm — ownership + validation + runtime-ready + exec-controls + arm-flag)
    TradingEnabled --> Active: dashboard reflects live state
    Active --> [*]

    note right of Provisioning
      can_reserve_new_runtime() gates ONLY the
      BrokerAdded→Provisioning edge. Every edge from
      Provisioning onward is driven by the runtime's
      DURABLE STATE — a runtime owner is NEVER blocked
      by capacity / registration / provisioning-availability.
      "Validate credentials" is fulfilled here (real MT5 login).
    end note
    note right of CapacityBlocked
      Not an allowlist — a truthful operational state for a
      user who does NOT yet own a runtime. Resolved by a
      freed/added slot; reconcile re-enqueues automatically.
    end note
```

**Legal states:** `Registered, EmailVerified, TermsAccepted, BrokerAdded, Provisioning, CapacityBlocked,
RuntimeReady, ProvisioningFailed, ValidationFailed, StrategySelected, TradingEnabled, Active`.
No transition skips a predecessor; every transition is guarded by durable state (never a client claim).

## Operational gate model (stage-specific — corrected)

There is **no universal gate**. Each transition consults only the predicates it needs. Capacity /
registration-open / provisioning-availability appear **only** on the new-runtime edge.

| Transition | Predicate(s) | Capacity? | Registration? |
|---|---|---|---|
| Create **new user account** (register) | `registration_allowed()` | no | **yes (only here)** |
| **Login** | none (any existing user) | no | no |
| Email verify / risk / plan | step prerequisites only | no | no |
| Add broker account (create record) | idempotent create; **no** eligibility gate | no | no |
| **Reserve a NEW runtime** (BrokerAdded→Provisioning) | `can_reserve_new_runtime(user)` = provisioning-enabled + provisioner-heartbeat-fresh + host-agent-reachable + **not already-owning** + capacity | **yes (only here)** | no |
| **Existing-runtime progression** (RuntimeReady, StrategySelected…) | runtime **durable state** (`runtime_ready` + `_runtime_progress_reason`) | no | no |
| Strategy selection / Enable Trading (arm) | ownership + account validation + runtime readiness + execution controls + `BETA_SELF_SERVE_ARM_ENABLED` | no | no |
| Reconciliation | BETA runtime + owner present — **runs for existing customers regardless** | no | no |

`can_reserve_new_runtime` short-circuits to `already_owned` when the user holds an active/held runtime, so
an owner is a no-op re-drive (`reserve_beta_slot` returns their existing runtime), never blocked.

## Entitlement architecture — Access → Visibility → Activation → Execution (Sponsor 2026-07-29)

The **entitlement layer OWNS** the customer-facing product boundaries; consumers (the marketplace, the
arm endpoint, the execution path) **never evaluate entitlement booleans themselves** — they ask the
entitlement layer. Four distinct, ordered layers, each owned by the entitlement layer:

| Layer | Question the consumer asks | Entitlement-owned answer |
|---|---|---|
| **Access** | May this customer open the marketplace? | `can_browse_marketplace` |
| **Visibility** | *Which marketplace **catalogues** may this customer browse?* | `visible_marketplace_catalogues` (a **frozenset of catalogue ids**) |
| **Activation** | May this customer arm/enable a visible item? | `can_assign_strategies` (permission) + operational readiness (runtime-ready, arm flag, single-tenant) |
| **Execution** | May armed automation place orders? | `can_deploy_automation` + the AUTO_DEMO master levers + per-source arm flags |

**Visibility is a catalogue, not a boolean.** Marketplace items **declare their catalogue** (an enduring
product grouping — e.g. `signal_copy` — never a rollout phase). The entitlement layer answers *which
catalogues* a plan may browse (`visible_marketplace_catalogues`, populated per plan like the existing
`historical_data_tier`). The marketplace renders items whose catalogue is in that set and embeds no
business rule. Adding a future catalogue is **data** (extend the set + tag items), never a new boolean
gate — this is why the previous `is_beta`-as-visibility-gate was replaced rather than swapped for another
boolean. Every active onboarding plan (`starter_trial`/`standard`/`pro`/`advanced`/`beta`) browses
`signal_copy`; a viewer/inactive plan browses nothing.

**`is_beta` is DEPRECATED as a visibility gate** — it survives only as a read-only cohort label (admin
view). The layers stay independent: broadening *who can see and arm* a strategy never broadens *who can
place orders* (Execution remains `can_deploy_automation` + Class-B levers, still fail-closed for beta).
This also removed the flow contradiction where the mandatory plan-selection step (only `standard`/
`starter_trial` selectable) stripped the `is_beta` the strategy step required, stranding every registrant.

## Architectural changes (minimum set)

**Backend**
- `billing/beta.py`: **stage-specific predicates** — `registration_allowed()`, `provisioning_service_healthy()`
  (kill-switch + `_provisioner_heartbeat_fresh()`), `host_agent_reachable()`, `runtime_capacity_available()`,
  `user_holds_runtime()`, `can_reserve_new_runtime(user) -> (ok, reason)`. `is_admitted_beta_tester` /
  `BetaTester` retained but **never consulted for eligibility**; `beta_onboarding_open()` demoted to
  staff/back-compat only.
- `onboarding/services.py`: `mark_account_connected` (non-staff) is **pure state-driven** via
  `_mark_beta_runtime_ready` → `_runtime_progress_reason(runtime)` (no capacity/registration gate);
  `mark_strategy_assigned` governed by the existing `StrategyAssignment` only. `_apply_beta_admission`
  retired. **Staff/Nuno legacy path unchanged.**
- `trading/views.py`: `perform_create` non-staff branch is **idempotent** (`get_or_create` on the
  normalised key; existing account returned, provisioning re-driven idempotently) + dedicated-runtime
  default; `_maybe_enqueue_beta_provisioning` drops admission, guards a duplicate PROVISION enqueue.
  **Staff branch unchanged.** New-runtime allocation is authoritatively gated + enforced atomically in
  `reserve_beta_slot`; `can_reserve_new_runtime` is the entry pre-check that surfaces the reason.
- `terminal_provisioning/beta_activation.py` + `reconcile_beta_provisioning`: drop the per-user admission
  check (chokepoint keeps `beta_runtimes_enabled` + `cohort==BETA` + owner + cap). Reconcile runs for any
  owned BETA runtime.
- **PR B only:** `PROVISIONING_REQUIRE_BROKER_LOGIN=1` — provisioning establishes a real MT5 session;
  `validation_status`/`broker_connected` become truthful.
- Structured reason codes: `registration_closed, provisioning_disabled, provisioner_unhealthy,
  host_unreachable, capacity_full, runtime_pending, runtime_provisioning, capacity_blocked, runtime_failed,
  runtime_not_ready, validation_failed, no_broker_account`.

**Frontend**
- Onboarding orchestration: create account (idempotent) → poll `account-status` → advance when the runtime
  is ready; render a "Validating & provisioning…" state; map reason codes to friendly copy.

## PR boundaries (jointly certified — PR B not deferred)

**PR A** — admission removal + permanent dedicated-runtime path + account/provisioning idempotency +
DB constraints (+ normalisation) + state-driven frontend orchestration + structured reason codes + friendly
errors + full tests **including Golden-Reference preservation**. No host risk beyond existing provisioning.

**PR B** — genuine MT5 broker-login validation during provisioning (`PROVISIONING_REQUIRE_BROKER_LOGIN=1`),
isolated host verification, failure/retry/timeout/credential-safety tests, **no-order proof**, rollback
evidence. A **separate bounded risk packet/PR** — but **part of the Customer Zero release**.

**PR A MUST NOT deploy independently as the certified permanent onboarding.** PR A and PR B are merged and
**jointly certified**, then STOP at the production-deployment gate for Sponsor approval (Golden-Reference
STOP-check before + after).

**Frontend-divergence deploy-gate item.** The state-driven `AccountConnectionStep` is authored in the
**local Git repo** (the canonical PR-A source). The deployed prod frontend lives in a separate tree
(`/home/ubuntu/guvfx-prod/frontend`) that has diverged from the repo (e.g. an earlier hot-fix that added
broker fields to the connect step). Reconciling the repo's PR-A frontend onto the deployed tree is a
**deploy-gate task** — performed only at Sponsor go-live, never as a live edit during PR A. No prod
frontend file is touched by PR A.

**Test-fixture consequence of `brokeridentity_present`.** Tightening the "every account has a broker
identity" invariant into a DB constraint surfaced a large volume of pre-existing test fixtures that
created bare accounts (no `broker_server`, empty `broker_name`). PR A adds a `broker_name` to those
incidental fixtures across the suite (mechanical, additive). Production data was audited clean (0 rows),
so no production remediation is required — only test data.

## Migration / normalisation plan (idempotency constraint)

**Order of operations (all in PR A):**
1. **Audit first (read-only):** query for existing duplicates by the normalised key before writing any
   constraint — `GROUP BY user, TRIM(account_number), broker_server, TRIM(broker_name) HAVING COUNT(*)>1`.
   Expectation: none (Customer Zero's account #11 is the only customer account; Nuno's are staff/separate).
   If any surface, resolve explicitly (keep the active/newest, deactivate the rest) — never silently.
2. **Normalise on write:** `account_number` and `broker_name` are `strip()`-normalised at intake
   (serializer/`perform_create`) so the stored value is canonical. **Case is preserved** — MT5 server
   names are case-sensitive; normalisation is whitespace-only. The idempotency lookup uses the same
   normalised values.
3. **Null-safe conditional uniqueness** — two **partial** `UniqueConstraint`s (Postgres treats NULLs as
   distinct, so a single constraint spanning a nullable FK would not enforce the free-text path). These
   two constraints **already existed** on `TradingAccount`; PR A relies on them as the DB-layer backstop:
   - `uniq_user_brokerserver_accountnumber` — `(user, broker_server, account_number)` `WHERE broker_server IS NOT NULL`.
   - `uniq_user_brokername_accountnumber` — `(user, broker_name, account_number)` `WHERE broker_server IS NULL AND broker_name <> ''`.
   PR A **adds** a `CheckConstraint` `brokeridentity_present` — `broker_server IS NOT NULL OR broker_name <> ''`
   — closing the NULL-FK + empty-name hole that would otherwise evade both partial constraints (trading
   migration `0013`). Prod audit found ZERO incompatible rows; the migration carries a `RunPython` pre-check
   that aborts loudly if any exist.
4. **Exactly-one active provisioning job** — a partial `UniqueConstraint`
   `uniq_active_job_per_runtime_op` on `ProvisioningJob (runtime, op) WHERE status IN (QUEUED, RUNNING)`
   (terminal_provisioning migration `0010`) makes "at most one active job per (runtime, op)" a DB fact;
   `enqueue_op` recovers the winner on the resulting `IntegrityError`. Its migration also carries a
   `RunPython` duplicate-abort pre-check.
5. **Data-preserving + reversible.** Each new constraint's reverse drops only the constraint.

**Idempotency proofs (tests in PR A):**
- Repeated submission of the same `(user, login, broker)` returns the **existing** account (200/201, same id)
  and the **existing** provisioning state — no second account, no second slot, no duplicate PROVISION job.
- Concurrent submissions cannot duplicate — proven by a genuine real-thread `TransactionTestCase`
  (`ConcurrentCreateTests`): `perform_create` does **canonical normalisation → existing-account lookup →
  transactional create → IntegrityError winner recovery** and does **NOT depend on a row lock**; the DB
  partial-unique constraints are the serialisation point, and the winner is recovered and returned
  idempotently. Result: exactly one account, one runtime, one active PROVISION job, same resolved state.

**Gate-removal migrations:** none (code-only). `BetaTester` rows retained; cohort unchanged (`BETA` remains
the customer cohort, `PRODUCTION`/Nuno untouched); cosmetic rename deferred.

## Compatibility with the Golden Execution Reference — LOW risk
- Nuno is staff/superuser → bypasses onboarding gates before and after (staff branches preserved).
- All capacity/activation gates are `cohort==BETA`-scoped → never touch Nuno's `PRODUCTION` runtime.
- Routing/execution untouched (ADR-0020 fan-out already Golden-preserved).
- **Implementation guardrail:** the deploy must pass the Golden Reference baseline STOP-check
  (routing, assignments, execution controls, runtime, 0 unexpected positions/orders) + an explicit
  "Nuno path unchanged" test. STOP on any structural change.

## Consequences
- **Simplifies public launch:** one path; capacity/health throttle replaces the allowlist bottleneck;
  idempotency + friendly errors + state-driven UI are production-quality prerequisites paid early.
- **Remaining public-launch gap (out of scope, flagged):** real billing/entitlement + payment (a BETA
  plan is auto-granted today). This ADR neither touches nor blocks it.
- **Reversible:** re-introducing an eligibility gate is a one-line change to `onboarding_available()`.

## Terminology (permanent)
The legacy beta-era names are now the **standard dedicated customer-runtime path**, not a separate
architecture: `cohort=BETA` = the standard customer runtime cohort; `BETA_MAX_TESTERS` = the customer
runtime-capacity cap; `reserve_beta_slot` / `beta_activation` / `beta_worker` = the standard
dedicated-runtime reservation + activation + provisioning path. **"Trusted Beta" is an operational
rollout state, not an eligibility or onboarding architecture.** No admission check may be reintroduced
through these legacy names. (A cosmetic rename to customer-neutral identifiers is deferred, non-blocking.)

## Alternatives considered
- *Keep a separate beta journey* — rejected: dual-path tech debt; allowlist doesn't scale to public.
- *Broker-independent provisioning (no validation)* — rejected: "credentials validated" would be untrue.
- *Pre-provision shared validator* — rejected: more moving parts than validating via the real runtime.
