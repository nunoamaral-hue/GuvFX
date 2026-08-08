# Hosted Persistent MT5 Workspace — Design & Repository-Truth Note

- Status: **Phase-1 foundation increment delivered (DARK); full Phase 1 is multi-increment and gated.**
- ADR: [ADR-0033](../ADRs/0033-hosted-persistent-mt5-workspace.md)
- Scope of the delivered increment: repository engineering only. **No production deploy, no RDS
  install, no licence purchase, no customer/Windows-user creation, no live-trading change, no merge
  into the Visible-MT5 stream.**

This document is the Workstream-A repository-truth note **and** the Workstream-P design record for the
persistent-workspace stream. It describes the target architecture across all workstreams (B–O), what
the current codebase already provides, the design tensions that must be resolved by an approved
decision before the execution-facing wiring is built, and exactly what the **first increment** ships.

---

## 1. Concept

```
Hosted user → persistent portable MT5 (user logs into their broker themselves) →
GuvFX ATTACHES (mt5.initialize(path=...), never login) → observes the active broker account →
strategies may operate ONLY when the active account == the strategy's bound account (fail closed).
```

GuvFX never receives, stores, or transports the broker password, and never calls `mt5.login()`. This
is *stronger* than the current ADR-0027 backend-seal credential model. It is also the resolution of the
whole MT5-IPC investigation: the production bridge already proves that **attach-to-an-already-connected
terminal yields reliable IPC**, whereas every fresh cold launch fails `-10004/-10005`.

---

## 2. Repository-truth note (Workstream A): reuse / extend / new

Verified by reading the current code (Django 5.1 backend). Citations are `path:symbol`.

| Concern | Existing primitive | Classification | Note |
|---|---|---|---|
| Per-account durable sidecar | `terminal_provisioning.AccountRuntime` (OneToOne TradingAccount) | **REFERENCE** | GuvFX-owned/headless/beta-provisioned. The workspace is USER-owned/attach — a **sibling**, not an extension (mirrors the `BrokerRuntimePause` "own model, don't overload a lifecycle" precedent). |
| Broker account identity | `trading.TradingAccount` (`account_number`=login, `broker_server`, `is_demo`) | **REUSE** | Workspace hangs OneToOne off it (`related_name="hosted_workspace"`), joining `.runtime` / `.broker_health` / `.broker_pause`. |
| Order-time safety gate | `scripts/mt5_signal_bridge.py::evaluate_binding` (+`verify_execution_binding`) | **REUSE** | Pure, fail-closed, mutation-tested; already enforces `connected`→`trade_allowed`→`login` pin→`server` pin→demo/live. The workspace "active-account match" **is** this gate's identity pins. |
| Central backend exec gate | `execution/broker_gate.py::require_execution_gate` / `evaluate_dispatch_gate` | **EXTEND (later)** | Single choke point at `ExecutionJob.save` + final dispatch. A live active-account-match condition + `SR_ACTIVE_ACCOUNT_MISMATCH` / `SR_MT5_DISCONNECTED` codes belong at **dispatch** (live), not the static gate. |
| Durable pause / controlled resume | `execution.BrokerRuntimePause` + `execution/runtime_pause.py` | **REUSE** | The generic pause machinery a mismatch/disconnect would reuse. `request_broker_runtime_resume` exists but is currently unwired. |
| Continuous health contract | `reliability.BrokerAccountHealth` (+`broker_health.py`) | **EXTEND (later)** | `DISCONNECTED` state exists but is fed by validation-attempt folding, not a live session heartbeat. |
| Attach + observe | `mt5_worker/bridge_supervision_patch.py::_rx2_supervision_snapshot` | **EXTEND (later)** | Already attaches path-only and reads `connected`/`trade_allowed`/`login` — **omits `server` + `trade_mode`**; extend for a full `WorkspaceObservation`. Backend cannot import MetaTrader5 → observation is host-side, reaches backend over HTTP. |
| Non-admin Windows identity + portable runtime | `terminal_provisioning` TX-1 (`Provision-GuvfxAccount.ps1`, `Set-GuvfxKioskShell.ps1`, `services.py`) | **EXTEND (later)** | Per-account `guvfx_u_<id>`, `C:\GuvFX\accounts\<id>`, kiosk `/portable`. |
| NTFS ACL idiom | `deploy/beta-agent/install_pool.ps1::Grant-GuvfxServiceRead` | **REUSE (later)** | SID-typed `Set-Acl` + read-back. **TX-1 has NO ACL step today** — the hard blocker. |
| Delivery seam | `mt5/adapters/session_adapter.py::SessionAdapter` + `guac_json.py` | **EXTEND (later)** | The RemoteApp-shaped abstraction; RemoteApp params are not emitted anywhere yet. |
| Observability | `operational_events` (`broker_projection.py`, `summary.py`) + `agent_status_presenter` | **EXTEND (later)** | Fail-open, on-commit, DARK, customer-safe two-audience presenter. |
| Onboarding / API | `onboarding/services.resolve_setup_stage`, `trading` DRF router, `beta_activation` predicates | **EXTEND (later)** | `/api/trading/accounts/` is the customer broker surface (there is no `/api/broker-accounts/` backend). |
| Feature flags | Idiom B (`billing/beta.py::_flag`) + `feature-flags.json` + `tests_ipr_beta_flags.py` | **REUSE** | Read-live, settings-then-env, DARK default. |
| Flags & DARK conventions | `broker_gate`, `broker_health`, `operations_events_enabled` | **REUSE** | Additive-DARK: no-op while OFF, fail-closed while ON. |

