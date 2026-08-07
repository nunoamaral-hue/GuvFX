# 0034 — Hosted Workspace Operating Model

- Date: 2026-08-07
- Status: **Proposed (draft) — pending Sponsor (PM) acceptance.** The programme *direction* is
  Sponsor-approved (Programme Architecture Reset, 2026-08-07); this ADR draft records the operating model
  and remains Proposed until accepted. No implementation, no behaviour change, nothing armed.
- Supersedes: the pre-ADR-0033 architectural assumption that GuvFX's product is *"a backend that validates
  broker credentials."* Builds on [ADR-0033](0033-hosted-persistent-mt5-workspace.md) (the DARK foundation +
  its Transition Amendment) and becomes the **architectural source of truth for the Hosted Workspace
  platform.** Companion: [Hosted Workspace Roadmap](../architecture/HOSTED_WORKSPACE_ROADMAP.md).
- Governance: ADR-0033/0027/0029/0030/0032 remain in force for the temporary-validation compatibility path
  until each is individually superseded under this ADR. No broad refactor; bounded DARK increments only.

## 1. Context — the programme reset

The MT5 IPC investigation is closed. Experiments A–I established that GuvFX can **attach** to a
user-logged-in, broker-connected MT5 (same- and cross-session), survive RDP disconnect, observe the full
trade lifecycle, and construct broker-validated trade requests — **without ever owning broker credentials**.
The programme therefore pivots:

> **From:** "A backend that validates broker credentials." **To:** "A hosted persistent MT5 workspace platform."

The **Workspace** — not the broker account — becomes the primary product entity and the unit of lifecycle,
ownership, isolation, delivery, and telemetry. The broker account becomes a **child resource** of the
Workspace, matching MT5's native operating model.

## 2. Domain model (authoritative)

```
User
 └─ HostedWorkspace                (1:1 today; owner-bound; the product entity)
     ├─ Windows session            (interactive logon; token; window station; desktop)
     ├─ MT5 process                (portable terminal64.exe; holds the live broker IPC pipe)
     │   ├─ Broker accounts        (0..n configured in Navigator; child resources)
     │   │   └─ Active broker      (exactly ONE active at a time — authoritative via account_info())
     │   ├─ Charts / EA runtime / Logs
     │   └─ Python attachment      (initialize(path=), never login — GuvFX's observation/execution channel)
     └─ Strategies                 (route to the workspace's ACTIVE broker account, gated by readiness)
```

Invariants:
- A Workspace **owns** its Windows identity, directory tree, MT5 process, and broker-connection state.
- GuvFX **never** holds/receives/transports the broker password and **never** calls `mt5.login()`.
- Exactly one broker account is *active* per MT5 process; the active account is authoritative from
  `account_info()` (login/server/trade_mode) + `terminal_info().connected/trade_allowed`.
- `observed_*` is always a **cache**; the order-time `evaluate_binding` gate is the sole execution authority.

The existing `hosted_workspace.HostedMt5Workspace` (OneToOne on `TradingAccount`, secret-free) is the seed of
this model. **Evolution required (deferred):** the Workspace becomes owner-bound (User-anchored) with broker
accounts as children; today it is account-anchored. This re-anchoring is an ADR-governed increment, not a
refactor taken here.

## 3. Workspace state machine (single authoritative model)

Every subsystem MUST consume this one model and MUST NOT invent its own interpretation.

```mermaid
stateDiagram-v2
  [*] --> Provisioning
  Provisioning --> WaitingForLogin: identity + terminal staged
  WaitingForLogin --> Connected: user logs into broker (attach observes connected)
  Connected --> ExecutionReady: active account matches bound + fresh positive observation
  ExecutionReady --> Executing: order in flight
  Executing --> ExecutionReady: order settled
  ExecutionReady --> Connected: active-account mismatch / trade_allowed lost
  Connected --> Disconnected: broker link / process lost
  ExecutionReady --> Disconnected: broker link / process lost
  Disconnected --> Recovering: reconnect / relaunch (user-owned session; cached-cred auto-login)
  Recovering --> Connected: attach observes connected again
  Connected --> Suspended: operator/risk pause
  ExecutionReady --> Suspended: operator/risk pause
  Suspended --> Connected: resume (all safety + lifecycle checks pass)
  Provisioning --> Retired
  Suspended --> Retired
  Disconnected --> Retired
  Retired --> [*]
```

