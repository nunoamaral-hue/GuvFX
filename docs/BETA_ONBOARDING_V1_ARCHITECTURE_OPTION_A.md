# Beta Onboarding V1 — Option A Target Architecture (Windows-native RDS/RemoteApp host pool)

> **Status: DESIGN FOR APPROVAL — v2 refined (2026-07-20). No procurement. No architecture-dependent
> implementation has started.** v2 adds, per Nuno: the **canonical ownership model** (§0, User → Broker
> Account → MT5 Runtime → Strategies → Positions → Notifications, runtime owned by the *broker account*),
> the **full provisioning state machine + async workflow** (§8, durable transitions / retries / recovery /
> truthful progress), the **deployment topology** (§18), the **implementation sequence** (§19), and the
> **final BoM / licensing / cost** for approval (§20). Approval of §20 + §19 gates all Phase-2+ work and
> any procurement. Onboarding stays CLOSED until Phase 4 isolation gates pass.
> Companion: [`BETA_ONBOARDING_V1_PROGRAMME.md`](BETA_ONBOARDING_V1_PROGRAMME.md) (blocker analysis + phases).

## Design principles (from Nuno's authorisation)

Native Windows Server + native MT5; properly licensed RDS; **RemoteApp** (only the user's MT5 app, never a
shared desktop); browser access via Guacamole and/or RD Web; **one non-admin Windows identity per beta
user**; **one isolated portable MT5 runtime per broker account**; strict 1:1 ownership mapping (GuvFX user
↔ broker account ↔ Windows identity ↔ MT5 runtime ↔ RemoteApp ↔ Guacamole connection); **fail-closed**
whenever an owned runtime is unavailable; horizontal scaling by adding session hosts; **≤~2 users/host**
until proven; automated (AUTO_DEMO) terminals keep running when the user disconnects; **no shared
Administrator desktop**; **no Windows containers** without a separate proof; **no Wine**. Nuno's existing
box remains his isolated production runtime, untouched.

Key enabler already in place: execution is **enqueue-only** (`ExecutionJob → ingest worker → per-runtime
bridge`), so multi-tenancy is achieved by giving each account its own **runtime + bridge endpoint** the
worker targets — not by rewriting execution.

---

## 0. Canonical ownership model (authoritative)

```
User ──owns──▶ Broker Account ──owns──▶ MT5 Runtime ──hosts──▶ Strategies ──produce──▶ Positions ──produce──▶ Notifications
   │                (≤10/user)              (exactly 1                (AUTO_DEMO          (broker
   └──owns──▶ Windows identity guvfx_u_<uid>  per account)             assignments)        tickets)
                    │
                    └── runs the runtime + is the subject of the RemoteApp / Guacamole session (ephemeral view)
```

**The MT5 Runtime is owned by the Broker Account — never by a strategy or a session.** Consequences that
the design and data model MUST honour:

- **One runtime per broker account**, 1:1. Its lifecycle (provision → run → repair → deprovision) is bound
  to the *broker account*, not to any strategy assignment and not to any interactive session.
- **Strategies are hosted BY the runtime**, downstream of it. Assigning/removing/enabling/disabling a
  strategy on an account **never** creates or destroys the runtime — it only changes what executes *inside*
  the already-owned runtime. A runtime with zero active strategies still exists and can run (idle).
- **Sessions are ephemeral views, upstream of nothing.** A Guacamole/RemoteApp session is a transient window
  onto the account's runtime; opening/closing it never provisions or tears down the runtime. **Automated
  (AUTO_DEMO) terminals keep running when the user disconnects** — they belong to the account's runtime, not
  to the session.
- **Positions and Notifications are strictly downstream** and inherit ownership transitively: a Position
  belongs to the Strategy→Runtime→Broker Account→User chain; a Notification belongs to its Position. Every
  cross-tenant guard, query scope, and routing decision keys off this single chain.
- **Windows identity** `guvfx_u_<uid>` is owned by the **User** (one per user); each of the user's
  account-runtimes runs *under* that identity with per-account NTFS isolation. (Identity is per-user;
  runtime is per-account.)

Data-model mapping: `User (1)─(N) TradingAccount (1)─(1) AccountRuntime (1)─(N) StrategyAssignment
(1)─(N) Position/Trade (1)─(N) Notification`. `AccountRuntime` (the new durable runtime record, an evolution
of `AccountProvisioning`) carries the provisioning state machine (§8) and is FK-owned by `TradingAccount`.

---

## 1. Windows host roles