### Boundaries this stream must NOT break (from area `boundaries-cz`)
- `scripts/mt5_signal_bridge.py` `:8788` is Customer Zero / account #1's **live order bridge** — never
  touch its binding; forbidden bind ports `:8787/:8788/:3389`.
- `deploy/beta-agent/lib/mgmt_protocol.py` + `mgmt_agent_core.py` are **byte-identical** to their
  `backend/terminal_provisioning/` copies (`BundleIntegrityTests`) and pinned in `manifest.json`
  (`manifest_version 2026-08-06.5`) — never edit one side; client-only helpers go in `mgmt_client.py`.
- The legacy login-validation programme (`broker_login_validation.py`, `validate_login.py`,
  `validation_runner.py`) is the **certified DARK path** and integrity-pinned — supersede via flag,
  never delete.
- Extend `backend/mt5/services/` (TerminalBinding/InteractionSession/adapter) rather than forking a
  parallel session system. Ignore the many tracked `*.bak` / `… 2.py` junk files.

---

## 3. Design tensions requiring an approved decision (before the execution wiring is built)

These are **not** codeable-around silently; ADR-0033 records them for Sponsor/PM decision.

1. **Attach model vs existing gate preconditions.** `broker_gate.evaluate_execution_gate` REQUIRES
   `account.password_enc` present **and** `validation_status == VALIDATED`; `signal_copy_arm` requires
   the same. Under "GuvFX attaches, never logs in," GuvFX holds **no** stored credential and runs **no**
   login-validation, so the gate/arm would refuse every job. Resolution needed: an *attach-verified*
   readiness that substitutes for the credential/VALIDATED precondition **only** on the workspace path,
   without weakening the legacy path.
2. **Single-tenant routing.** While `MULTI_ACCOUNT_ROUTING_ENABLED` is OFF, `_resolve_target` returns
   None if >1 routable arm exists on a source, and arm/toggle refuse a second (`source_single_tenant`).
   A multi-user beta where several users arm the same provider source needs fan-out ON.
3. **Process-level pin vs multi-tenant.** The bridge account pin is a single process-level env
   (`MT5_EXPECTED_LOGIN`) — one login per process. A shared bridge across multiple persistent
   workspaces needs the expected login carried **per job/session** (from `TerminalBinding.mt5_account_login`),
   which is unbuilt.
4. **`accounts.dat` vs RULE-10.** TX-1's `Populate-GuvfxViewerRuntime.ps1` writes `Login=0`,
   `AllowLiveTrading=0` and **deletes `accounts.dat`**, and the golden/RULE-10 machinery *refuses* any
   `accounts.dat`. The persistent-attach model (user keeps a broker-connected `accounts.dat`) directly
   contradicts this and needs a distinct runtime profile.

---

## 4. Target architecture by workstream (B–O)

- **B — Domain model / state machine.** `hosted_workspace.HostedMt5Workspace` (sibling OneToOne).
  `WorkspaceState`: NOT_PROVISIONED → PROVISIONING → AWAITING_USER_LOGIN → CONNECTED /
  ACTIVE_ACCOUNT_MISMATCH / DISCONNECTED / DEGRADED / STOPPED / ERROR. Three facts kept **independent**:
  *process running* ≠ *broker connected* ≠ *safe to execute*. **[SHIPPED]**