| Canonical state | Meaning | Reconciliation with today's `WorkspaceState` |
|---|---|---|
| **Provisioning** | identity/terminal/tree being staged | `NOT_PROVISIONED` + `PROVISIONING` |
| **WaitingForLogin** | terminal up, no broker login yet | `AWAITING_USER_LOGIN` |
| **Connected** | broker-connected; not yet execution-authorised | `CONNECTED` |
| **ExecutionReady** | Connected + active-account match + fresh obs (was `is_execution_ready` property) | derived today → promote to a state |
| **Executing** | an order is in flight | *new* (transient) |
| **Disconnected** | broker link or process lost | `DISCONNECTED` |
| **Recovering** | reconnect/relaunch in progress | `DEGRADED` (partial) |
| **Suspended** | paused (operator/risk/mismatch) | `STOPPED` + mismatch as a *reason* |
| **Retired** | decommissioned (terminal) | `ERROR`/terminal + explicit retire |

`ACTIVE_ACCOUNT_MISMATCH`, `DEGRADED`, `ERROR` become **reason codes** attached to `Connected`/`Suspended`/
`Recovering`, not top-level states — keeping the state set small and every subsystem aligned. Migrating the
9-state enum to this 9-state canonical set (with reason codes) is a deferred, ADR-governed increment.

## 4. Operating-model decisions

- **Workspace lifecycle:** `Provisioning → WaitingForLogin → Connected → ExecutionReady`, with
  `Disconnected → Recovering` and `Suspended`/`Retired` per §3. Driven by real host observation via the
  guarded-attach primitive (below).
- **Workspace ownership:** one persistent Windows identity + session + MT5 per user (Model A from the
  feasibility study). Model B (pooled) and Model C (shared identity) are **rejected** (a live broker session
  is in-memory and per-user).
- **Workspace persistence:** the MT5 process + broker connection survive RDP/Guacamole *disconnect*
  (proven, Exp E). Logoff/reboot end the session; recovery is §"Recovery model".
- **Active account model:** poll (no callback API; 1–5 s cadence); the pure, fail-closed
  `matching.evaluate_active_account_match` decides; a switch drives `Suspended(reason=active_account_mismatch)`
  and pauses; return-to-expected resumes.
- **Connection model:** GuvFX attaches read-only via `initialize(path=, portable=True)`; liveness is a
  server-sourced datum (advancing tick), not just `connected`. A cold/not-logged-in terminal fails `-10005`.
- **Strategy routing:** a strategy routes to its Workspace's **active** broker account and executes **only**
  when the workspace is `ExecutionReady` for that bound account. Multiple simultaneous brokers for one user
  force multiple terminals (one IPC pipe per process).
- **Execution ownership:** the order-time authority remains `scripts/mt5_signal_bridge.py::evaluate_binding`
  / `verify_mutation_identity` (mutation-tested, unchanged), strengthened by the mandatory
  `MT5_REQUIRE_IDENTITY_PIN`. Execution is **attach-only**; `order_send`-via-attach already runs in the
  production `:8788` bridge. GuvFX never authenticates.
- **Session management:** interactive per-user session (autologon on a dedicated host); the MT5 process is
  bound to its session and cannot migrate (moving it drops the broker pipe).
