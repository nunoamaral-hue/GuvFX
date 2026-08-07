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