- **C — Active-account observation.** Pure `WorkspaceObservation` + `normalize_observation` (host JSON →
  scalars, fail-closed). Host-side attach probe extends `_rx2_supervision_snapshot` to add
  `server`+`trade_mode`; backend consumes over HTTP. Polling 1–5 s (no callback API); transient
  `None`/disconnected is a state, not a fault. **[pure part SHIPPED; host probe + poller DEFERRED]**
- **D — Strategy binding + pause/resume.** Reuse `evaluate_binding` at order time (the boundary) and
  the pure `evaluate_active_account_match` at the DB layer; **pause** (not hard-refuse, not auto-switch)
  on mismatch with a customer-safe nudge; **auto-resume** only when the expected `(login, server)`
  returns and `connected && trade_allowed`. **[pure matcher SHIPPED; gate/pause wiring DEFERRED —
  tension #1]**
- **Credential model.** User types creds into MT5; GuvFX never holds them. The model stores **no**
  secret (login/server are identifiers, like `account_number`). **[SHIPPED as an invariant + test]**
- **E/F — Provisioning / Windows identity / NTFS ACL / supervision.** Extend TX-1 with an ATTACH-mode
  runtime (keep `accounts.dat`), a per-user ACL hardening step (reuse `Grant-GuvfxServiceRead`,
  RULE-11 positive+negative proof), and scoped process supervision (never `kill all terminal64.exe`).
  **[DEFERRED — PLAN-only in repo; host certification is Sponsor-gated (tension #4 + hard ACL blocker)]**
- **G — RemoteApp delivery.** New `SessionAdapter` implementation + a factory seam (today
  `invoke_adapter_launch` hard-codes `GuacamoleVncAdapter`); emit Guacamole `remote-app` params; reuse
  `withCleanGuacAuth`. **[DEFERRED — PLAN-only; true RemoteApp needs a Windows Server RD host]**
- **H — Connection API.** A read-only per-account workspace projection (flag-gated 404 via a
  `_guard`, IDOR-safe), surfaced through the existing `{summary,timeline}` response or a
  `accounts/<pk>/workspace/` action. **[DEFERRED]**
- **I — Onboarding.** A flag-gated setup stage ("open MT5 → log into your broker → we detect it").
  **[DEFERRED]**
- **J — Validation coexistence.** Add `connection_validated_by = persistent_mt5 | temporary_validation`;
  no forced migration; the validation agent is *repurposed* (not deleted) into a health/connection
  observer. **[design recorded; DEFERRED]**
- **K — Visible-MT5 interface contract.** The workspace **provides** {workspace identity, RemoteApp
  binding, active runtime, connection descriptor, lifecycle}; the Visible-MT5 stream **consumes**
  open/display/reconnect. **No merge.** **[contract recorded here for the PM; DEFERRED]**
- **L — Persistence / reboot.** Disconnect keeps MT5 alive; logoff/reboot kills it; a process cannot
  migrate. Reboot recovery needs auto-logon + auto-launch + **MT5 auto-reconnect from saved creds**
  (unverified — needs a host test). **[state model design recorded; DEFERRED]**
- **M — Observability.** New `project_workspace_lifecycle` / `project_connection_state` (fail-open,
  on-commit, DARK); `_workspace_state` in the summary; customer-safe presenter (never reveal a login
  exists / is wrong / which account is active — `CONNECTIVITY`/`RUNTIME` default customer-visible, so
  override to operator-only). **[DEFERRED]**
- **N — Tests.** Domain, observation, strategy-safety (incl. stale-poll-cannot-bypass), security
  (ACL pos/neg), provisioning (portable mandatory), supervision, flags-inert. **[pure + model + flag
  tests SHIPPED; host/gate tests DEFERRED with their code]**
- **O — Adversarial review.** 17 attack dimensions; every HIGH/MEDIUM fixed or returned as a STOP.
  **[run against this increment's diff]**

---

## 5. What THIS increment ships (DARK, additive, backend-only)

`backend/hosted_workspace/` — a new app added to `INSTALLED_APPS`:
- `models.py::HostedMt5Workspace` (+ `WorkspaceState`) — sibling OneToOne, immutable-binding guard,
  secret-free `contract()`, `is_execution_ready` (display-only, explicitly **not** the order gate).
- `matching.py` — pure `WorkspaceObservation`, `ExpectedAccount`, `MatchDecision`,
  `evaluate_active_account_match` (fail-closed, mutation-tested), `normalize_observation`.
- `flags.py` — `HOSTED_PERSISTENT_MT5_ENABLED`, `HOSTED_MT5_REMOTEAPP_ENABLED`,
  `HOSTED_MT5_ACTIVE_ACCOUNT_POLLING_ENABLED` (Idiom B, DARK).
- `admin.py` — minimal read-only.
- `migrations/0001_initial.py` — one additive `CreateModel`.
- `tests_matching.py` / `tests_model.py` / `tests_flags.py` / `tests_flag_inventory.py`.
- `docs/operations/broker-connectivity/feature-flags.json` — `hosted_workspace_flags` section.

**Inert:** nothing in the execution / onboarding / delivery path reads any of it. There is **no** gate
wiring, **no** live probe, **no** host script, **no** API, **no** frontend surface in this increment.

---

## 6. Unresolved external gate — licensing (RECORDED, not decided)

Multi-user concurrency needs the RDS role (currently forbidden) + 1 RDS CAL/user, and hosting MS
software for external paying customers is commercial hosting → **SPLA** *or* **Windows Server + RDS
CALs with Software Assurance** *or* **AVD** — a Sponsor/commercial decision with a licensing specialist.
Implementation stays licensing-provider-agnostic where practical. **No purchase, no RDS install.**

---

## 7. M3c — Workspace Core: authoritative persistence + read model (DARK, 2026-08-08)

The M-series (ADR-0034) built the pure observation chain: **M1** guarded attach → **M3b-2** agent →
**M3b-1** producer → **M3a** manager decision. Every piece is pure/deterministic and, until now, wrote
nothing. **M3c** closes the loop by adding the one seam that *persists* the manager's decision, records
its provenance, and emits its telemetry — while remaining fully DARK (no production caller, flag-gated
API/telemetry). It is the Workspace **Core** subsystem: `Agent → RawSnapshot → Observation → Manager →
Decision → **Authoritative Persistence** → **Telemetry** → **Read Model / API**`.

### 7.1 Model (`models.py`, additive)

- `HostedMt5Workspace` gains the **canonical** M3c fields (distinct from, and additive to, the inert
  legacy `WorkspaceState` block): `canonical_state` (M2a `WorkspaceLifecycleState`), `canonical_reason`
  (M2a `WorkspaceReason`), `observation_version` (last-applied caller sequence), `decision_version`
  (count of material decisions), `last_decision_at`, `last_transition_at`, a latest-observation health
  projection cache (`proj_process_running/ipc_available/connected/account_match/trade_allowed/
  execution_ready`, nullable), and `last_correlation_id`. Property `canonical_execution_ready` is the
  read-model readiness flag — **display only, never the order gate**.
- New append-only **`WorkspaceTransition`** — one row per *material* decision (state change, reason
  change, or execution-readiness change): `from_state`/`to_state`/`reason`, `observation_version`/
  `decision_version`, `state_changed`/`execution_ready_changed`, `telemetry_event`, `source`,
  `correlation_id`, and a **unique `dedupe_key`** (`{uuid}:{obs_version}:{to_state}:{reason}`). The
  dedupe key is reused verbatim as the emitted `OperationalEvent.dedup_key`, so a replay double-appends
  neither a transition nor an event. Rows are only ever created, never updated. Stores no credential.
- Migration `0002` is **additive / deterministic / reversible / legacy-safe** — `CreateModel` +
  `AddField`(defaults) + `AddIndex` only; no data migration, no backfill. Proven by apply → reverse to
  `0001` → re-apply → `makemigrations --check` clean.

### 7.2 Single authoritative writer (`persistence.persist_workspace_decision`)

The **only** code path that mutates the canonical fields, appends provenance, or emits `workspace.*`
telemetry. Guarantees (all fail-closed):

1. **Row-level serialisation** — `transaction.atomic` + `select_for_update` on the workspace row for the
   whole decision; concurrent observations cannot interleave a read-modify-write.
2. **Stale-observation protection** — caller supplies a strictly-increasing per-workspace
   `observation_version`; `<= stored` ⇒ `REJECTED_STALE` (no mutation); non-positive/non-int (incl.
   `bool`) ⇒ `REJECTED_INVALID`.
3. **Stale-decision protection** — the transition legality is **re-validated against the LOCKED
   `canonical_state`** (`evaluate_workspace_transition`), never the decision's own premise; an illegal
   non-idempotent move ⇒ `REJECTED_ILLEGAL`, holds the stored state. (A decision derived against an
   out-of-date view is rejected even at a higher version.)
4. **Idempotency** — a material decision writes exactly one `WorkspaceTransition` via `get_or_create`
   on `dedupe_key`; the same key reuses the same `OperationalEvent.dedup_key`.
5. **Atomic state + event** — state update, transition row, and telemetry all commit in one
   transaction. Telemetry (`record_event`) is fail-open (ADR-0032) with its own savepoint, so a
   telemetry hiccup can never roll back a committed state change; a rolled-back state change emits no
   event. **Telemetry is emitted ONLY here, ONLY on a real canonical-state change, ONLY when the
   transition row was freshly created.** The builder's `account_id`/`detail` are mapped to the
   recorder's `account`/`metadata`; `state_version = decision_version`.

Persists / emits **no** credential. Performs no attach, launch, login, or order.

### 7.3 Consumer, read model, API (DARK)

- `consumer.ingest_observation` — thin orchestration: reads the stored canonical premise, overrides the
  observation's `previous_state`, asks the **M3a manager** for the decision, hands it to the writer. It
  is a **no-op returning `None`** while `HOSTED_PERSISTENT_MT5_ENABLED` is OFF, and **is wired by no
  production caller** in this increment. Never attaches/launches/logs in/orders.
- `read_model.workspace_state_projection` — an allow-listed, secret-free projection (canonical state +
  reason, health cache, timestamps). `staff=True` adds an operator block (versions, correlation id,
  supervision, **masked** login, bounded recent transitions). Never emits a full login / attach path /
  credential.
- `views.HostedWorkspaceStateView` — `GET /api/hosted-workspace/workspace-state/?account_id=<id>`.
  DARK gate FIRST (404 while OFF, before any DB read); IDOR-safe owner scoping (non-staff resolve only
  their own account; staff bypass); 400 on missing `account_id`; 404 for an owned account with no
  workspace. Mounted at `api/hosted-workspace/` in `guvfx_backend/urls.py`.

### 7.4 Order-authority boundary (unchanged, restated)

Persisted `canonical_execution_ready` / `proj_execution_ready` are **read-model/operational** signals.
The order-time authority remains `evaluate_binding` in the bridge (ADR-0033). Nothing in M3c can place,
size, or approve an order, and no execution path reads these fields.

### 7.5 Tests + review

`tests_persistence.py` — 29 focused tests across the 24-point bar (stale/illegal/idempotent/versioning/
material-vs-no-op/telemetry-only-on-state-change/secret-free/DARK/IDOR/legacy-safety) plus a runnable
mutation-adequacy harness for the writer's novel pure predicates (`_coerce_version`, `_as_bool`).
`make check` green (backend 3167). Multi-lens adversarial review recorded in the PR.

**Still out of scope (unchanged):** strategy execution, `order_send`, RemoteApp/RDS, multi-user pooling,
AppLocker, broker-credential ownership, production deployment, onboarding UI.

---

## 8. Execution Engine — Provider-B enablement (DARK, demo-only, 2026-08-08)

A full read-only inventory of the execution surface (7 parallel mappers + synthesis) established the
decisive fact: **the entire order-safety spine already exists and is certified** — the bridge's order-time
identity authority (`verify_execution_binding`→`evaluate_binding` for opens; `verify_mutation_identity`→
`evaluate_mutation_identity` for close/modify — live `account_info()`/`terminal_info()` re-read + demo/live
+ `trade_allowed` + login + server; a two-tier per-job pin; idempotency/lost-ACK), the central fail-closed
gate (`broker_gate` creation + `evaluate_dispatch_gate` fresh at claim), and result/pause/reconcile — with
114 bridge tests + 12 readiness tests. The Hosted Workspace (Provider B) is therefore a **binding-and-wiring**
job, **not** a new engine. This increment closes the two backend gaps that made that spine *unreachable* for
a hosted account.

**Order-authority invariant (restated).** Persisted `canonical_execution_ready` / readiness eligibility is a
READ-MODEL projection, never permission to trade. The authority immediately before every mutation stays the
live bridge gate: **live broker truth + mandatory expected-account pin + order-time identity gate**. Nothing
here replaces it with database state.

### 8.1 Delivered

- **G1 — Provider-B readiness on the M3c canonical projection** (`execution/readiness.py`). Latent BLOCKER:
  `PersistentWorkspaceProvider` read the legacy `observed_*`/`state`/`last_observed_at` cache that the M3c
  single writer deliberately does **not** maintain, so a hosted account would fail-close forever in
  production (the readiness tests passed only because fixtures set the legacy fields directly). Repointed at
  `proj_connected`/`proj_account_match`/`proj_trade_allowed` + `canonical_execution_ready` + `last_decision_at`
  freshness — the fields the certified writer maintains. Every reason code preserved; fixtures updated.
- **G3 — server-derived per-job identity pin** (`execution/hosted_pin.py` + central injection in
  `ExecutionJob.save()`). The bridge already enforces the pin; the backend never populated it (it relied on
  the process-global env pin). `identity_pin_for(account)` derives `expected_login` (account number),
  `expected_server` (broker server name) and `is_demo` from the account's durable bindings, and
  `inject_identity_pin` merges them into the payload at the single creation boundary for every mutation job
  type (PLACE/OPEN/CLOSE/MODIFY) — so a wrong-account close/modify fails closed at the bridge's
  mutation-identity gate (PARTS I/J). Fail-closed (pin required even if a binding value is missing); never
  clobbers a caller-supplied pin.

**DARK + regression-safe.** Both are no-ops (`{}` / `False`) for a non-Provider-B account and while
`HOSTED_PERSISTENT_MT5_ENABLED` is OFF — the flag is checked *before* any account access, so the legacy
Provider-A / Customer-Zero path is byte-for-byte unchanged with zero added overhead. No order is placed,
closed, or modified by this change. Persists / emits no credential (login/server are identifiers).

### 8.2 Decision A — RESOLVED (Amber, per packet PART D)

Provider-B readiness must read exactly one field set. Chosen: the M3c **canonical** projection (the certified,
row-locked, single-writer output), not the legacy cache. The legacy `observed_*` fields become vestigial for
the execution path (still present for the inert ADR-0033 foundation). This is the direct implementation of
"the persistent-workspace path uses Workspace Core."

### 8.3 Genuine decisions that GATE the remainder (surfaced, not forced)

These block full engine completion and any arming; each is a STOP condition (new architecture decision /
production-arming / RED authority). None is taken in this increment.

- **Decision B — real (customer-logged-in) accounts: RED.** The whole hosted stack is demo-pinned
  (`is_demo=True`; bridge close/modify refuse `trade_mode != 0`; the certified matcher hard-codes
  demo/allow_live=False). The product framing implies real accounts, which needs live authorisation across
  the match layer + bridge close/modify. Per the governance overlay this is **RED — requires Nuno's explicit
  approval**. This increment stays demo-only.
- **Decision C — isolation topology: Sponsor/deployment.** One bridge per workspace (pinned by `attach_path`)
  vs a shared worker with strict `TerminalNode`/account entitlement + the mandatory per-job pin. Determines
  routing (G4), provisioning (G5) and the host/cost model. The per-job pin (delivered) is belt-and-braces
  for either; the topology commitment is deferred to host certification.
- **Decision D — per-workspace execution mode: Amber-to-RED.** Real orders on the auto path require the
  GLOBAL `ExecutionControl.signal_execution_mode=DEMO` lever; there is no per-workspace scope. Arming one
  hosted workspace today would flip the global gate for every source. Per-workspace scoping touches the
  AND-gate SSOT and is required *before any arming* — out of scope for this DARK increment.
- **PART M — workspace-level `EXECUTING` is not a faithful model** of N concurrent per-strategy jobs on one
  account (it would oscillate/corrupt). Per PART M's own guidance this is surfaced as an architecture finding
  rather than forced: in-flight execution is a per-`ExecutionJob` fact, not a workspace lifecycle flip.

### 8.4 Remaining repository work (unblocked once B/C decided), for a follow-up increment

G2 scheduled observation→persist runner (so `last_decision_at` freshness advances — needs the host agent /
Decision C); G4 fail-closed account↔worker entitlement at claim (topology-dependent, Decision C); G5 gated
provisioning/opt-in seam; G6 bridge deploy asserting `MT5_GUARDED_ATTACH` + `MT5_REQUIRE_IDENTITY_PIN`;
G9 workspace-degradation→pause producer + resume caller; G10 hosted idempotency/comment-key contract. Each is
DARK and demo-only until B/C/D are decided; none arms production.