- **RemoteApp delivery:** a **single MT5 seamless-window app** delivered via the existing `guac_json`
  signing/encryption layer with per-user dedicated routing + session reuse (reconnect-via-resume). Elevated
  to an **early** milestone: it is simultaneously customer functionality and engineering observability (being
  able to open the customer's actual MT5 session simplifies support/debugging).
- **Recovery model:** `Disconnected → Recovering → Connected`. Basic recovery = reconnect on a dropped link
  (MVP). Advanced recovery = reboot → autologon → autolaunch → MT5 **auto-reconnect from saved encrypted
  creds** (the only credential-free reboot path; mechanism evidenced in Exp H(e), full chain unsoaked → V2)
  and crash-loop supervision (WinSW).
- **Security boundaries:** GuvFX never holds the broker password (strictly stronger than the ADR-0027 seal).
  Per-user **NTFS ACL isolation is a HARD BLOCKER for any multi-user host** — `Provision-GuvfxAccount.ps1`
  currently applies **none**, so a shared host leaks `accounts.dat`. The MVP avoids this via
  **dedicated-host-per-user** (no cross-tenant surface). RemoteApp/AppLocker single-app lockdown + RULE-11
  positive/negative certification gate any multi-user host.

## 5. The Hosted Workspace Agent (evolution of the Validation Agent)

The `:8791` Validation Agent is **conceptually superseded**. Do not extend it under the "validation" concept.
It evolves into the **Hosted Workspace Agent** whose responsibilities are: supervise MT5, observe connection,
attach (guarded), expose health, expose the active account, recover, reconnect. **Validation becomes one
capability of the Workspace Agent, not its purpose.** The temporary credentialed `VALIDATE_LOGIN` op is a
legacy compatibility capability, retired per §7. This is a conceptual re-framing recorded here; the code
evolution is a bounded DARK increment, not taken now.

## 6. Workspace telemetry (mandatory from the beginning)

Workspace telemetry is first-class from the first increment, emitted onto the existing operational-event
model (ADR-0032) as a `workspace.*` event family. Minimum event taxonomy:

`WorkspaceCreated`, `WorkspaceStarted`, `WorkspaceConnected`, `WorkspaceDisconnected`, `WorkspaceRecovered`,
`BrokerChanged`, `AttachSucceeded`, `AttachFailed`, `ExecutionStarted`, `ExecutionFinished`,
`RemoteAppConnected`, `RemoteAppDisconnected`, `Restart`, `Crash`, `Recovery`.

Rules: events are durable, correlation-id-tagged, secret-free (login masked), and each maps to a state
transition or a supervision fact in §3. No subsystem emits a workspace lifecycle claim outside this family.

## 7. Temporary-validation retirement strategy

Temporary broker validation (ADR-0027 login-seal path) is now a **legacy compatibility path**: **supported,
not extended, not deleted.**

- **Per-account obsolescence trigger:** an account set to `readiness_provider='persistent_workspace'` with
  `HOSTED_PERSISTENT_MT5_ENABLED` on — Provider B then proves eligibility from a fresh (≤300 s) attach
  observation, replacing **only** `password_enc`+`VALIDATED`.
- **Never automatic:** `readiness_provider` is never auto-converted; each migration is individually evidenced.
- **Full retirement (delete) only when the last account is migrated** and the subsystem has left DARK — then
  `mt5_validate_worker`, `broker_login_validation`, the credential-seal path, the `:8791 VALIDATE_LOGIN` op,
  `password_enc`-as-credential, `VALIDATED`-as-authority, and `TemporaryValidationProvider` retire. Append-only
  `BrokerAccountValidationAttempt` history and TOMBSTONE rows are **retained** (audit).
- Until then: maintain for compatibility only; all new effort targets the Workspace architecture.

## 8. MVP boundary (redefined)

**MVP = the first end-to-end customer journey, dedicated-host-per-user, demo-only:**

```
sign up → workspace provisioned → user logs into MT5 → workspace detected →
Python attaches (guarded) → broker connection observed → ONE strategy executes safely → RemoteApp available
```

**In MVP:** WS3 guarded-attach, WS1 workspace lifecycle + basic (disconnect) recovery, WS2 active-account
detection + Provider B attach-only demo execution, WS4 **RemoteApp delivery (early)**, workspace telemetry,
the canonical state machine. **Deliberately dedicated-host-per-user** so multi-user NTFS-ACL isolation is not
a functional prerequisite.

**Deferred to V2:** multi-user pooling + NTFS-ACL/AppLocker isolation, capacity/density + **licensing-cost**
optimisation, advanced recovery (reboot auto-reconnect at scale, crash-loop policy), live trading.

**Reset tension surfaced (no-assumption rule):** moving RemoteApp into the MVP pulls the **RDS role + a
per-user RDS CAL/SAL into the MVP** (RemoteApp requires the RDS role). So the *commercial licensing decision*
(which model — SPLA vs CALs-with-SA) is now an **MVP-blocking prerequisite**, even though multi-user
*licensing-cost optimisation* is V2. This is a Red/Sponsor gate that the reset moves earlier.

## 9. Risks introduced by the reset

1. **Licensing pulled into MVP:** RemoteApp-early ⇒ RDS + per-user CAL/SAL required for MVP; the commercial
   licensing decision (ADR-0033 condition 5) is now an MVP-blocking prerequisite, not a V2 item.
2. **State-machine migration risk:** promoting `ExecutionReady`/`Executing`/`Recovering`/`Suspended`/`Retired`
   to first-class states and demoting mismatch/degraded/error to reason codes is a schema + consumer change
   across subsystems — must be a bounded, ADR-governed, backward-compatible migration, not a refactor.
3. **Workspace re-anchoring risk:** re-anchoring the Workspace from account-owned to **user-owned** (brokers
   as children) touches the `hosted_workspace` model + onboarding; must preserve the DARK/reversible property.
4. **Scope-creep / concept churn:** a "reset" invites broad refactors — explicitly forbidden; safety gates,
   execution authority, and Customer Zero protections stay unchanged until individually replaced.
5. **Telemetry-first cost:** mandating the full event family from increment one risks over-building; scope
   telemetry to the states/facts that exist per increment (emit-what-you-transition).
6. **RemoteApp-early without isolation:** RemoteApp on a *dedicated* host is safe, but any drift to a shared
   host before WS5/RULE-11 re-exposes the `accounts.dat` leak — the dedicated-host constraint must be enforced.
7. **Agent re-framing vs live `:8791`:** the Validation Agent is deployed/monitored; evolving it must not
   regress the current supervised/health/alert behaviour (ADR-0013 line).
8. **Unverified host assumptions still gate real use:** per-user attach (EXP-1, manual login) and reboot
   auto-reconnect from cached creds remain to be proven on a disposable host.

## 10. Decision

Adopt the Hosted Workspace operating model in §§2–8 as the architectural source of truth, with the
Workspace as the primary entity, the §3 state machine as the single authoritative model, the Workspace Agent
re-framing (§5), mandatory workspace telemetry (§6), RemoteApp elevated to an early milestone, the redefined
MVP (§8), and temporary validation as a maintained-not-extended legacy path (§7). Implement **only** in
bounded DARK increments under ADR governance; existing safety guarantees, execution gates, and Customer Zero
protections remain unchanged until individually replaced.

## 11. Consequences

- The roadmap re-sequences RemoteApp early and makes telemetry + the canonical state machine cross-cutting
  foundations (see the revised [roadmap](../architecture/HOSTED_WORKSPACE_ROADMAP.md)).
- First engineering increment (recommended) is unchanged in kind: the **WS3 guarded-attach primitive** — the
  safety foundation the Workspace Agent's attach depends on — additive/DARK/mutation-tested.
- The licensing gate moves earlier (into MVP) and must be resolved before RemoteApp delivery.

## 12. Reversal path

This ADR is a draft; nothing ships from it. All downstream increments stay DARK/additive/reversible (unset
flags immediate; drop additive tables while OFF). The temporary-validation path is untouched and remains the
default for every account.

## 13. Approval

PM owns lifecycle status. Programme *direction* is Sponsor-approved; this ADR stays **Proposed** until the
document is accepted. Every host/execution/licensing/arming step remains **Red** (Sponsor-gated).
