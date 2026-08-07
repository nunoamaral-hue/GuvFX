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
