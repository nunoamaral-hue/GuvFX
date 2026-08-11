# 0034 — Hosted Workspace Onboarding & Provisioning Subsystem

- Date: 2026-08-09
- Status: Proposed (DARK, repository-complete) — PM owns lifecycle status
- Umbrella: ADR-0034 Hosted Workspace (M-series state core + Execution Engine + **this Onboarding
  subsystem** + Workspace Delivery). See `docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md` and
  `docs/ADRs/0033-hosted-persistent-mt5-workspace.md`.
- Integration note (2026-08-09): originally authored on a branch that predated Workspace Delivery. Now
  **integrated onto merged Workspace Delivery (#316)**: `delivery_readiness` reads the real
  `HostedMt5Workspace.delivery_state` (D6); node allocation assigns BOTH node authorities — `execution_node`
  (order routing) and `workspace_node` (RemoteApp delivery) — to the customer's single host as two explicit
  facts (D8); and the separate `owner` FK was **removed** as a simplification, unifying ownership on the
  immutable `trading_account.user` (D9). See the "Integration" decisions below.

## Context

The M-series (M1–M3c) and the Execution Engine gave the Hosted Workspace a certified **state core**: a
single authoritative writer (`persist_workspace_decision`), a fail-closed lifecycle state machine, a
certified active-account matcher, and an order-time bridge gate. What was missing was the **customer
journey that produces those workspaces** — the repository-side path from *"a user signs up"* to *"a
confirmed, ready workspace eligible for strategy assignment."*

This subsystem builds that journey, end to end, entirely DARK. It is **workspace-centric**: the customer
logs into their **own** MT5 with their **own** broker credentials; GuvFX only **observes**. No broker
password ever enters the GuvFX backend on this path.

## Scope

**In scope (built, DARK):** entitlement + admission predicate; idempotent workspace request creating an
INTENT-only account (no password); node allocation contract; provisioning orchestration
(`PROVISIONING → WAITING_FOR_LOGIN` via the certified writer); customer-facing journey projection; DARK
customer API (journey / request / confirm) + staff ops fleet API; broker-account confirmation (human ACK)
gated on an observed match; a readiness `workspace_confirmed_at` precondition; tiered strategy-assignment
eligibility (**assignment < armed < order-authorised**); degrade-closed delivery-readiness projection;
telemetry; customer-safe errors; feature flags; the full test bar.

**Out of scope (unchanged / deferred, Sponsor/host-gated):** enabling production trading; placing any
order; RDS install / licensing / CAL; production Windows-user creation; production RemoteApp publishing;
multi-user host pooling; AppLocker; destructive deletion; live rollout. Provider-A (`temporary_validation`)
and Customer Zero are untouched and never auto-converted.

## Decisions

- **D1 — Workspace-centric, password-free (load-bearing product invariant).** No `password` /
  `broker_password` / `mt5_password` field, parameter, or request body is accepted or stored anywhere on
  the hosted-workspace path. The request orchestrator has **no** password parameter (structurally tested),
  and the customer API rejects any password-bearing body outright (`REQ_PASSWORD_FORBIDDEN`). The customer
  supplies only broker **identifiers** (expected login/server); confirmation is gated on an **observed**
  active-account match, never a client-asserted identity.

- **D2 — One workspace per user, idempotent, concurrency-safe.** `request_hosted_workspace` serialises on
  the user row (`select_for_update`) and returns the existing workspace on re-request. Allocation reserves
  node capacity atomically (candidate `select_for_update`, no over-fill) and is idempotent; a retry
  *converges* a workspace stuck at `PROVISIONING` yet **never regresses** one that has already progressed.
  One-workspace-per-user is enforced through the immutable `trading_account.user` binding (see D9) — there is
  no separate ownership column that could drift.

- **D3 — Canonical state advances ONLY through the certified single writer.** The orchestrator drives
  `PROVISIONING → WAITING_FOR_LOGIN` by feeding a synthetic observation through `derive_workspace_decision`
  + `persist_workspace_decision` (same `observation_version + 1` contract the production observation runner
  uses, so the two serialise via the writer's stale-guard — loser is `REJECTED_STALE`, never corruption).
  No second writer is introduced.

- **D4 — Confirmation is a human gate that strictly NARROWS eligibility.** A new readiness precondition
  (`RW_NOT_CONFIRMED`) requires the customer's durable ACK (`TradingAccount.workspace_confirmed_at`) before
  a Provider-B account can ever become execution-ready. It is placed **after** the observed-match check
  (so it is only reached once there is a real account to confirm, and both reason codes stay reachable) and
  can only make readiness **more** restrictive — never less. Provider-A is untouched.

- **D5 — Three hard tiers: assignment-eligible < armed < order-authorised.** The strategy-assignment
  eligibility projection reports `assignment_eligible` (confirmed + connected + matched) as the LOWEST
  tier; `armed` (`execution_enabled` + canonical `EXECUTION_READY`) as a strictly higher, separately-owned
  fact reported read-only; and NEVER asserts order authority — the order-authorisation tier is reported as
  an external live gate (`external_live_gate`). Persisted state remains read-model only; the live bridge
  gate is the sole order-time authority.

- **D6 — Degrade-closed delivery-readiness (reconciled with merged #316).** The delivery projection reads
  the REAL `HostedMt5Workspace.delivery_state` (owned by the Workspace Delivery single writer) but never
  fabricates readiness: only a genuinely `CONNECTED` RemoteApp is `DELIVERY_READY`; `AUTHORIZED`/`DISCONNECTED`
  ⇒ `DELIVERY_PREPARING`; OFF ⇒ `NOT_AVAILABLE`; and flag-ON but undelivered (`NONE`/`FAILED`) ⇒
  `EXTERNAL_GATE`, because the real RemoteApp host (RDS) is a Sponsor/host gate that is not installed.
  Delivery is NEVER execution: this signal is read-model only and can never authorise an order. Delivery
  reads only `workspace_node`/`delivery_state`; it never reads `execution_node`.

- **D7 — Fully DARK + invisible.** Every endpoint 404s (before any DB read) unless **both** the master
  (`HOSTED_PERSISTENT_MT5_ENABLED`) and onboarding (`HOSTED_WORKSPACE_ONBOARDING_ENABLED`) flags are ON;
  both default OFF. Admission is a fail-closed AND of both flags + the durable `can_use_hosted_workspace`
  billing capability. Reads/writes are owner-scoped (IDOR-safe); the ops fleet is staff-only and
  404-invisible to non-staff. POST is CSRF-enforced on the cookie-auth path.

### Integration decisions (added 2026-08-09, on merged Workspace Delivery #316)

- **D8 — Node allocation assigns BOTH node authorities; delivery and execution never read each other.**
  In the single-host persistent-workspace model the customer's ONE host serves BOTH order execution and
  RemoteApp delivery. `allocate_workspace_node` therefore records **two explicit, separately-owned facts** on
  the same host under one lock: `execution_node` (order routing, via the certified execution assignment) and
  `workspace_node` (RemoteApp delivery, via the delivery single writer). This is a **second authority
  assignment, not a fallback**: execution reads only `execution_node`, delivery reads only `workspace_node`,
  and neither reads the other. A RemoteApp connection never grants execution readiness; execution readiness
  never implies delivery. The two families (canonical state · execution binding · delivery state) stay
  distinct — never collapsed.

- **D9 — Ownership is the single immutable fact `trading_account.user`; the separate `owner` FK was removed.**
  The Onboarding foundation originally added a nullable `owner` FK plus a `save()` coupling guard requiring
  `owner == trading_account.user`. The simplification review (packet §18) found this to be a **duplicate
  durable truth**: the workspace↔account binding is already immutable (guarded in `save()`), so
  `trading_account.user` is a single, drift-proof owner — and the Workspace Delivery authority already derived
  ownership that way (`workspace.trading_account.user_id`), leaving onboarding the only consumer of the second
  column. The `owner` FK, its migration, and the coupling guard were **removed**; entitlement, provisioning,
  the confirm owner-check, and both staff projections now resolve ownership through `trading_account.user`.
  Result: one source of truth, no bulk-update path that could bypass the coupling guard, and consistency with
  the delivery authority. `makemigrations --check` for `hosted_workspace` is clean with no `owner` migration.

## Security / governance boundary

- **Green:** additive DARK modules (`entitlement`, `provisioning`, `onboarding_read_model`, `eligibility`,
  `onboarding_ops`, `onboarding_views`), read-model projections, telemetry, tests.
- **Amber (documented):** the `execution/readiness.py` confirm precondition touches the shared readiness
  gate — additive and strictly narrowing, DARK (Provider-B only runs when both flags are ON, and no prod
  account is Provider-B). Node allocation now writes the `workspace_node` delivery authority in addition to
  `execution_node` (D8) — both on the same host, additive, DARK. No `owner` migration is added: ownership is
  derived from the immutable `trading_account.user` (D9).
- **Red (NOT taken):** arming execution, placing an order, host/RDS/licensing, production RemoteApp,
  multi-user pooling, live rollout — all Sponsor/host-gated and explicitly out of scope.

## Evidence / validation

**At the integrated head (Onboarding rebased onto merged #316, `owner` FK removed):**

- Focused suites: `hosted_workspace` (whole app) — **325 passed**; `execution` + `billing` — **963 passed**
  (both node authorities + EE routing/claim + confirm-precondition fixtures). No test weakened for the
  integration; the owner-coupling tests were reframed as derived-ownership tests (`OwnershipTests`).
- `make check`: backend **3460 OK** (`manage.py test`, 221s) · frontend **lint 0 errors** (19 pre-existing
  warnings) · **build compiled successfully** (38/38 static pages).
- `makemigrations --check` for `hosted_workspace`: **clean** — model matches migrations `0001–0005`, no
  `owner` migration, no drift. (Pre-existing index-rename drift in unrelated apps — backtests / reliability /
  signal_intake / strategies — predates this work, is untouched here, and is not gated by `make check`.)

**Foundation branch (pre-integration) review, retained for provenance:**

- Adversarial review + completeness audit (multi-agent, find → adversarially-refute, loop-until-dry):
  **Round 1** = 0 HIGH / 2 MEDIUM (the SAME defect, found by two dimensions) / 2 LOW — all fixed:
  (M) `allocate_workspace_node` capacity gate was fail-open — `TerminalNode.has_capacity` counts only
  `is_active=True` accounts, but hosted intent accounts are `is_active=False`, so a node never registered its
  hosted bindings and over-subscribed → fixed with `_node_has_capacity` counting bound Hosted Workspaces under
  the node lock (+regression test); (L) the API password-rejection guard was a shallow substring check → made
  recursive over a broadened secret-token set (+2 tests); (L) an active-account mismatch
  (`SUSPENDED/ACCOUNT_MISMATCH`) collapsed to the generic "contact support" bucket → now surfaced as
  `PHASE_BROKER_CONNECTED` "switch your active account" (+2 tests). **Round 2** (re-run against the fixed
  code) = **0 surviving findings** across all six dimensions — converged.

**Integration-head review (10 lenses, find → adversarially-refute; multi-agent):** IDOR / secret-ingestion /
duplicate-ownership / allocation-concurrency / delivery-execution-confusion / confirm-forgery /
migration-integration / DARK-bypass / second-writer / simplification-regression — **0 surviving HIGH, 0
surviving MEDIUM**. Nine lenses returned nothing; one raised a single **LOW** (below), refuted as
non-blocking and not a regression. Every HIGH/MEDIUM candidate was handed to an independent refuter; none
survived.

- **Documented LOW (pre-existing, not introduced here):** one-workspace-per-user is enforced in the
  onboarding **request** path (`request_hosted_workspace`: user-row lock + `filter(trading_account__user=…)`),
  not by a DB constraint. The operator-driven Execution-Engine path
  `execution.hosted_provisioning.provision_hosted_workspace` does `get_or_create(trading_account=account)`
  per-account with no per-user check — so a staff operator could provision a second workspace on a **second
  broker account of the same user**. This is **by design** (an operator binds a node to a specific account's
  workspace) and is **not a regression from removing the `owner` FK**: that path never referenced `owner`, and
  the FK was nullable with no unique constraint, so it never enforced per-user uniqueness. Every workspace
  remains strictly owner-scoped via `trading_account.user` (no cross-user access). Recorded as technical debt;
  a future DB-level guarantee (if wanted) would be a partial unique index on the user across hosted accounts,
  not a resurrected column.
- **Not covered (stated limitation):** any host behaviour (RDS, RemoteApp, attach, reboot) — Sponsor/host
  -gated; the `workspace.account_discovered` event is defined in the taxonomy but its emit belongs to the
  certified observation chain (out of this subsystem's boundary) and is surfaced today via the journey
  read-model phase.

## Reversal path

Unset either flag (read-live; immediate) — every endpoint 404s and Provider-B readiness fail-closes. The
integration adds no `owner` migration (ownership is derived from `trading_account.user`); the only Onboarding
migrations are those already carried by the state core / delivery (`hosted_workspace 0001–0005`). No existing
behaviour changes while DARK; Provider-A / Customer Zero are untouched.

## Revisit trigger

Sponsor authorises the next subsystem, or the licensing/RDS gate resolves, or Workspace Delivery (#316)
lands (activating the `#316`-seam in `delivery_readiness`).

## Approval

PM owns lifecycle status. The **Amber** items (readiness confirm precondition, owner FK) proceed as
additive-DARK. The **Red** items require explicit Sponsor approval and are NOT taken here.

## Amendment (2026-08-11) — Hosted-capability / commercial-plan decoupling

**Context.** During the Stream 9 autonomous-onboarding certification, an admitted Hosted Beta tester
(`BetaTester` allowlist, active) who registered through the normal public flow and verified their email was
routed to the legacy Broker Accounts form, not the hosted journey. Root cause: the Hosted Workspace
capability `can_use_hosted_workspace` was granted **only** by the `beta` commercial plan
(`billing/entitlements.py`), so a tester who self-selected any other commercial plan (e.g. `standard`) at
registration was denied (`DENY_NOT_ENTITLED`) even though they were an admitted programme member. The Hosted
Beta programme was thereby coupled to a single commercial-plan value — a plan the customer chooses for
billing reasons, not for beta admission.

**Decision.** Hosted Workspace capability becomes **independent of the commercial subscription**. A single
predicate `hosted_workspace.entitlement.has_hosted_workspace_capability(user)` returns the **fail-closed OR**
of two separate sources:

- the durable **commercial** entitlement `can_use_hosted_workspace` (a plan may still grant it), **OR**
- active membership of the **Hosted Beta programme** (the `BetaTester` admission allowlist,
  `is_admitted_beta_tester`).

The billing entitlement engine stays **commercial-only** (unchanged); `hosted_workspace` composes the two
concerns. Both capability derivation sites move to the shared predicate: `hosted_workspace_admission`
(entitlement.py) and the `CHECK_ENTITLED` gate in `strategy_assignment_eligibility` (eligibility.py), so
admission and assignment-eligibility stay consistent.

**Invariants preserved (no broadening; still fail-closed):**

- Every caller still ANDs the two DARK flags (`HOSTED_PERSISTENT_MT5_ENABLED` +
  `HOSTED_WORKSPACE_ONBOARDING_ENABLED`, default OFF) on top of the capability — capability alone never
  admits, and the change is inert in production while dark.
- The new source admits **only** the active, operator-controlled `BetaTester` set (empty by default; an
  inactive row does not admit). Paid users not in the programme are unaffected.
- The commercial plan is **never modified** to grant hosted access (the certification identity keeps its
  `standard` plan). Access comes from the programme, not the plan.
- Capability is Access/Visibility only — it grants **no** order authority (`can_deploy_automation` and the
  live bridge gate are untouched). Customer-Zero/staff exclusion remains in the setup router.

**Reversal.** Pure code predicate behind the same DARK flags; no migration. Reverting the OR (or removing the
`BetaTester` source) restores plan-only capability. Unsetting either flag disables the whole path as before.