| Role | Purpose | Beta placement |
|---|---|---|
| **AD Domain Controller (DC)** | Directory for per-user identities + groups; required for RDS **Per-User CAL** + RemoteApp collections | 1 small VM (dedicated) |
| **RDS Connection Broker (RDCB)** | Routes each user to their RemoteApp/session; load-balances the host pool; reconnects to existing sessions | Co-located on the infra VM for beta |
| **RDS Gateway (RDGW)** | Tunnels RDP over TLS/443 for external browser access (behind Guacamole / RD Web) | Co-located on the infra VM |
| **RDS Web Access (RDWeb)** | Optional HTML5/portal RemoteApp feed | Co-located (optional) |
| **RDS Licensing** | Activates + issues Per-User CALs | Co-located on the infra VM |
| **RDS Session Host (RDSH)** | Runs each user's MT5 **RemoteApp** + the per-account **automated** MT5 terminals | **Pool of 2–3 hosts** |
| **Provisioning control-plane** | Creates identities/runtimes/entitlements; start/stop/repair/remove | The existing **Ubuntu** control plane, driving the hosts over **WinRM/PowerShell (+ the GuvFX Windows agent)** |

For beta, the **infra roles (DC + RDCB + RDGW + RDWeb + Licensing) collapse onto ONE Windows Server VM**
("infra host"); the **RDSH pool is separate and horizontally scalable**. HA-splitting the infra roles is a
post-beta concern (see §17).

## 2. Number & initial specification of hosts

| Host | Count (beta) | Spec (initial) | Notes |
|---|---|---|---|
| Infra (DC+RDCB+RDGW+RDWeb+Licensing) | 1 | 4 vCPU / 8–16 GB / 80 GB SSD, Windows Server 2022/2025 | Not HA at beta; snapshot/backup daily |
| RDSH session host | **2–3** | 4 vCPU / 16 GB / 80–120 GB SSD, Windows Server 2022/2025 | ≤~2 interactive users/host initially |
| (existing) Nuno production box | 1 | unchanged | **Untouched**; not part of the pool |

5 beta users ÷ ~2/host = **3 RDSH** for headroom + one-host-failure tolerance; start with **2 RDSH** and
add the 3rd once density/isolation is proven. Total **new** Windows VMs at beta: **3–4**.

## 3. Session-host capacity assumptions

- **≤~2 interactive beta users per RDSH** until compatibility, resource use, isolation and concurrent
  operation are production-proven.
- Each user: **up to 10 broker accounts**, but **not all interactive at once**. Distinguish:
  - **Automated terminals** (AUTO_DEMO) — one lightweight MT5 process per *active* account, running
    headless under the user identity, **kept alive across RemoteApp disconnects** (see §8).
  - **Interactive RemoteApp** — on-demand, only while the user is viewing/trading.
- RAM budget per RDSH (16 GB): OS/RDS ≈ 4 GB; MT5 terminal ≈ 150–300 MB each; 2 users × (say) up to ~5
  active automated terminals ≈ 1.5–3 GB; interactive RemoteApp overhead ≈ 200–400 MB/session → **≈ 16 GB
  comfortably covers 2 users**. CPU: MT5 is light except tick bursts. **Density is a Phase-4 proof, not an
  assumption** — raise only with evidence.

## 4. RDS Connection Broker, Gateway, Web Access, Licensing

- **Connection Broker:** publishes a **RemoteApp collection** ("GuvFX-MT5"), routes each user to a host
  running their session, reconnects to an existing session (so the interactive view re-attaches, while the
  automated terminal has never stopped), and load-balances new sessions across the pool.
