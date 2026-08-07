# 0033 — Hosted Persistent MT5 Workspace

- Date: 2026-08-07
- Status: Accepted with conditions

## Acceptance (2026-08-07)

Accepted with conditions after an independent red-team of the Model-B readiness contract. Binding
conditions:

1. Attach-verified readiness replaces **only** `password_enc` + `VALIDATED`, and is **ANDed with** (never
   substituted for) the lifecycle checks. *Implemented — Increment 2, `execution/readiness.py`.*
2. The order-time bridge gate is **strengthened**: a **mandatory identity pin** (payload-sourced, demo +
   live; enforceable as a terminal property via `MT5_REQUIRE_IDENTITY_PIN`) and the fresh re-verify
   relocated to **immediately before `order_send`** (TOCTOU narrowing). *Implemented — Increment 2,
   `scripts/mt5_signal_bridge.py`.*
3. Per-user dedicated routing + authenticated owner-bound observations + server-side pin derivation.
   *Contract specified (Increment 2 docs); wiring is a later increment — Tension 2.*
4. DARK until the 16-item disposable-host pilot + RULE-11 NTFS-ACL certification pass.
5. Commercial RDS/licensing gate resolved before any customer rollout.

Increment 1 (PR #301, merged) = the DARK foundation. Increment 2 = the readiness abstraction + hardened
order-time gate (this repository, DARK). See `docs/architecture/EXECUTION_READINESS.md`.

## Context

The MT5-IPC investigation converged on one fact: `mt5.initialize(path=...)` **attach** to an
already-broker-connected terminal yields reliable IPC (proven daily by `scripts/mt5_signal_bridge.py`),
whereas every fresh cold launch fails `-10004/-10005`. The proposed Hosted product model builds on this:
each Hosted user gets a **persistent, user-owned, portable MT5** they log into themselves; GuvFX only
**attaches** to observe which broker account is active, and permits a strategy to operate **only** when
the active account matches the strategy's bound account. GuvFX never receives/stores/transports the
broker password and never calls `mt5.login()`.

This ADR covers the **Phase-1 foundation increment** (a new DARK backend app) and records the
architecture decisions the execution-facing increments require. Full design: [design doc](../architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md).

## Verified facts

- `evaluate_binding` (`scripts/mt5_signal_bridge.py:225`) is the pure, fail-closed, **mutation-tested**
  order-time gate; it already enforces `connected`→`trade_allowed`→`login` pin→`server` pin→demo/live.
- The per-account OneToOne sidecar pattern is established: `AccountRuntime`, `BrokerAccountHealth`,
  `BrokerRuntimePause` all hang off `trading.TradingAccount`.
- `AccountRuntime` is GuvFX-owned/headless/beta-provisioned (guvfx_u_<id>); it is **not** a user-owned
  attach model (`terminal_provisioning/models.py:157`).
- The central backend gate is `execution/broker_gate.py` at `ExecutionJob.save` + final dispatch; it
  REQUIRES `password_enc` present and `validation_status == VALIDATED` (`broker_gate.py`).
- TX-1 provisioning has **no** NTFS ACL step; the ACL idiom exists only on the beta side
  (`deploy/beta-agent/install_pool.ps1::Grant-GuvfxServiceRead`).
- `Populate-GuvfxViewerRuntime.ps1` writes `Login=0`, `AllowLiveTrading=0` and **deletes** `accounts.dat`;
  golden/RULE-10 refuses any `accounts.dat`.
- Backend cannot import MetaTrader5; observation is host-side + HTTP. MetaTrader5 is a process-global
  singleton (one terminal per process).
- Flag Idiom B (`billing/beta.py::_flag`) is the read-live, settings-then-env, DARK-default pattern.

## Assumptions

- Attach-to-broker-connected IPC generalises from the single production bridge terminal to per-user
  persistent terminals (to be proven by EXP-1 on a disposable host — a **manual** broker login).
- MT5 auto-reconnects from its saved encrypted creds after a reboot (unverified; needed for recovery
  without GuvFX ever holding the password).
- Per-user NTFS ACL hardening + RemoteApp/AppLocker can isolate multiple users on one RD host
  (to be proven with RULE-11 positive+negative controls on a disposable host).

## Decision drivers

Safety (fail-closed execution), reversibility (DARK/additive), scope discipline (no whole-subsystem
rewrite in one PR), governance (no silent Amber change to gates/security), and separation of concerns.

## Options considered

- **A — Extend `AccountRuntime`.** Cons: conflates GuvFX-owned provisioning with user-owned attach;
  risks the integrity-pinned `terminal_provisioning` app; overloads one state field with two lifecycles.
- **B — New sibling model in a new `hosted_workspace` app.** Pros: clean separation (mirrors
  `BrokerRuntimePause`), keeps the new subsystem out of the integrity-pinned app, cleanly DARK. Cons:
  one more app + migration.
- **C — Build the full A–P subsystem in one PR.** Cons: violates small-diffs / no-whole-subsystem-rewrite;
  bundles execution-safety wiring that needs the tensions resolved first; not reviewable/green in one pass.

## Decision

Adopt **Option B** and **decompose Phase 1**. The first increment ships the inert, additive, DARK
backend foundation: the `HostedMt5Workspace` sibling model + a pure, mutation-tested
`evaluate_active_account_match` + three DARK flags + inventory + tests. The order-time boundary remains
`evaluate_binding` in the bridge; **no** backend gate is wired in this increment. The four design
tensions (attach vs `password_enc`/`VALIDATED` preconditions; single-tenant routing; process-level pin
vs multi-tenant; `accounts.dat` vs RULE-10) and the licensing gate are recorded here and **must be
resolved by an approved decision** before the execution-facing increment. The Windows host tooling
(ACL/AppLocker/supervision/RemoteApp/provisioning) is PLAN-only in repo and host-certified only via a
Sponsor-gated disposable-host pilot (RULE-11).

## Consequences

- A reviewable, green, reversible foundation lands now; the risky execution wiring is explicitly gated.
- Follow-ups: (1) resolve tension #1 (attach-verified readiness) → wire the live active-account-match at
  `evaluate_dispatch_gate` behind `HOSTED_PERSISTENT_MT5_ENABLED`; (2) host probe + poller; (3) ACL
  package + RULE-11 cert; (4) RemoteApp adapter; (5) API/onboarding/observability; (6) licensing.

## Risks and controls

- **Hard blocker (security):** no per-user NTFS ACL in TX-1 → a multi-user host leaks `accounts.dat`.
  Control: ACL hardening + RULE-11 proof before any multi-user pilot. **Not in this increment.**
- **Red:** wiring active-account-match into the execution gate is execution-safety-critical → deferred
  behind a DARK flag + this ADR's acceptance; the pure matcher is fail-closed and mutation-tested.
- **Amber:** new app/migration — additive, DARK, reversible (drop table while OFF).

## Evidence / validation

- `backend/hosted_workspace/` tests: **22 passed** (`manage.py test hosted_workspace`), including
  `MutationAdequacyTests` (every comparison/boolean/`not` operator mutant in the matcher killed) and the
  RULE-11 non-vacuous-oracle positive control.
- `makemigrations --check` for `hosted_workspace`: clean (the reported drift is pre-existing in
  `strategies`, not introduced here).
- Full `make check` result recorded in the PR. Not covered: any host behaviour (ACL, attach, RemoteApp,
  reboot) — all Sponsor-gated / disposable-host.

## Reversal path

Unset the flags (read-live; immediate). The `HostedMt5Workspace` table is additive and unread while
DARK; migration `hosted_workspace 0001` can be reversed to drop it. No existing behaviour changes.

## Revisit trigger

Sponsor decision on the licensing model and the four design tensions; or EXP-1 (disposable-host
attach) failing to reproduce the bridge's positive control.

## Approval

PM owns lifecycle status. **Amber** (new app/migration) proceeds as additive-DARK. The **Red** items
(execution-gate wiring, host ACL/RemoteApp, licensing) require explicit Sponsor approval and are NOT
taken in this increment.

---

## Transition Amendment (PROPOSED — 2026-08-07) — persistent workspace as the readiness authority

Status: **Proposed. Pending Sponsor (PM) acceptance.** No behaviour change, no flags armed. Companions:
[Hosted Workspace Roadmap](../architecture/HOSTED_WORKSPACE_ROADMAP.md) and — after the 2026-08-07 Programme
Architecture Reset — **[ADR-0034 Hosted Workspace Operating Model](0034-hosted-workspace-operating-model.md)**,
which becomes the architectural source of truth for the Hosted Workspace platform (this ADR-0033 remains the
DARK foundation + eligibility-transition decision it builds on).

**Context.** The MT5 IPC investigation is closed. Experiments A–I technically validated the persistent,
attach-only (never-login, never-own-credentials) workspace: attach to a user-logged-in broker-connected
terminal (same- and cross-session), survives RDP disconnect, requires a broker connection (cold → `-10005`),
and — decisively — `initialize(path=)` is **dual-mode** (it *relaunches + auto-logs-in from cached
`accounts.dat`* if the terminal is down). The attached session ran `order_check` (retcode 0) and observed a
full manual trade lifecycle without GuvFX holding credentials; `order_send`-via-attach already runs in the
production `:8788` bridge.

**Decision (proposed).** Adopt the six-workstream transition roadmap that moves hosted accounts from the
ADR-0027 temporary broker-login validation model to the **persistent attach-only workspace** as the
*readiness authority*. The **WS3 guarded-attach primitive** — a never-launch, assert-connected attach that
neutralises the proven dual-mode relaunch/auto-login hazard — is mandated as the **first** engineering
increment.

**What it supersedes.** For accounts on `readiness_provider='persistent_workspace'` it supersedes ADR-0027's
login-based *eligibility* (`password_enc` + `validation_status==VALIDATED`) **only** — replaced by a fresh
(≤`WORKSPACE_OBSERVATION_FRESH_SECONDS`=300 s) positive attach observation ANDed with
`is_active`/`disconnected_at` (binding condition 1).

**What it does NOT supersede.** The order-time `evaluate_binding` gate (strengthened with the mandatory
`MT5_REQUIRE_IDENTITY_PIN`), the central `broker_gate`, and the lifecycle/health/pause checks remain for
*both* providers; `observed_*` stays a **cache** that can never alone authorise an order. Retirement is
**per-account and never automatic** (`readiness_provider` is never auto-converted); Provider A + the ADR-0027
stack stay live for every un-migrated account. Append-only `BrokerAccountValidationAttempt` history and
disconnect TOMBSTONE rows are **retained**.

**Conditions that remain open** (unchanged authority; carried into the roadmap gates):
- **Condition 3** — per-user dedicated routing + authenticated owner-bound observations + server-side pin
  derivation (Tension 2, still contract-only/unwired).
- **Condition 4** — the 16-item disposable-host pilot + RULE-11 per-user NTFS-ACL/AppLocker positive+negative
  certification (**TX-1 `Provision-GuvfxAccount.ps1` currently applies NO ACL — the hard security blocker**).
- **Condition 5** — the commercial RDS/SPLA licensing gate.
- New correctness requirement: `last_observed_at` must be written **atomically** with the `observed_*`
  snapshot (else a stale snapshot rides a fresh timestamp through the freshness bound).

**MVP boundary (proposed).** Milestone M6 — single-tenant, **dedicated-host-per-user**, demo-only,
attach-only. Dedicated hosting has no cross-tenant filesystem surface, so the MVP does **not** depend on the
still-missing multi-user NTFS ACL (deferred to M7). Reversal path unchanged: unset the flags (immediate) +
drop the additive `hosted_workspace 0001` table.

**Approval.** PM owns lifecycle status; this amendment stays **Proposed** until Sponsor acceptance. Arming any
hosted-workspace flag and every host/execution/licensing step remains **Red**.