- **Gateway:** RDP-over-HTTPS(443) with a TLS cert (same Let's Encrypt/Traefik estate or a dedicated cert);
  the only externally reachable RDS surface. Session hosts are **not** publicly exposed.
- **Web Access:** optional RemoteApp portal; for GuvFX we prefer the in-app Guacamole experience (§5).
- **Licensing:** **Per-User RDS CALs** (requires AD). Licensing server activated against Microsoft; CALs
  tracked per beta user. (Per-Device CALs are the workgroup alternative but don't fit per-user identity.)

## 5. Guacamole vs RD Web vs both

**Recommendation: Guacamole is the external browser gateway; RD Web is an optional admin/fallback path.**
Guacamole is already deployed + GuvFX-integrated (branded in-app UX, per-user connection entitlement mapped
from GuvFX). Each user's Guacamole **RDP connection** is configured with the **`remote-app`** parameter to
launch **only** their MT5 RemoteApp (not a desktop), using **their** Windows identity, routed through the
**RD Gateway**. GuvFX remains the source of truth for entitlement; RD Web stays available for operator use.

## 6. Per-user Windows identity lifecycle

- **Create** (on beta provisioning): AD user **`guvfx_u_<uid>`**, **non-administrative**, member of
  `GuvFX-BetaUsers` (RemoteApp collection access only), **denied** full interactive desktop logon beyond
  RemoteApp; strong random password generated by the provisioner and **Fernet-encrypted** in GuvFX.
- **Enable / Disable (suspend):** toggle account + collection membership (fail-closed: disabled → no
  launch). **Remove (offboard):** revoke entitlements, delete runtimes, delete the AD user.
- Every transition is driven by the provisioning control-plane (AD cmdlets over WinRM) and **audited**.

## 7. Per-account portable MT5 runtime layout

- Per broker account: a **portable MT5 directory** on the RDSH, e.g.
  `D:\GuvFX\users\<uid>\accounts\<account_id>\mt5\` (portable mode → isolated config/logs/history/EA),
  a distinct `terminal64.exe` instance. **NTFS-ACL'd to only `guvfx_u_<uid>` (+ SYSTEM/admin)** so no other
  user can read it — this eliminates the shared-handoff-dir credential exposure (C16).
- The **automated** terminal for this account runs under `guvfx_u_<uid>` as a background/service-like
  process (per-user scheduled task / logon task), independent of any interactive RemoteApp session.

## 8. Runtime provisioning — state machine + asynchronous workflow (owned by the broker account)

Provisioning is an **asynchronous, durable, idempotent state machine on `AccountRuntime`** (FK-owned by
`TradingAccount`). Every transition is persisted before the side-effect is attempted and reconciled after,
so a crash/host-failure resumes from the last durable state. **No exception is ever swallowed** — each
failure writes a durable `RuntimeEvent` (stage, attempt, sanitised reason, raw-error-ref) and moves the
runtime to a truthful state the Account Status panel renders verbatim.

### 8.1 States

| State | Meaning | User-facing label |
|---|---|---|
| `NOT_PROVISIONED` | Account exists; no runtime yet | Not provisioned |
| `QUEUED` | Provisioning requested; awaiting a worker + host slot | Queued |
| `BLOCKED` | Prerequisite missing (no host capacity / entitlement / gate closed) | Blocked (reason) |
| `PROVISIONING` | Materialising identity/dir/portable-MT5/config/creds/task | Provisioning… |
| `STARTING` | Launching the automated terminal | Starting… |
| `AUTHENTICATING` | MT5 logging into the broker account | Authenticating… |
| `RUNNING` | Automated terminal up + logged-in; account operational | Running |
| `DEGRADED` | Was running; a health check failed (not logged-in / terminal gone) | Degraded (auto-repairing) |
| `REPAIRING` | Re-materialise/restart in progress | Repairing… |
| `STOPPING` / `STOPPED` | Deliberately stopped; runtime exists, terminal not running | Stopped |
| `DEPROVISIONING` / `REMOVED` | Account offboarded; runtime torn down | Removing… / Removed |
| `FAILED` | Terminal, non-retryable failure (bad creds / capacity exhausted / retries spent) | Failed (reason) — Retry |

### 8.2 Transitions (with retries + recovery)

```
NOT_PROVISIONED ─(validated+entitled+gate-open)─▶ QUEUED
QUEUED ─(host slot)─▶ PROVISIONING          QUEUED ─(no capacity/entitlement)─▶ BLOCKED ─(cleared)─▶ QUEUED
PROVISIONING ─ok─▶ STARTING                 PROVISIONING ─err─▶ retry×N(backoff) ─exhausted─▶ FAILED
STARTING ─launched─▶ AUTHENTICATING          STARTING ─err─▶ DEGRADED
AUTHENTICATING ─login ok─▶ RUNNING           AUTHENTICATING ─bad creds─▶ FAILED   ─transient─▶ retry ─exhausted─▶ DEGRADED
RUNNING ─health fail─▶ DEGRADED ─▶ REPAIRING ─ok─▶ RUNNING   REPAIRING ─exhausted─▶ FAILED
RUNNING ─(pause/deactivate)─▶ STOPPING ─▶ STOPPED ─(resume)─▶ STARTING
any ─(account delete)─▶ DEPROVISIONING ─▶ REMOVED
FAILED ─(user/admin Retry)─▶ QUEUED
```

- **Retries:** every fail-able transition has a bounded exponential-backoff retry (`attempt`, `last_error`,
  `next_retry_at` on `AccountRuntime`); on exhaustion → `FAILED`/`DEGRADED` with a truthful reason. Raw agent
  strings are **sanitised** for the user (mapped to safe messages: *invalid broker credentials*, *wrong
  server*, *host at capacity*, *broker unreachable*), with the raw text kept admin-only.
- **Recovery / host failure:** the `AccountRuntime` record + state persist independently of the host; a
  reconciler (extends `execution_health`) detects a runtime whose host is down or whose terminal died and
  re-drives from the durable state onto a healthy host (idempotent materialisation).
- **Idempotency:** every step is safe to re-run (create-if-absent identity/dir; overwrite config; re-inject
  cred; ensure-task). Re-entry from any state converges to the target.

### 8.3 Asynchronous workflow

1. **Trigger:** broker-account validation succeeds **and** the user is entitled **and** the onboarding gate
   is open (Phase 4) → enqueue a durable **`ProvisioningJob`** (same pattern as `ExecutionJob`) with a
   target op (`PROVISION` / `START` / `STOP` / `REPAIR` / `DEPROVISION`).
2. **Claim:** a **provisioning worker** (extends `terminal_provisioning`) claims the job (lease + single-
   flight), loads the `AccountRuntime`, and drives the state machine **one durable step per iteration** via
   the Windows agent over **WinRM/PowerShell** (create identity, materialise runtime, inject cred, start/stop/
   repair/remove) — replacing today's hand-run `Provision-GuvfxAccount.ps1`.
3. **Persist-then-act:** write the next state + `RuntimeEvent` **before** the side-effect; reconcile the
   actual result after; on error record it and apply the retry/DEGRADED/FAILED policy — **never `pass`**.
4. **Truthful progress:** the **Account Status panel** (Phase 0 scaffold) reads `AccountRuntime.state` +
   the latest `RuntimeEvent` and shows the exact stage (Queued / Provisioning / Starting / Authenticating /
   Running / Degraded / Failed / Blocked) with the sanitised reason + retry ETA — **never false success**;
   unimplemented stages show `NOT PROVISIONED` / `BLOCKED`.
5. **AUTOMATED vs INTERACTIVE:** the workflow provisions/keeps the **automated** terminal (AUTO_DEMO) alive
   regardless of any interactive RemoteApp session; interactive sessions are a separate, ephemeral concern
   (§10) and never drive runtime state.

## 9. Credential injection & encryption boundaries

- Broker credentials: **Fernet-encrypted at rest** in the GuvFX DB (already true, `GUVFX_FERNET_KEY`).
- Decrypted **only at injection time**, transported to the RDSH over the **authenticated agent/WinRM
  channel (TLS)**, written **only** into the per-account runtime dir ACL'd to `guvfx_u_<uid>`, and kept to
  the minimum needed for MT5 auto-login. **No shared handoff directory** (kills C16).
- The Windows identity password is generated by the provisioner and Fernet-stored; used to build the
  per-user Guacamole/RemoteApp connection. **Encryption boundary:** plaintext exists only transiently inside
  the isolated runtime; never in a shared path, never in logs.

## 10. Guacamole / RemoteApp entitlement mapping

One **Guacamole connection per (user, account-runtime)**, configured with the user's Windows identity +
the MT5 **RemoteApp** + the RD Gateway, and **granted only to that GuvFX user** (Guac permission).
GuvFX is the source of truth: provisioning **creates** the connection + grant; offboarding **revokes** it.
Strict 1:1 chain enforced and audited: **GuvFX user ↔ broker account ↔ `guvfx_u_<uid>` ↔ MT5 runtime ↔
RemoteApp resource ↔ Guacamole connection**. No shared connection object ever.

## 11. Broker-account-to-runtime routing

Each `TradingAccount` → its **own `Mt5Instance`/runtime** on a specific RDSH (per-account). The now
fail-closed `_get_user_mt5_instance` (Phase 0) resolves to the **user's own leased per-account runtime**, or
**None** (clear message) — never a shared/other-user box. The ingest worker/bridge targets **that** runtime's
endpoint for the account's orders.

## 12. Strategy-assignment-to-runtime routing

The AUTO_DEMO `StrategyAssignment` (per account) → the account's runtime. The Phase-3 auto-router **fan-out**
(C10) resolves **all** routable assignments bound to a source and plans **one execution per account**, each
placed on that account's runtime, sized by the **per-account lot override** (C11, Phase 0). No global
single-target ambiguity; one user's arming/failure never affects another's.

## 13. Protection against cross-tenant access (defence in depth)

1. **Windows:** non-admin per-user identities; **RemoteApp-only** (no shared desktop); **NTFS ACLs** per
   runtime dir; cross-user deny.
2. **RDS:** Connection Broker routes each user only to their RemoteApp collection.
3. **Guacamole:** per-user connection grants; no shared connection.
4. **GuvFX app:** fail-closed instance resolution (C2/C17/C19 ✓ Phase 0); user-scoped querysets (✓) +
   tenant-scoped alerts/health/ops (✓ Phase 0).
5. **Network:** RD Gateway TLS-only; session hosts not publicly exposed.

## 14. Host failure & account recovery

Automated terminals are **per-account tasks** with **durable provisioning state** → on RDSH failure,
re-materialise the account's runtime on another pool host (idempotent) and re-point routing; the Connection
Broker handles interactive reconnect. No user data loss (positions are broker-side; the runtime is
rebuildable from config + Fernet creds). The provisioning/GuvFX DB is backed up (ties into the estate's
backup gap — see KNOWN_ISSUES/NEXT).

## 15. Observability & admin support

- Per-runtime health (terminal running / logged-in / last heartbeat / last execution) → the **Account
  Status panel** (Phase 0, truthful states incl. `NOT PROVISIONED` / `BLOCKED`) + **user-scoped admin**
  visibility of all users/accounts/runtimes/entitlements/failures (Phase 0) — without cross-tenant leakage
  in user views (✓ Phase 0 scoping).
- Provisioning **state + failure records** (Phase 0) make onboarding debuggable; raw agent-error strings are
  no longer surfaced to users (Phase 0).

## 16. Expected beta infrastructure & licensing cost (BoM — estimate, for approval)

> Indicative ranges for Nuno's approval; **not a purchase**. Two licensing models shown. Exact quotes depend
> on the chosen cloud/provider and whether licences are **rented (SPLA, monthly)** or **owned (one-off CAL)**.

| Item | Qty (beta) | Model A — Cloud/SPLA (monthly) | Model B — Owned licences (one-off) |
|---|---|---|---|
| Windows Server VM — infra (DC+RDS roles) | 1 | ≈ $80–150/mo (incl. Windows via SPLA) | Windows Server Std ≈ $900 one-off + VM host |
| Windows Server VM — RDSH session host | 2–3 | ≈ $90–160/mo each | Windows Server Std ≈ $900 each one-off + VM host |
| **RDS Per-User CAL / SAL** | 5 | ≈ **$4–7 / user / mo** (RDS SAL via SPLA) | RDS User CAL ≈ **$140–160 one-off / user** |
| TLS cert / Gateway | 1 | included (Let's Encrypt) or ≈ $0–15/mo | — |
| Backups / snapshots | — | ≈ $10–30/mo | — |
| **Indicative beta total** | | **≈ $350–700 / month** | **≈ $3,500–5,500 one-off + VM hosting** |

**Recommendation: start on the SPLA/monthly model** (elastic, no upfront CAL outlay, matches "add hosts to
scale"); revisit owned CALs once beta density + retention are proven. **No purchase until Nuno approves this
BoM.**

## 17. Scaling path beyond five users

- **Add RDSH** to the pool (each ~2 users initially; raise density once §3 is proven); the Connection Broker
  load-balances automatically.
- **CALs/SALs** scale per user (SPLA monthly is elastic).
- The consolidated **infra VM** (DC + CB + GW + Licensing) serves well beyond beta; at ~scale, **split roles
  and add HA** (redundant CB, GW farm, secondary DC, dedicated Licensing).
- App-layer fan-out (per-account routing/sizing, Phase 3) is already O(N accounts) and host-agnostic, so the
  ceiling is host capacity + RDS infra HA, not the GuvFX code.

---

## 18. Deployment topology (beta)

```
                         Internet (HTTPS/443)
                                 │
                        ┌────────▼────────┐
                        │  Traefik (VPS)  │  guvfx.com / api.guvfx.com / guac.guvfx.com
                        └───┬─────────┬───┘
        ┌───────────────────┘         └───────────────────┐
   ┌────▼─────┐                                       ┌────▼──────────┐
   │ Frontend │                                       │  Guacamole    │──RDP/RemoteApp via RDGW──┐
   │ + Backend│                                       │  (per-user    │                          │
   │ + workers│──ExecutionJob/ProvisioningJob────┐    │  connections) │                          │
   │ (Ubuntu) │                                  │    └───────────────┘                          │
   └────┬─────┘                                  │                                               │
        │ WinRM/agent (provision/start/…)        │ per-account bridge (orders)                   │
        ▼                                        ▼                                               ▼
   ┌─────────────────────────────┐   ┌───────────────── Windows RDSH pool (2–3 hosts, ≤~2 users each) ─────────────────┐
   │ Infra host (Windows):       │   │  guvfx_u_<uid> (non-admin)  ├─ acct A runtime (portable MT5, automated + bridge) │
   │  AD DS · RDCB · RDGW · RDWeb │◀──┤                             ├─ acct B runtime …                                 │
   │  · RDS Licensing (Per-User) │   │  RemoteApp: only MT5.exe    └─ (NTFS-isolated per account)                      │
   └─────────────────────────────┘   └───────────────────────────────────────────────────────────────────────────────┘
   ┌─────────────────────────────┐
   │ Nuno production Windows box │  ◀── UNTOUCHED, not in the pool
   └─────────────────────────────┘
```

- Backend/workers/Guacamole/Traefik stay on the existing Ubuntu VPS. **New** Windows footprint = 1 infra host
  + 2–3 RDSH. External RDP reaches RDSH **only** via RDGW (443); RDSH are not otherwise public.

## 19. Implementation sequence (Phase 2+, each gated + reviewed; onboarding stays CLOSED until Phase 4)

1. **Data model** — `AccountRuntime` (FK-owned by `TradingAccount`, the §8 state machine) + `RuntimeEvent`
   (durable failures/progress) + `ProvisioningJob` (async queue). Migrations additive.
2. **Windows agent** — idempotent WinRM/PowerShell provisioning endpoints (create identity, materialise
   runtime, inject cred, start/stop/repair/remove) + health probe. Read-only proof first, then materialise.
3. **Provisioning worker** — async job runner driving the state machine one durable step per iteration
   (persist-then-act, retries, reconcile, no swallowed errors).
4. **Per-account routing** — order routing to the account's runtime bridge (builds on the Phase-0
   fail-closed `_get_user_mt5_instance`); the runtime endpoint is resolved from `AccountRuntime`.
5. **Multi-tenant execution** — auto-router **fan-out** (resolve all AUTO_DEMO assignments per source → one
   plan/execution per account → that account's runtime) + per-account **sizing override** (Phase-0 model).
6. **RemoteApp + Guacamole** — RDS collection publishing MT5 RemoteApp; per-user AD identity; per-account
   Guacamole connection granted only to the owner (entitlement mapped from GuvFX).
7. **Account Status panel** — wire the Phase-0 scaffold to the live `AccountRuntime` state (truthful stages).
8. **Isolation + load hardening (Phase 4 gates)** — per-user/per-account isolation red-team; 5-user load;
   no cross-tenant data; existing production unaffected. Only then may onboarding open.

## 20. Final BoM · licensing · costs (for approval — NO procurement yet)

Bill of materials (beta): **1 Windows infra host** (AD DS + RDCB + RDGW + RDWeb + RDS Licensing) + **2–3
Windows RDSH** + **5 RDS Per-User CALs/SALs** + TLS/backup. See §2 (specs) and §16 (cost table). Two
licensing models:

| Model | Up-front | Monthly | Best when |
|---|---|---|---|
| **SPLA / cloud-rented (recommended for beta)** | ~$0 | **~$350–700/mo** (3–4 Windows VMs + RDS SAL ×5) | elastic, prove density first |
| **Owned licences** | **~$3.5–5.5k one-off** (Windows Server + RDS User CALs) + VM hosting | VM hosting only | steady-state after beta |

**Recommendation:** start SPLA/monthly; revisit owned CALs once §3 density + retention are proven. **This BoM
+ topology + licensing + cost + the §19 sequence are what require Nuno's approval before any
architecture-dependent (Phase 2+) work or procurement begins.**

## Approval gate

**Nuno approves this design + the BoM/cost model → then Phase 1 procures (per approval) and Phase 2 begins.**
Onboarding remains CLOSED until Phase 4 proves: per-user terminal isolation, per-account runtime isolation,
user-scoped routing + sizing, user-scoped Guacamole/RemoteApp, no shared AUTO_DEMO routing ambiguity, no
cross-tenant operational data, load + adversarial isolation tests pass, and existing production is unaffected.
