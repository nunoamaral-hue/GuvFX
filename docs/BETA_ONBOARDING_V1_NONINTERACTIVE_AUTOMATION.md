# GFX Beta Onboarding V1 — Revised Non-Interactive Automation Package (for approval)

> **Status: FOR APPROVAL — this is PLANNING, not procurement authority.** Per Nuno's 2026-07-20 revision,
> the 5-user beta **removes all customer-facing MT5 terminal access** (no RDP, no RemoteApp, no
> Guacamole-to-Windows, no visible terminal, **no RDS CALs/SALs**). Beta users interact **only** through the
> GuvFX web app; each hosted broker account keeps an isolated **headless native-Windows portable MT5 runtime
> + bridge** for automated execution/sync/modify/recovery/monitoring with **no customer session ever open**.
> This document is the revised topology + provider comparison + exact monthly cost + Windows licensing
> assumptions + startup/session design + security analysis (12 technical proofs) + 10-runtime capacity +
> implementation sequence + proof/soak criteria. It authorises **NO purchase, NO paid server, NO licence
> buy, NO architecture-dependent spend.**
>
> **Why this is credible, not novel:** the headless model is **already proven in production** on Nuno's box —
> Wayond + ti_signals trade today via an autologon console session running portable MT5 + a bridge, surviving
> RDP disconnect and self-healing after reboot. This package **generalises that proven pattern**, per-account
> isolated and web-controlled, minus interactive access — and **removes the entire RDS cost layer**, cutting
> the incremental beta cost from the prior ~$915/mo RDS design to **≈ $50–60/mo**.
>
> **Governance invariants (throughout):** onboarding stays CLOSED (`BETA_ONBOARDING_ENABLED` off);
> `can_deploy_automation` stays False for `beta`; Nuno's existing Windows host / MT5 runtimes / broker
> accounts / Guacamole / strategies / routing / lot sizes / AUTO_DEMO are untouched and out of scope;
> TI Signals and Wayond execution unchanged; no shared Windows desktop; no customer Windows login; no
> fallback to any unowned runtime (fail-closed to None). Interactive terminal access is rendered as
> **"NOT AVAILABLE DURING BETA"** — never "failed", never "provisioned". The data + entitlement models needed
> to add RDS/RemoteApp later are **preserved** (a future separately-licensed feature) but **no RDS is built or
> procured now**.
>
> **Companion docs:** [`…ARCHITECTURE_OPTION_A.md`](BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md) ·
> [`…OPERATIONS_CAPACITY_SLO.md`](BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md) ·
> [`…PROCUREMENT_PACKAGE.md`](BETA_ONBOARDING_V1_PROCUREMENT_PACKAGE.md) (the superseded RDS package — the
> RDS/RemoteApp topology there is now a **future** feature, not beta) · [`…PROGRAMME.md`](BETA_ONBOARDING_V1_PROGRAMME.md).
>
> **Provenance & price integrity.** Researched live on the web (multi-provider fan-out + per-provider
> adversarial price verification) and passed a completeness critic (verdict *REVISE BEFORE SENDING*; all
> findings resolved: the security section was re-authored to the headless model, the Contabo 24-month-vs-
> monthly term caveat added, the RAM-only capacity ceiling corrected, path/UX strings unified). Every figure
> is tagged **MEASURED / ESTIMATE / quote-required**; no quote-required figure is presented as exact.

---

## Table of contents

- **§1 Revised Windows topology (headless, no RDS)**
- **§2 Ten-runtime capacity estimate**
- **§3 Startup & session-management design**
- **§4 Security analysis & the twelve technical proofs**
- **§5 Proof plan & soak criteria**
- **§6 Provider comparison & Windows licensing assumptions**
- **§7 Cost comparison, recommendation & exact monthly cost**
- **§8 Implementation sequence (non-procurement software)**
- **§9 Preserving the data & entitlement model for future interactive access**
- **§10 What Nuno is asked to approve**

---

## 1. Revised Windows topology (headless, no RDS)

> **Scope note:** This is PLANNING for approval only. It authorises no procurement, no paid server, no licence purchase, and no architecture-dependent spend. Onboarding stays **CLOSED**; `can_deploy_automation = False`. Nuno's existing production box, MT5, broker, Guacamole, strategies, routing, lot-sizes, and AUTO_DEMO are **untouched and out of scope**. TI Signals and Wayond are unchanged.

---

### What was removed vs. the prior Option A

The entire Windows-facing remote-desktop delivery layer is deleted from the design:

| Removed component | Why it is gone |
|---|---|
| RD Session Host / RemoteApp | No human ever logs into the automation pool; nothing to publish. |
| RD Connection Broker (RDCB) | No session brokering without sessions. |
| RD Gateway (RDGW) | No RDP-over-HTTPS ingress to broker. |
| RD Web Access (RDWeb) | No customer-facing Windows portal. |
| Guacamole → Windows automation | Web-RDP into the pool is removed. **Guacamole survives only for the existing web app / Nuno's own MT5 viewing**, unchanged. |
| Any public RDP (3389) to the pool | No customer path into Windows at all. |

The automation pool becomes **headless**: no interactive login surface, no gateway, no broker. The backend is the *only* thing that talks to it, over one internal management path.

---

### Shared, unchanged infrastructure (both candidates)

```
                          INTERNET (customers)
                                 │
                                 │  HTTPS 443 only (frontend + API)
                                 ▼
        ┌────────────────────────────────────────────────────────┐
        │  UBUNTU VPS  (EXISTING — UNCHANGED)                      │
        │                                                         │
        │   Traefik ── guvfx.com / api.guvfx.com / guac.guvfx.com │
        │      │                                                  │
        │      ├── Next.js frontend                               │
        │      ├── Django backend (DRF)                           │
        │      ├── workers (execution / listeners)                │
        │      ├── PostgreSQL 16                                  │
        │      └── Guacamole  (web APP + Nuno's MT5 viewing ONLY) │
        │                                                         │
        │   Customers reach ONLY this box. No customer route      │
        │   continues past the backend.                           │
        └───────────────────────────┬────────────────────────────┘
                                     │
                                     │  ★ SINGLE INTERNAL MANAGEMENT PATH ★
                                     │  backend ──► pool ONLY
                                     │  Tailscale private overlay (no public IP)
                                     │  WinRM 5986 / HTTPS bridge+provisioning API
                                     │  mTLS or bearer token, fail-closed
                                     ▼
                        ┌──────────────────────────────┐
                        │  HEADLESS WINDOWS AUTOMATION  │
                        │  POOL   (candidate A or B)    │
                        └──────────────────────────────┘

        ┌────────────────────────────────────────────────────────┐
        │  NUNO'S PRODUCTION BOX (EXISTING — OUTSIDE THE POOL)     │
        │  MT5 + signal bridge + AUTO_DEMO + TI/Wayond routing.    │
        │  NOT provisioned, NOT reachable as a pool node.          │
        └────────────────────────────────────────────────────────┘
```

**The `★` line is the one and only ingress to Windows.** It is:
- private (Tailscale overlay / internal NIC — **no public IP, no port-forward, no DNS**),
- backend-initiated only (customers cannot address it; it is not exposed by Traefik),
- authenticated + fail-closed (mTLS or per-node bearer token; on auth/identity mismatch the bridge refuses and `can_deploy_automation` stays `False`).

**No customer-facing Windows ingress exists in either candidate.** There is no 3389, no RDWeb URL, no Guacamole tunnel into the pool.

---

### Candidate A — single shared automation host (intra-OS isolation)

```
   ★ management path ──► ┌──────────────────────────────────────────────┐
   (WinRM 5986 /         │  WINDOWS HOST  (1 machine)                    │
    HTTPS bridge API)    │  autologon automation identity(ies)          │
                         │                                              │
                         │  Provisioning agent (HTTPS API, localhost/★) │
                         │                                              │
                         │  C:\GuvFX\accounts\1\  ├ portable MT5        │
                         │                        └ bridge :8788        │
                         │  C:\GuvFX\accounts\2\  ├ portable MT5        │
                         │                        └ bridge :87xx        │
                         │  C:\GuvFX\accounts\N\  ├ portable MT5        │
                         │                        └ bridge :87xx        │
                         │                                              │
                         │  Isolation = per-account guvfx_u_<id>        │
                         │  identity + per-account runtime dir + ACLs   │
                         │  (intra-OS; shared kernel/registry/host)     │
                         └──────────────────────────────────────────────┘
```

- **Isolation boundary:** OS-user + filesystem ACL + distinct autologon identity per account (`guvfx_u_<id>`, `C:\GuvFX\accounts\<id>`). All accounts share **one kernel, one registry, one host**.
- **Pros:** cheapest; one OS to patch; simplest provisioning; lowest idle overhead.
- **Cons:** a host-level compromise or a noisy-neighbour resource spike crosses all tenants; no OS/tenant hard boundary; blast radius = whole pool.

### Candidate B — per-user VMs (OS / tenant isolation)

```
   ★ management path ──► ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
   fans out to each      │ VM user-1     │  │ VM user-2     │  │ VM user-N (~5)│
   VM's private NIC      │ autologon id  │  │ autologon id  │  │ autologon id  │
                         │ provision agt │  │ provision agt │  │ provision agt │
                         │ portable MT5  │  │ portable MT5  │  │ portable MT5  │
                         │ bridge :8788  │  │ bridge :8788  │  │ bridge :8788  │
                         │ C:\GuvFX\acct │  │ C:\GuvFX\acct │  │ C:\GuvFX\acct │
                         └───────────────┘  └───────────────┘  └───────────────┘
                          separate OS         separate OS        separate OS
                          separate kernel     separate kernel    separate kernel
```

- **Isolation boundary:** full VM per tenant — separate OS, kernel, registry, memory, and virtual NIC. A single bridge port (:8788) can be reused because each VM is its own address.
- **Pros:** hard OS/tenant boundary; blast radius = one tenant; per-VM resource guarantees; clean per-tenant teardown/restore.
- **Cons:** ~5 small VMs to license, patch, and run (ESTIMATE — see governance note); higher idle cost and ops surface than A.

---

### Recommendation: **Candidate B (per-user VMs)** for the beta, with A as the fallback if capacity forces it

Reasons:

1. **The whole reason this project is blocked is tenant isolation.** Memory of record: the platform is single-tenant / not beta-ready with 21 Critical items, and onboarding is safe *only because it is hard-blocked*. A per-VM boundary is the isolation control that the security posture actually needs; intra-OS ACLs (A) do not give an OS/tenant boundary and leave a shared-host blast radius.
2. **Least-privilege / per-account runtime isolation is a standing security rule.** Per-VM cleanly enforces "components get only the access they need" and keeps one tenant's MT5/bridge from ever addressing another's.
3. **Blast radius.** A compromise or runaway in one tenant's MT5/bridge is contained to that VM, not the whole pool.
4. **Scale is small.** ~5 small VMs for the closed beta is a bounded footprint; the isolation benefit outweighs the extra ops surface at this size. If measured capacity/cost later makes B infeasible, **A remains a valid fallback** because the topology (headless, single management path, no customer ingress) is identical — only the isolation substrate differs.

Both candidates preserve the invariant that matters: **the backend is the sole client of the pool, over one private management path, with zero customer-facing Windows ingress.**

---

### Governance / evidence separation

- **MEASURED (from repo/memory, verified facts):** portable per-account layout `C:\GuvFX\accounts\<id>`; per-account autologon identity `guvfx_u_<id>`; bridge binds `:8788` (agent ports 8787/8788 observed); Guacamole today serves the web app + Nuno's MT5 viewing; existing Ubuntu VPS runs frontend/backend/workers/Postgres/Guacamole; Nuno's production box runs AUTO_DEMO + TI/Wayond routing.
- **ESTIMATE (NOT fact — requires confirmation before any spend):** VM count "~5"; any per-VM licence/host cost; capacity headroom of a single shared host under N accounts; WinRM-vs-HTTPS-bridge choice for the management path. No price or sizing here is authorised or verified.
- **No procurement authorised.** This document selects a shape for review only. `can_deploy_automation` stays `False` and onboarding stays CLOSED until Nuno approves the procurement package and Phase-4 isolation is proven.

---

## 2. Ten-runtime capacity estimate

**Scope of this estimate.** Sizing the pool for a **hard initial cap of 10 continuously running MT5 runtimes** = 5 beta users × ≤2 accounts each. This is PLANNING for approval only. It authorises **no procurement, no paid server, no licence purchase, and no architecture-dependent spend**. Onboarding stays CLOSED; `can_deploy_automation` = False. Nuno's existing host, MT5, broker, Guacamole, strategies, routing, lot-sizes, and AUTO_DEMO are untouched and out of scope; TI Signals and Wayond are unchanged.

### Measured footprint (basis)

| Metric | Value | Provenance |
|---|---|---|
| `terminal64.exe` idle-light RSS | ~100–165 MB | **MEASURED** |
| `terminal64.exe` + signal bridge RSS | ~210 MB | **MEASURED** |
| CPU, steady state | Near-idle, brief tick bursts | **MEASURED** |
| Windows Server base (OS + services) | ~4 GB | **ESTIMATE** (typical Server 2019/2022 desktop-experience baseline; not measured on the target host) |

> **Single-sample caveat.** The per-runtime figures are from a small number of observations of one terminal on one host, not a sustained multi-terminal soak. RSS under real market load, multiple charts, and concurrent tick bursts may exceed the idle-light band. Treat ~210 MB as a working point estimate, not a guaranteed ceiling. All totals below inherit this caveat.

---

### Option (a) — single shared host

Ten GUI terminals co-resident in one Windows host.

| Line | Budget | Basis |
|---|---|---|
| Runtime RAM | 10 × ~210 MB = **~2.1 GB** | MEASURED per-runtime × cap |
| OS + services base | **~4 GB** | ESTIMATE |
| Headroom (GUI/session overhead, Guacamole, buffers, spikes) | **~6–8 GB** | ESTIMATE |
| **Total RAM** | **~16 GB host** | ESTIMATE (fits a 4–8 vCPU / 16 GB host) |
| CPU | 4–8 vCPU; near-idle steady state, absorbs tick bursts | ESTIMATE from MEASURED near-idle profile |
| Disk | ~60–120 GB (OS + 10 portable MT5 dirs + logs/history caches) | ESTIMATE |
| IOPS | Low-moderate; bursty on tick/history writes. SSD-class strongly preferred | ESTIMATE |

**Real unknown — flag for soak, not resolvable by RAM math.** RAM is comfortable at this cap. The genuine risk in the single-host model is **not memory** but the **per-session GUI resource ceiling**: Windows imposes a finite desktop heap and a per-session **USER + GDI object budget**. Ten full GUI `terminal64` instances in a *single interactive session* (charts, windows, handles) is exactly the regime where desktop-heap exhaustion or USER/GDI limits bite — and it fails as "terminal won't open / renders broken," not as a clean OOM. This is **UNVERIFIED** and must be a named **soak item** before any single-host commitment: run 10 GUI terminals in one session under load and watch USER/GDI handle counts and desktop-heap headroom, not just RAM.

---

### Option (b) — per-user VM

One small VM per beta user, each running ≤2 runtimes.

| Line | Per-VM budget | Basis |
|---|---|---|
| Runtime RAM | 2 × ~210 MB = **~0.5 GB** | MEASURED × ≤2 accounts |
| OS + services base | ~2.5–3 GB | ESTIMATE |
| **Total RAM** | **4 GB / VM** (comfortable) | ESTIMATE |
| CPU | **2 vCPU** — near-idle, tick bursts easily absorbed | ESTIMATE from MEASURED profile |
| Disk | ~40–60 GB / VM | ESTIMATE |
| IOPS | Low; SSD-class preferred | ESTIMATE |
| **Fleet** | **5 VMs** (2 vCPU / 4 GB each) → 10 vCPU / 20 GB aggregate | ESTIMATE |

**Why (b) sidesteps the (a) unknown.** Each user's ≤2 terminals sit in their **own session on their own VM**, so the desktop-heap / USER+GDI budget is per-VM and never contended across users. It trades a slightly higher aggregate RAM/OS overhead (5× base vs 1×) for stronger isolation and the elimination of the shared-session GUI ceiling as a single point of failure. It also aligns with the Phase-4 per-account isolation posture.

---

### Explicit cap statement

- **10 continuous runtimes is the initial hard cap** — an operational limit on how many MT5 terminals may run *at once*.
- This is **distinct from the ≤2-accounts-per-user configuration cap**, which limits how many accounts a single user may *register*. At 5 users the two happen to coincide (5 × 2 = 10); they are not the same control and must not be conflated. The continuous-runtime cap is the one that governs pool sizing and must be enforced independently of the per-user account cap.

---

### MEASURED vs ESTIMATE summary

- **MEASURED:** per-runtime RSS (~100–165 MB idle-light / ~210 MB with bridge), near-idle CPU with tick bursts. Single-sample, one host.
- **ESTIMATE:** all host/VM totals, OS base (~4 GB), headroom, disk, IOPS, and vCPU sizing — derived from the measured per-runtime figures plus typical Windows Server baselines. Not independently benchmarked at the 10-runtime cap.
- **UNVERIFIED / SOAK ITEM:** single-session desktop-heap and USER/GDI ceiling for 10 GUI terminals in one session (option a). No estimate here is presented as fact.

*Governance: PLANNING for approval only — no procurement, paid server, licence buy, or architecture-dependent spend authorised. Onboarding remains CLOSED; `can_deploy_automation` = False. No price is asserted as fact.*

---

## 3. Startup & session-management design

> **Governance banner.** This subsection is **PLANNING for approval only**. It authorises **no procurement, no paid server, no MT5/Windows licence purchase, and no architecture-dependent spend**. Onboarding stays **CLOSED**; `can_deploy_automation` remains **False** for all beta users; `AccountRuntime` stays `NOT_PROVISIONED` (the Phase-2 provisioner is not deployed). Nuno's existing single Windows MT5 host, MT5 install, broker accounts, Guacamole, strategies, source-scoped routing, lot sizes, and the live AUTO_DEMO path (TI Signals, Wayond) are **untouched and out of scope**. Nothing here changes the live execution path.

This is the technical crux: how a native Windows host runs **many headless MT5 runtimes with zero customer interactive access**. It is not a new invention — it generalises the pattern Nuno **already runs in production today** (`docs/OPERATIONS_DASHBOARD.md`: "autologon console-session model; bridge started via RDP; self-heals via autologon + logon tasks"), moving it from *one* manually-started terminal to *N* supervised, per-account terminals under the `terminal_provisioning` app and the `AccountRuntime` state machine.

---

### (a) One non-admin automation identity per host, autologon into the single console session

Each host boots and **autologons a single dedicated automation account into the interactive console session (Session 1)**. This is the account MT5 GUI terminals run under.

- The autologon identity is a **non-administrator, host-local service identity** (e.g. `guvfx_svc_host<N>`), distinct from the per-account `guvfx_u_<id>` identities that `AccountProvisioning` already mints (`terminal_provisioning/models.py`: `is_admin` *"MUST remain False"*, runtime under `C:\GuvFX\accounts\<id>\`). It has **no RDP rights, no customer-facing login, and no admin group membership** — least-privilege per the security rule.
- MT5 is a **GUI application** and needs a real interactive desktop with a window station to run reliably; it does **not** run well as a bare Windows Service. Autologon gives it exactly one persistent interactive desktop without a human ever sitting at it. This is the crux of "headless": headless to the *customer* (no interactive access is ever granted), but a real console desktop exists for the terminals to paint into.
- **No customer ever receives an interactive session on these hosts.** There is no Guacamole/RDP grant to beta customers in this design; the viewer path stays governed by the existing viewer≠trading rule (PX-7A) and is out of scope here.

MEASURED: the single-host, single-console, autologon+bridge pattern is proven in production today (one terminal, one bridge on `:8788`). ESTIMATE: everything below about *multiple* terminals per console is a design projection to be validated by soak test (see (f)); it has not been measured at N>1.

---

### (b) Per-account terminal64.exe + bridge launch and supervision (Proof 1 — auto-start after reboot)

Each account's runtime is the portable-directory layout `AccountProvisioning` already records: `runtime_root = C:\GuvFX\accounts\<id>\`, with a `runtime_structure` map (`terminal\`, config, logs). Portability means `terminal64.exe /portable` reads config and writes logs **inside that per-account tree** — no shared `%APPDATA%`, no cross-account state bleed.

Launch and supervision, grounded in the proven `start_signal_bridge.bat` pattern:

- **On-logon bootstrap.** A single on-logon Scheduled Task (running as the autologon identity, in the console session) starts the **supervisor**. This mirrors today's on-logon self-heal, generalised from one terminal to a roster.
- **Supervisor / watchdog process** reads the durable roster — every `AccountRuntime` whose owning account is entitled and marked deploy-eligible — and for each one launches: (1) `terminal64.exe /portable` bound to that account's `runtime_root`, and (2) the per-account **signal bridge** on a **per-account port** (today's single bridge is `:8788`; each runtime gets its own bound port with its own agent token, preserving per-account runtime isolation).
- **State is driven through the machine, not inferred.** The supervisor calls `record_transition()` (`runtime_state.py`) as it works: `QUEUED → PROVISIONING → STARTING → AUTHENTICATING → RUNNING`, appending immutable `RuntimeEvent` evidence at each step. The **user-facing state is derived only from the durable `AccountRuntime.state`** (`user_facing_state()`), never from a transient probe — so a momentary liveness blip never lies to the panel.
- **Proof 1 (auto-start after reboot):** power-cycle the host → autologon → on-logon task → supervisor → each rostered runtime reaches `RUNNING` with a fresh `STARTING`/`AUTHENTICATING`/`RUNNING` event chain, **with no human touching the box**. Acceptance = every eligible runtime returns to `RUNNING` within an SLO window after boot, evidenced by the `RuntimeEvent` timeline.

MEASURED: single-runtime auto-recovery via autologon+logon-task is proven today. ESTIMATE: the supervisor managing an *N-runtime roster* is new code (Phase-2, not built) — the mechanism is a generalisation of a proven one, but N-runtime behaviour is unmeasured.

---

### (c) Why the console-session terminals survive admin RDP connect/disconnect (Proof 2)

The classic Windows/MT5 operational rule is: **"don't run the terminal in your RDP session."** When you RDP *into* a box and launch an app in your RDP session, then **disconnect**, that session's desktop can be torn down / the session goes disconnected and GUI apps in it lose their interactive desktop — they die or freeze. This is precisely the failure this design designs *around*.

- The trading terminals run **only in the autologon console session (Session 1)**, launched by the supervisor — **never** in an admin's RDP session.
- For maintenance, an admin connects over RDP as a **separate admin identity into a separate RDP session** (Session ≥2). Connecting or **disconnecting that admin RDP session does not touch Session 1**: the console session and its terminals keep running with their desktop intact.
- The admin never launches or "adopts" a terminal in their own RDP session. Maintenance actions (patching, log inspection, restarting the supervisor) operate on Session 1's processes from the admin session but the *GUI terminals stay parented to the console desktop*.
- **Proof 2 (RDP resilience):** admin RDP-connects for maintenance, then disconnects → all Session-1 runtimes remain `RUNNING`, bridges stay reachable, no `DEGRADED` transition is recorded. Acceptance = zero runtime state change across an admin connect/disconnect cycle, evidenced by the absence of `DEGRADED`/`STARTING` events in the `RuntimeEvent` timeline during that window.

This directly matches the existing production behaviour captured in `docs/LIVE_TRADING_RISK_WATCH.md` (autologon console session is the desktop dependency; a **console** logoff — not an RDP disconnect — is the disruptor, and autologon restores it).

---

### (d) The watchdog: liveness probe, restart-on-crash, and the idempotency guard (Proof 8 — no duplicate orders)

The supervisor is also a **watchdog**. Per runtime, on a fixed cadence it runs a **liveness probe**: process-alive check on `terminal64.exe` + a health call to that runtime's bridge port + a bounded staleness check on the runtime's last heartbeat.

- **On failure:** transition `RUNNING → DEGRADED → REPAIRING` (durable, with a sanitised `reason_code`), then attempt an ordered restart of the terminal and/or bridge, with backoff via `AccountRuntime.attempt` / `next_retry_at`. Success → back to `RUNNING`; exhausted retries → `FAILED` (which surfaces as attention, per `_ATTENTION`).
- **The idempotency guard is the safety-critical part.** A crash-restart must **never re-emit an order that was already sent** — a terminal can crash *after* an `order_send` succeeded at the broker but *before* our records settled. Before the watchdog **re-arms** a restarted runtime, it performs a **reconcile step**:
  1. Query the broker via that runtime's bridge for **current open positions + recent deals** (the source of truth the bridge already exposes: `/mt5/positions`).
  2. Cross-check against `ExecutionJob` / `Trade` records for that account (the same reconcile discipline already used by the orphaned-RUNNING-PLACE_ORDER reconciler in memory: *reconcile against `Trade`, never re-run, alert-if-missing*).
  3. Only **enqueue-only** re-arm — the runtime resumes consuming *new* signals; it does **not** replay in-flight intents. Any job that was `RUNNING` at crash time is reconciled (matched → mark done; unmatched → alert, do **not** re-send), never blindly re-executed.
- This preserves the standing **no-unrestricted-LLM/automation live-trading authority** posture: a restart is a *recovery* action, gated by reconciliation, not an order-generating action.
- **Proof 8 (restart does not duplicate orders):** kill a terminal immediately after a simulated order, let the watchdog restart+reconcile, and assert **exactly one** broker position/deal and **one** `ExecutionJob` for that intent — zero duplicates. Acceptance = position/deal count and `ExecutionJob` count both unchanged across the crash-restart cycle, evidenced end-to-end.

MEASURED: the reconcile-not-replay discipline is proven in the existing orphaned-job reconciler and the SYNC short-lease work. ESTIMATE: wiring it into a crash-restart watchdog loop is new and unmeasured.

---

### (e) Reboot / patch handling

- **Planned patch window.** Admin (separate RDP session) signals a drain: supervisor transitions eligible runtimes `RUNNING → STOPPING → STOPPED` cleanly (bridge stops accepting new work; in-flight jobs allowed to settle or are reconciled), then the host is patched/rebooted. On boot, path (b) brings everything back to `RUNNING` with the reconcile guard from (d) covering any intent that straddled the reboot.
- **Unplanned reboot / crash.** Identical recovery path (b)+(d): autologon → supervisor → per-runtime start → reconcile-before-re-arm. No human step; no order replay.
- **Windows Update discipline.** Reboots are **scheduled inside declared maintenance windows**, not left to automatic surprise reboots, because an unannounced reboot mid-signal is the worst case — recoverable by (d) but avoidable by scheduling. Patch state is an operational SLO item, not something this planning doc procures.
- **State honesty during maintenance:** because the panel reads only the durable `AccountRuntime.state`, a runtime in `STOPPED`/`STARTING` during a patch window is shown truthfully as STOPPED/PROVISIONING, never falsely as RUNNING.

---

### (f) The GUI-session ceiling — a soak-test item, not a RAM calculation

The per-host runtime count is **not** bounded by RAM alone. All GUI terminals in **one Windows session share that session's desktop heap and the USER/GDI object limits** (per-session and per-process caps on window handles, GDI objects, and the interactive desktop heap allocation). Each `terminal64.exe` is a full GUI app consuming window handles and GDI objects; stacking many into a single console session can exhaust **session GUI resources** (handle/heap starvation → terminals fail to paint, hang, or refuse to launch) **long before RAM or CPU is the limit**.

Consequences for the design:

- The realistic **runtimes-per-host** figure must be established by **soak test** (launch N portable terminals in one console session, run for a sustained period, watch USER/GDI handle counts, desktop-heap headroom, and watchdog `DEGRADED` rate) — **not** derived from a `RAM ÷ per-terminal-MB` division.
- This is called out explicitly as an **open, MEASURED-pending capacity item**. Any per-host density number in the capacity plan is an **ESTIMATE until the soak test runs**; it must never be presented as a measured fact or used to justify a host count or spend.
- Mitigations if the ceiling is low (all future, all needing an approved decision + measured need — no speculative infra now): raise per-session limits within supported bounds, or shard runtimes across more hosts. Density is a soak-test output, not an assumption.

---

**References:** `terminal_provisioning` app — `AccountProvisioning` (per-account `guvfx_u_<id>` identity + `C:\GuvFX\accounts\<id>\` portable runtime, `is_admin=False`), `AccountRuntime` durable state machine + immutable `RuntimeEvent` evidence (`models.py`, `runtime_state.py` `record_transition()`/`user_facing_state()`), and the proven production `start_signal_bridge.bat` / autologon-console / on-logon-task self-heal pattern (`docs/OPERATIONS_DASHBOARD.md`, `docs/OPERATIONS_RUNBOOK.md`, `docs/LIVE_TRADING_RISK_WATCH.md`). All Phase-2 supervisor/watchdog code described here is **not built and not deployed**; this remains planning pending Nuno's approval.

---

## 4. Security analysis & the twelve technical proofs

This section proves that the **headless, no-RDS** MT5 automation design is safe to build and, later, to operate under closed beta. The design under proof is deliberately narrow: beta users interact **only** through the GuvFX web app. There is **no RDP, no RemoteApp, no Guacamole-to-Windows path, no visible terminal, and no interactive customer session** anywhere in the beta data path. Every hosted broker account owns an isolated **native-Windows portable MT5 runtime + bridge** that runs **headless** — automated execution, position sync, trade-modify, watchdog recovery, and monitoring — with **no customer session ever open**.

Two isolation topologies are specified and their boundaries are stated explicitly per proof:

- **Primary (recommended): per-user Windows VMs.** ~5 tiny VMs, each running one user's ≤2 runtimes under a single dedicated **non-admin autologon automation identity**. The VM is the isolation unit, so each user gets an **OS/tenant boundary** enforced by the hypervisor.
- **Fallback: shared single automation host.** All runtimes run under one non-admin autologon identity on one host. Isolation is **intra-OS only**: per-account NTFS ACLs, per-account bridge tokens/ports, per-runtime credential injection, and least-privilege. There is **no** OS/tenant boundary between accounts on this host.

The headless mechanism itself is already **MEASURED** on Nuno's production box: a dedicated non-admin automation account autologs on at boot into the single console session (Session 1); a per-account supervisor / scheduled task launches each account's portable `terminal64.exe` plus its bridge; admin maintenance uses a **separate** RDP session whose disconnect never touches Session 1. This is the behaviour proofs 1 and 2 depend on, and it has been observed to hold. Everything below that describes the *pool at beta scale* (multiple VMs, five users, watchdog fleet) is **ESTIMATE / design-only** until the soak plan runs. Nothing here authorises procurement, and onboarding remains **CLOSED**.

Legend used throughout: **MEASURED** = observed on the existing production box or in shipped code; **ESTIMATE** = designed and reasoned but not yet exercised at beta footprint.

---

### 1. MT5 auto-starts after a Windows reboot

**Mechanism.** The dedicated non-admin automation identity is configured for **autologon** into the single console session (Session 1) at boot. A per-account **supervisor** (a scheduled task registered `At log on` for that identity, or a supervisor service launched by it) starts each account's portable `terminal64.exe` in `/portable` mode against `C:\GuvFX\accounts\<id>\`, followed by that account's bridge. Startup is idempotent: the supervisor checks for an already-running runtime for `<id>` before launching, so a re-entrant trigger cannot double-spawn. On a per-user VM the same mechanism runs inside each VM; on the shared-host fallback one supervisor iterates every provisioned `<id>`.

**How proven.** *Inspection*: the autologon registry keys, the scheduled-task XML (trigger, principal, `/portable` working dir), and the supervisor's pre-launch existence check are read and recorded. *Test*: a cold reboot of the host/VM, then assert — without any interactive login — that (a) the automation identity is in Session 1, (b) one `terminal64.exe` and one bridge exist per provisioned `<id>`, and (c) each bridge answers a health probe on its own port. This is **MEASURED** for a single runtime on the production box; multi-runtime cold-start ordering at beta footprint is **ESTIMATE** pending soak.

### 2. MT5 stays running after the admin maintenance RDP disconnects

**Mechanism.** The terminals live in the **autologon Session 1**, which is owned by the automation identity, not by the administrator. Admin maintenance uses a **separate** interactive session for a **different** identity. Disconnecting (or logging off) that admin session tears down only the admin's session objects; Session 1 and every process in it are untouched. There is **no** session-brokering layer of any kind between the admin session and Session 1 — the two are independent OS sessions, so no broker state can couple the admin's disconnect to the automation processes.

**How proven.** *Inspection*: confirm the terminals' owning session id (`qwinsta` / process `SessionId`) is Session 1 and distinct from the admin's session id. *Test*: with runtimes live, connect an admin RDP session, then disconnect and separately log it off; assert every `terminal64.exe`/bridge PID and session id is unchanged and each bridge still answers. **MEASURED** on the production box (admin disconnect did not disturb Session 1); repeatability across the pool is **ESTIMATE**.

### 3. No auto-login / interactive desktop creates cross-tenant exposure — *the crux*

This is the central isolation claim, and the answer differs by topology; both are stated honestly.

**Per-user VM (primary) — mechanism.** Each user's runtimes run in a **dedicated VM** under a **single** non-admin autologon identity that runs **only that user's ≤2 runtimes**. There is **no customer login, no customer desktop, and no customer-reachable session** in the VM at all — the automation identity's desktop is a service surface, not a product surface. The boundary between users is the **hypervisor/OS boundary**: separate kernels, separate filesystems, separate network namespaces. *Threat closed*: cross-tenant read/write of another user's runtime directory, memory, credentials, or bridge — a process in user A's VM cannot name, signal, or open a handle to anything in user B's VM. *Residual*: hypervisor escape and shared management-plane compromise (mitigated by patching, least-privilege on the host, and keeping the management path private — proof 12).

**Shared-host (fallback) — mechanism.** All runtimes share one OS under one non-admin autologon identity. Because they share a kernel and a user context, isolation is **intra-OS only**: per-account NTFS ACLs on `C:\GuvFX\accounts\<id>\`, per-account bridge tokens/ports, per-runtime injected credentials, and the identity being non-admin. *Threat closed*: casual cross-account file access via filesystem ACLs, and cross-account bridge calls via distinct tokens/ports. *Residual — stated plainly*: runtimes share a user context and desktop, so same-user process enumeration, window-message reach, and shared per-session objects are **not** eliminated; a compromise of one runtime process has a materially larger blast radius than under the VM model. The fallback is therefore documented as *weaker*, acceptable only as a contingency, never as the target posture.

**How proven.** *Inspection*: enumerate identities and their session/VM assignment; confirm exactly one non-admin automation identity per VM (primary) or per host (fallback) and `is_admin = False` for all. *Test (primary)*: from user A's VM attempt to open user B's runtime directory, bridge port, and process handle — assert all fail at the OS/network boundary. *Test (fallback)*: from account A's runtime context attempt to read account B's `C:\GuvFX\accounts\<B>\` and call B's bridge token — assert ACL denial and token rejection, and **record** that same-user process/window reach is *not* denied (documented residual). Design-only; **ESTIMATE**.

### 4. Each runtime uses its own writable portable directory

**Mechanism.** Every account runs `terminal64.exe` in **portable mode** rooted at **`C:\GuvFX\accounts\<id>\`** — the exact convention already encoded by the shipped `terminal_provisioning` app (`AccountProvisioning.runtime_root`, unique-constrained; the model docstring names `C:\GuvFX\accounts\<id>\`). All terminal state — config, logs, `MQL5`, profiles — is written inside that root and nowhere else. The directory is **NTFS-ACL'd** so that only the owning identity (VM automation identity, or the account's ACL on the shared host) can read/write it. Uniqueness is enforced in the model layer (`tx1_uniq_runtime_root`), so two accounts cannot be pointed at the same root.

**How proven.** *Inspection*: read the ACL on each `<id>` root and the `runtime_root` uniqueness constraint. *Test*: start two runtimes and assert each writes only under its own `<id>` root (file-creation watch), and that a non-owning identity is denied read/write by ACL. Path convention and uniqueness are **MEASURED** in shipped code; per-root ACL enforcement at pool scale is **ESTIMATE**.

### 5. Each runtime authenticates to only its assigned broker account

**Mechanism.** Credentials are **1:1 with the account** and injected **per runtime** at start; there is no shared login and no shared credential file. The broker login for account `<id>` is materialised only into that runtime's context (see proof 9 for the injection channel). Because each `terminal64.exe` is a separate process with its own portable config, one runtime holds exactly one broker session and cannot be steered to another account's login.

**How proven.** *Inspection*: confirm the injected credential for each runtime resolves to that account's broker number and no other; confirm no shared credential file exists on any common path. *Test*: start all runtimes, then query each bridge for its connected account/login and assert a 1:1 map to the intended broker account with zero overlap. **ESTIMATE** (design-only); the credential *storage/decrypt* half is **MEASURED** in shipped `trading.crypto` + `terminal_provisioning.services`.

### 6. Bridge requests route only to the owning runtime

**Mechanism.** Each runtime's bridge listens on a **per-account port** and requires a **per-account token**; the backend stores, for each account, the tuple *(endpoint, token)* and targets **only** the owning runtime's endpoint when issuing a job. A request carrying account A's token to account B's port is rejected by B's bridge, and the backend never addresses B's endpoint for A's work. On the shared host this token/port pairing is the primary routing isolation; on the per-user VM it is defence-in-depth on top of the network boundary.

**How proven.** *Inspection*: read the backend's per-account endpoint/token map and each bridge's bound port + token check. *Test*: (a) send a correctly-tokened request to the owning bridge → accepted; (b) replay that request to a *different* account's bridge → rejected; (c) confirm the backend's execution path resolves the endpoint from the owning account only (no wildcard/broadcast). **ESTIMATE**.

### 7. One runtime crash does not affect other accounts

**Mechanism.** Each account is a **separate OS process** (`terminal64.exe` + its bridge), with no shared mutable in-process state. The supervisor scopes recovery to the crashed `<id>` only. On a per-user VM a crash is additionally contained to that user's VM. There is no shared parent whose failure would cascade: the supervisor is a controller, and its restart logic acts per-`<id>`.

**How proven.** *Inspection*: confirm process independence (distinct PIDs, no shared writable handles across accounts) and that the supervisor's restart unit is a single `<id>`. *Test*: forcibly kill one runtime and assert every *other* runtime keeps its broker session, its bridge stays responsive, and no other account's execution stalls; then confirm the supervisor restarts only the killed one (feeds proof 8). **ESTIMATE**.

### 8. Watchdogs restart failed runtimes *without duplicating orders* — the highest-risk proof

This is the proof most able to cause real financial harm, so the mechanism is specified in detail and its default is **fail-safe** (do nothing rather than risk a duplicate).

**Mechanism — reconcile-before-rearm.** The watchdog never simply relaunches-and-retries. The restart sequence is:

1. **Detect** an unhealthy runtime for `<id>` (heartbeat gap / bridge unreachable).
2. **Single-flight lock.** Acquire a per-account lease/advisory lock so only one watchdog acts on `<id>` at a time. This mirrors the existing production watcher pattern (single-flight advisory lock + short lease) already proven for the TP-protection watcher and the execution SYNC lease.
3. **Reconcile before re-arm.** Before re-enabling *any* order-placing activity, reconcile intended state against **ground truth**: query broker **open positions / recent orders** via the bridge and cross-check against the durable **`ExecutionJob`** history. Any job already reflected at the broker is treated as **done** and is **never re-run**.
4. **Enqueue-only.** The watchdog itself places **no** orders. It only restarts the runtime and, where needed, **enqueues** jobs onto the existing execution pipeline, where the standard idempotency/lease guards apply. A restart that finds broker state already consistent enqueues nothing.
5. **Idempotent identity.** Recovery actions carry the originating job/plan identity so a late-arriving duplicate is recognised and dropped, consistent with the platform's existing reconcile-against-`Trade`/never-re-run posture for orphaned `PLACE_ORDER` jobs.
6. **Alert on ambiguity.** If reconciliation cannot establish ground truth (bridge returns partial/unknown), the watchdog **holds** — leaves the runtime in a durable `DEGRADED`/`FAILED` state and alerts — rather than guessing. Doing nothing is the safe default.

The durable **`AccountRuntime`** state machine backs this: transitions are serialised with `select_for_update` and every transition writes an immutable `RuntimeEvent`, so recovery state cannot interleave or be silently lost.

**How proven.** *Inspection*: read the watchdog's ordering (lock → reconcile → enqueue-only), the lease/advisory-lock implementation, and the reconcile query against broker positions + `ExecutionJob`. *Test (must be explicit in the soak plan)*: (a) kill a runtime **immediately after** it has placed an order but **before** the terminal confirms locally; on restart assert the watchdog reconciles, sees the broker position, and **enqueues nothing** — zero duplicate order; (b) induce two watchdog instances against the same `<id>` and assert single-flight admits exactly one; (c) simulate an unknown/partial broker response and assert the watchdog **holds + alerts** rather than re-arming. The primitives (single-flight lease, reconcile-before-act, enqueue-only, immutable state evidence) are **MEASURED** in existing production watchers; their application to *runtime restart* is **ESTIMATE** and is the top item the soak plan must actually exercise.

### 9. Broker credentials encrypted, never in logs or command lines

**Mechanism.** Credentials are **Fernet-encrypted at rest** (`trading.crypto.encrypt_password`; `AccountProvisioning.password_enc`, never exposed via API/UI/audit — enforced in the shipped model). They are **decrypted only at injection time** (`services.py` builds the materialisation spec via `decrypt_password` only when materialising). Injection uses a **file / stdin / named-pipe** channel — **never the command line** (a command-line secret would be visible in `tasklist`/`Get-Process -IncludeUserName` and process listings) and **never logged**. The state machine's user-facing error field is structurally scrubbed: `record_transition` writes only the sanitised `reason_code` into `last_error` (capped, no raw agent string), while raw detail lives on the admin-only immutable event — so a credential or raw broker string cannot leak into the panel.

**How proven.** *Inspection*: confirm at-rest ciphertext only (no plaintext at rest), the injection channel is file/stdin/pipe, and grep the codebase + the runtime launch path for any command-line credential or logged secret (expect none). Confirm `last_error` receives `reason_code` only. *Test*: start a runtime and assert (a) the broker password never appears in any process command line (`tasklist`/WMI), (b) it never appears in bridge/worker/agent logs, and (c) the encrypted-at-rest value round-trips. Encryption-at-rest and the `reason_code`-only scrub are **MEASURED** in shipped code; the file/stdin/pipe injection at runtime start is **ESTIMATE** (the injection executor is Phase-2).

### 10. Runtime status visible via the GuvFX Account Status panel

**Mechanism.** The panel is driven **only** by the durable **`AccountRuntime`** record via `user_facing_state()` — never inferred from a transient health probe. `account_status.build_account_status()` renders the runtime and **hosted-terminal** stages truthfully: the `hosted_terminal` stage is `RUNNING` **only** when `AccountRuntime.state == RUNNING`, otherwise `NOT_CONFIGURED` ("Not provisioned yet"); the payload carries an explicit `terminal_provisioning_available: False` so the UI can never infer a live terminal from a green overall. Until the Phase-2 provisioner is deployed every runtime stays `NOT_PROVISIONED`, so the panel cannot imply an MT5 terminal that does not exist.

**How proven.** *Inspection*: read `account_status.py` (lines mapping runtime state → stage; the explicit `terminal_provisioning_available: False`; `_overall` requiring `hosted_terminal == RUNNING`). *Test*: with a runtime in each state, assert the panel stage equals the durable state's user-facing mapping and never over-reports. **MEASURED** — the panel logic is shipped (Increment 3) and unit-tested (`tests_account_runtime.py`).

### 11. Users configure and operate strategies entirely from the web app

**Mechanism.** All user actions — assign a strategy, enable/disable AUTO_DEMO, view status, view trade history — are web-app + REST operations against the existing DRF surface; none require Windows access. Strategy assignment/enablement is surfaced in the same status stages (`strategy_assigned`, `strategy_enabled`) the panel already renders. The user has **no** credential for, and **no** network route to, any Windows host (proof 12).

**How proven.** *Inspection*: confirm every beta user action maps to an authenticated web/API endpoint and that none of them requires or exposes a Windows path. *Test*: complete the full configure→enable→observe loop for an account using only the web app, with the user holding no Windows access, and assert success. Web-app operability is **MEASURED** for existing flows; the end-to-end loop *including a live hosted runtime* is **ESTIMATE** (runtime is Phase-2).

### 12. No RDS role or customer-access path enabled accidentally

**Mechanism.** The design keeps the customer surface web-only and asserts the **absence** of any interactive-access machinery:

- The Windows **Remote Desktop Services role and its sub-features are NOT installed** on any pool host or VM — no session-host role, no connection-broker role, no gateway role, no web-access role (this is the *only* place these components are named, and here only to assert they are absent).
- **No Guacamole-to-Windows** connection exists in the beta path; Guacamole's admin/MT5 remote-desktop use stays outside the beta data path.
- The backend reaches the pool **only** over a **private internal management path** (WinRM/HTTPS to the bridge/provisioning agent). There is **no public/customer Windows ingress** — no customer-reachable RDP/HTTP listener on any pool host.
- The interactive-terminal field is **hard-wired** to the exact string **"NOT AVAILABLE DURING BETA"** in the panel — never "failed", never "provisioned" — consistent with `terminal_provisioning_available: False`.

**How proven.** *Inspection*: on each host/VM, assert the RDS role/features are not installed (`Get-WindowsFeature` shows them absent) and no customer-facing remote-access listener is bound to a public interface; confirm the management endpoint is bound to the private network only; confirm the panel string constant. *Test / CI*: a **host-inspection check** (and a CI assertion over the panel/status code) that fails if any RDS Windows feature is present, if any customer-reachable Windows port is exposed, or if the interactive-access field is anything other than "NOT AVAILABLE DURING BETA". The panel string + `terminal_provisioning_available: False` are **MEASURED** in shipped code; the host-inspection/CI gate is **ESTIMATE** (to be authored with the provisioner). *Future-feature note*: if interactive terminal access is ever offered post-beta, it would be introduced as its own governed workstream (RemoteApp / a brokered path or equivalent) behind an explicit human-gated decision — it is **out of scope** here and its features stay uninstalled during beta.

---

### Isolation posture summary

| Topology | Boundary delivered | Residual risk |
|---|---|---|
| **Per-user Windows VMs** *(primary/recommended)* | **OS/tenant boundary per user** (hypervisor). One non-admin autologon identity per VM runs only that user's ≤2 runtimes; no customer login, no customer desktop, no cross-VM handle/port/path reach. | Hypervisor escape; shared management-plane compromise; per-VM footprint cost. Boundary strength is **ESTIMATE** until soak. |
| **Shared single automation host** *(documented fallback)* | **Intra-OS isolation only**: per-account NTFS ACLs on `C:\GuvFX\accounts\<id>\`, per-account bridge tokens/ports, per-runtime injected credentials, non-admin identity. | Runtimes share a kernel, user context, and desktop → same-user process/window reach and shared session objects are **not** eliminated; larger blast radius from a single compromised runtime. Explicitly the *weaker* option; contingency only. |

Both topologies share the same headless core (autologon Session 1 + per-account supervisor, no customer session), the same portable-directory convention, the same credential handling, and the same reconcile-before-rearm watchdog. The **only** axis that changes between them is the strength of the inter-account boundary; the recommendation is the per-user VM precisely because it upgrades that boundary from intra-OS to OS/tenant.

### Residual risks / assumptions

- **Single-sample footprint.** The headless behaviour (proofs 1–2) is MEASURED on exactly one production box with one runtime. Multi-runtime, multi-VM, five-user behaviour is **ESTIMATE**; nothing about concurrent cold-start ordering, resource contention, or fleet watchdog behaviour has been observed at beta footprint.
- **GUI-session desktop-heap ceiling is a soak unknown.** Multiple `terminal64.exe` GUI processes in a **single** interactive session (Session 1) consume per-session desktop-heap and window-station resources. How many runtimes one session sustains before heap exhaustion or GUI instability is **not measured** and is a primary soak question — it also bears on the per-VM ≤2-runtime sizing assumption.
- **Autologon session hardening.** Autologon stores/derives a credential for the automation identity and leaves Session 1 logged in. This must be hardened (non-admin identity, restricted rights, secured autologon secret, no customer reach into Session 1) and that hardening is **assumed, not yet verified**.
- **Shared-host fallback's weaker boundary.** If the fallback is ever used, same-user process/window reach between accounts is a **known, accepted** residual (proof 3). It should be treated as a temporary contingency with compensating monitoring, not a steady state.
- **Watchdog duplicate-order safety is the top soak item.** The reconcile-before-rearm primitives are MEASURED individually in existing watchers, but their composition for *runtime restart* (proof 8) is **ESTIMATE** and must be adversarially tested (kill-after-order-before-confirm; dual-watchdog; unknown broker response → hold+alert) before any live authority.
- **Provisioner/injection executor is unbuilt.** Credential *injection* at runtime start, the host-inspection/CI gate (proof 12), and the Phase-2 provisioner do not exist yet; their proofs are inspection/test **designs**, not executed results.
- **Design-only, unbuilt, onboarding CLOSED, no procurement.** This entire section is planning documentation. It authorises **no** procurement and **nothing** runs against production. Beta onboarding remains hard-CLOSED, and promotion of any part of this to a running state requires the governance decision path and Nuno's explicit approval.

---

## 5. Proof plan & soak criteria

> **Status of every number below:** all thresholds (minutes, days, %) are **ESTIMATE** target thresholds set for planning — **none has been MEASURED** on an Option A host, because no Option A host exists yet. The proof plan defines what must be *executed and met* before the beta opens; it does not assert any result. Each proof is `PENDING` until run against a real 2-user session host. Evidence for each is captured to the manifest schema under `evidence/schema/` (exact command + actual result + limitations), per the evidence rule — `PASS` is recorded only when the criterion actually ran and met.

**Preconditions common to all proofs.** A headless Windows automation host — for the recommended topology, **per-user VMs** (each with its own non-admin autologon identity); for the shared-host fallback, a single host with ≥2 non-admin identities (`guvfx_u_<id>`) — each owning ≥1 isolated portable MT5 runtime under its own per-account directory (`C:\GuvFX\accounts\<id>\`), driven by the `AccountRuntime` state machine (`RuntimeEvent` immutable audit on). **No RDS role, no RemoteApp, no customer Windows ingress.** Runtimes are automated (AUTO_DEMO-style) demo/paper terminals only — **no live authority, no customer ingress, onboarding CLOSED, `can_deploy_automation=False`**. Nuno's existing prod host / MT5 / broker / Guacamole / strategies / routing / lot-sizes / AUTO_DEMO are **out of scope and untouched**; TI Signals and Wayond are unchanged.

---

### The 12 technical proofs

**Proof 1 — Host reboot → full runtime recovery (autologon + scheduled tasks)**
- **TEST:** With all runtimes in `RUNNING`, hard-reboot the session host (OS `shutdown /r`, and separately a power-cycle). On boot, autologon establishes each `guvfx_u_<id>` session and the per-runtime scheduled tasks relaunch each portable terminal.
- **PASS:** Every runtime that was `RUNNING` before reboot returns to `RUNNING` (terminal64 process up, bridge reachable, `RuntimeEvent` shows a clean `START`) within **N = 5 min (ESTIMATE)** of power-on, with **0** runtimes left in `STOPPED`/`ERROR`. No manual intervention.
- **SOAK metric:** recovery-time-to-RUNNING per runtime; count of runtimes recovered / expected.

**Proof 2 — Admin RDP coexistence (admin work does not disturb automation)**
- **TEST:** Connect an interactive **admin RDP** session, launch and use a terminal/app, then disconnect (and separately log off). Observe automated runtimes throughout and for **M = 30 min (ESTIMATE)** after disconnect.
- **PASS:** All automated runtimes stay `RUNNING` for the full window — the admin logon/logoff does not tear down `guvfx_u_<id>` sessions, does not disconnect their MT5 terminals, and injects no duplicate bridge on `:8788` (guards from TX-RDP4D hold). **0** unexpected `STOP`/`ERROR` events attributable to the admin session.
- **SOAK metric:** runtime uptime across an admin connect/use/disconnect cycle; unexpected-stop count (must be 0).

**Proof 3 — No cross-tenant exposure from auto-login / interactive desktop** *(there is NO customer RemoteApp/Guacamole-to-Windows path in this design — this proof tests the automation session's tenant boundary, not a customer viewer)*
- **TEST:** Two variants by topology. **(Per-user VM, recommended):** from user A's VM, attempt any network/OS path to user B's VM's runtime, portable dir, credentials, or process — there is no shared OS, so the attack surface is only the network. **(Shared-host fallback):** from within the single autologon automation session, attempt to read/modify another account's `C:\GuvFX\accounts\<id>\` dir, its stored credential material, and to enumerate/terminate its `terminal64` — relying only on the intra-OS NTFS ACLs + least-privilege. Also confirm the autologon identity is **non-admin** and that **no customer credential can authenticate to Windows at all** (customers have no Windows login).
- **PASS:** Per-user VM: **0** cross-VM reachability beyond the account's own bridge endpoint; the OS/tenant boundary holds. Shared-host: all cross-account file/cred/process operations **denied by OS ACLs** (access-denied, not partial); autologon identity confirmed non-admin; **0** customer Windows logins possible. The auto-login session exposes **no** cross-tenant data in either topology.
- **SOAK metric:** cross-tenant-access count (must be **0**); the honest residual — shared-host relies on intra-OS controls, per-user VM on an OS boundary — is recorded per the security analysis.

**Proof 4 — Per-user identity isolation (no cross-tenant file/process access)**
- **TEST:** From inside `guvfx_u_A`'s session, attempt to read `guvfx_u_B`'s portable runtime directory, config, credential material, and to enumerate/terminate B's terminal64 process.
- **PASS:** All cross-identity reads and process operations are **denied by OS ACLs** (access-denied, not partial). A cannot open B's data dir, cannot read B's stored credentials, cannot signal B's process. **0** successful cross-tenant accesses.
- **SOAK metric:** cross-tenant-leak count (must be **0**) — the headline isolation invariant.

**Proof 5 — Portable-directory / runtime-state isolation (no shared mutable state)**
- **TEST:** Each runtime uses a distinct portable MT5 directory (own config, logs, MQL data). Mutate config in runtime A (symbol set, chart, EA input) and confirm no effect on B; confirm no shared writable path is used by two runtimes.
- **PASS:** A's mutation is confined to A's directory; B is byte-for-byte unaffected. No two runtimes share a writable config/data file (verified by path map + checksums of B's files before/after). **0** shared-state collisions (root cause echo of the past "leg-evidence collision" class of bug — must not recur).
- **SOAK metric:** shared-path collision count (must be 0); checksum drift on unrelated runtimes (must be 0).

**Proof 6 — Routing / arming isolation (a signal reaches exactly one account; arming is per-tenant)**
- **TEST:** Inject a demo signal targeted at account A. Separately, have user B **arm/disarm** a signal source. Observe A and B routing outcomes.
- **PASS:** A's signal executes only on A's runtime and **never** on B's; B's arming/disarming does **not** fail-close or alter A's auto-copy (the multi-tenant fail-close regression identified in V1 is proven fixed). Each execution's `AccountRuntime`/plan lineage ties to exactly one owner. **0** mis-routed executions.
- **SOAK metric:** mis-route count (must be 0); cross-tenant arming side-effects (must be 0).

**Proof 7 — Single terminal crash isolation + targeted watchdog restart**
- **TEST:** With multiple runtimes `RUNNING`, forcibly kill **one** `terminal64` (`taskkill /F` on a single PID).
- **PASS:** All *other* runtimes remain `RUNNING` and undisturbed; the watchdog detects the dead runtime and restarts **only that one** back to `RUNNING` within **N = 5 min (ESTIMATE)**, emitting a `CRASH`→`RESTART` `RuntimeEvent` pair. The watchdog does **not** restart, duplicate, or touch any healthy runtime. Restart count for healthy runtimes = **0**.
- **SOAK metric:** blast-radius (other-runtime disturbances, must be 0); restart-precision (spurious restarts, must be 0); dead-runtime recovery time.

**Proof 8 — Crash mid-order-cycle → NO duplicate order (reconcile-before-arm) — highest-risk proof**
- **TEST:** Drive a runtime into the middle of an order cycle (order intent recorded / in flight to the broker), then crash it (kill terminal64, and separately reboot the host) at the worst moment — after intent, around broker acknowledgement. On restart, the runtime must **reconcile broker state before it re-arms** and must never blindly re-send.
- **PASS:** After recovery, the broker holds **exactly one** order/position for that intent — **0 duplicates** and **0 lost** intents. The runtime reconciles open orders/positions/deals against its own plan/`AccountRuntime` state *before* transitioning back to armed; an intent already present at the broker is marked done (never re-sent), an intent provably absent is either safely retried under an idempotency key or left for human review — never silently re-fired. `RuntimeEvent` shows `RECONCILE` preceding `ARM` on every recovery. (Directly builds on the orphaned-RUNNING-PLACE_ORDER reconciler discipline: reconcile against the trade record, never re-run, alert-if-missing.)
- **SOAK metric:** **duplicate-order count (must be 0)** across all induced mid-cycle crashes — this is the single gating metric of the whole soak. Also: reconcile-before-arm ordering violations (must be 0); lost-intent count (must be 0).

**Proof 9 — No plaintext credentials anywhere on the host (tasklist / command-lines / logs)**
- **TEST:** While runtimes are up, capture full process command-lines (`tasklist /v`, `wmic process get CommandLine` / `Get-CimInstance Win32_Process`), scheduled-task action definitions, environment blocks, and all runtime/bridge/agent log files. Grep every artefact for broker login, password, investor password, tokens, and Fernet material.
- **PASS:** **Zero** plaintext credentials in any command-line, task action, env dump, or log. Secrets are supplied only via the encrypted store / injected out-of-band; logs and process tables show redacted placeholders or nothing. (Evidence redacts file/path/category per the security rule — the secret itself is never reproduced in the manifest, only the *absence* is asserted with the command that proved it.)
- **SOAK metric:** plaintext-credential hits across the soak window (must be **0**), sampled continuously as logs rotate.

**Proof 10 — Per-account sizing isolation (lot override applies to the right account only)**
- **TEST:** Set distinct per-assignment lot overrides on A and B (Inc1 model), inject matching demo signals, and read the resulting demo order sizes.
- **PASS:** A's order uses A's override, B's uses B's; changing A's override never alters B's sizing, and neither leaks to Nuno's global `SignalSourceConfig` sizing. Fail-closed margin guard still holds. **0** sizing cross-bleed events.
- **SOAK metric:** size-mismatch count (must be 0).

**Proof 11 — Density / capacity headroom under sustained load**
- **TEST:** Run the target density (**≤2 users/host — ESTIMATE cap, unproven O-item**) with all runtimes active over the soak window; sample working-set, private bytes, thread count, CPU, and bridge memory per runtime and host-wide (anchored to the read-only prod measurement: ~165 MB WS / 98 MB priv / 16 threads / near-idle CPU per terminal64 + ~33 MB bridge — **MEASURED on prod, single terminal**; multi-runtime aggregate is **UNVERIFIED**).
- **PASS:** No runtime is OOM-killed or throttled; host memory/CPU stay within a defined headroom band (e.g. peak RAM ≤ 80% — **ESTIMATE**) for the full soak; no reliability metric (recovery time, uptime) degrades as concurrency rises to the cap. Density claim ≤2/host is either confirmed or corrected with measured numbers.
- **SOAK metric:** peak/mean RAM & CPU per host; runtime-per-host at which any pass threshold is first violated (records the *real* safe density).

**Proof 12 — Beta stays structurally CLOSED (RDS features absent, no ingress, status truthful)**
- **TEST:** On the current/staging estate, assert the beta cannot serve external users: check RDS/RemoteApp session-host roles are **NOT installed** where the closed gate requires; confirm no public customer ingress path reaches a runtime (no open onboarding, `BETA_ONBOARDING_ENABLED` unset, `can_deploy_automation=False`); load the Account Status panel as a would-be beta user.
- **PASS:** RDS session-host / connection-broker features are **not installed / not exposed**; there is **no** reachable customer ingress; and the truthful Account Status panel shows the interactive-access field as **"NOT AVAILABLE DURING BETA"** (never "failed", never "provisioned", never a false success), per Inc3. **0** externally reachable runtime endpoints.
- **SOAK metric:** exposed-customer-ingress count (must be **0**) — sampled for the whole soak so "closed" is proven continuously, not just at t0.

---

### Overall soak

Run the full proof estate as one continuous soak.

- **Scale:** **10 automated runtimes (ESTIMATE)** across the minimum number of session hosts required at the ≤2/user density (i.e. this soak also stress-tests density beyond the single-host proofs), all demo/paper AUTO_DEMO, onboarding CLOSED throughout.
- **Duration:** **X = 14 days (ESTIMATE)** of continuous running.
- **Induced faults (scheduled, not just observed):** at least — 3 host reboots (mix of graceful + power-cut), 5 single-terminal kills, 3 mid-order-cycle crashes (Proof 8 conditions), 3 admin-RDP connect/use/disconnect cycles, and continuous credential/ingress sampling.
- **Tracked metrics (targets are ESTIMATE):**
  - **Uptime %** per runtime — target **≥ 99.5%** of scheduled running time.
  - **Recovery time** after each induced fault — target **≤ 5 min** to `RUNNING`.
  - **Restart count** — every restart is an *expected, attributed* watchdog/reboot action; **spurious restarts of healthy runtimes = 0**.
  - **Duplicate-order count = 0** (hard gate).
  - **Cross-tenant-leak count = 0** (hard gate — file, process, routing, sizing, or config).
  - **Plaintext-credential hits = 0** (hard gate).
  - **Exposed-customer-ingress count = 0** (hard gate — beta stayed closed the whole time).
  - **Lost / mis-routed signal count = 0.**

Every fault injection, its exact command, and its actual outcome are written to the evidence manifest with limitations noted; negative/failed runs are **retained** (research + evidence rules), not discarded, and a failure resets the clean-soak clock.

---

### Exit criteria that gate opening the beta

The beta may open **only** when **all** of the following hold — any single failure blocks the gate:

1. **All 12 proofs recorded `PASS`** (criterion actually executed and met; no `PARTIAL`/`FAIL`/`PENDING` remaining).
2. **A full X-day soak completed with zero on the four hard-gate counters:** duplicate-order = 0, cross-tenant-leak = 0, plaintext-credential = 0, exposed-ingress = 0.
3. **Uptime ≥ target and recovery ≤ target** sustained across every induced fault, with **zero spurious restarts** of healthy runtimes.
4. **Proof 8 (reconcile-before-arm) demonstrated at least 3× with 0 duplicates**, including the host-reboot-mid-cycle variant — the single most important line.
5. **Measured density confirms (or corrects) the ≤2/user assumption**, so launch host count is based on a MEASURED number, not the current ESTIMATE.
6. **Every open O-item that bears on isolation/recovery** (per-runtime heartbeat, host-death detection, reason-code taxonomy, retry-N, DB-backup gap P6) is either closed or explicitly accepted by Nuno as out-of-gate.
7. **Nuno's explicit approval** on the evidence pack — automated exit criteria being green is a *necessary*, not sufficient, condition; opening the beta is a Red action.

Until every item above is satisfied, **onboarding stays CLOSED and `can_deploy_automation` stays False.**

---

**Governance footer.** This is **PLANNING for approval only.** It authorises **no** procurement, **no** paid server, **no** licence purchase, and **no** architecture-dependent spend. The soak requires a headless automation host (per-user VMs, recommended) that does **not** yet exist; standing up that host is itself gated on Nuno's separate approval of the procurement package. Onboarding remains **CLOSED**; `can_deploy_automation` remains **False**. Nuno's existing host / MT5 / broker / Guacamole / strategies / routing / lot-sizes / AUTO_DEMO are untouched and out of scope; **TI Signals and Wayond are unchanged**. All durations, counts, percentages, and density figures above are **ESTIMATE** targets, not measured results; the only **MEASURED** input reused is the single-terminal prod resource footprint cited in Proof 11.

---

## 6. Provider comparison & Windows licensing assumptions

> The revised design needs only **plain Windows Server VMs** (no RDS). Six routes researched live
> (2026-07-20), tagged MEASURED / ESTIMATE / quote-required. **Key finding:** only **Contabo** has a
> fully public all-in Windows-VPS price at a beta-appropriate size (~€10.49/mo); every cloud and the other
> SPLA hosters leave the Windows-licence line quote-required. **No RDS SAL/CAL is priced anywhere — $0.**

### SPLA-included Windows VPS (Kamatera / Vultr / Contabo)

All three vendors license Windows Server to tenants under Microsoft's **SPLA** (Services Provider License Agreement) — the OS licence is rented monthly, either bundled into the VPS price or added as a per-instance line item. **No RDS SAL (Remote Desktop Services Subscriber Access License) is priced here** — multi-session RDS/RemoteApp licensing is explicitly out of scope for this comparison. Figures below are the *smallest realistically usable* Windows-capable configuration per vendor, not the vendor's headline teaser price.

| Provider | Base VPS (smallest usable) | Windows Server licence (SPLA) | All-in monthly | Confirmable on public page? | VAT / tax |
|---|---|---|---|---|---|
| **Contabo** — Cloud VPS 4 (4 vCPU / 8 GB / 100 GB) | **€5.50/mo** (24-mo term) — MEASURED | **€4.99/mo** add-on — MEASURED | **≈ €10.49/mo** (entry) | Yes (both figures on contabo.com) | EU display **incl. VAT**; non-EU display ex-VAT. Licence scales with VPS size above entry → quote-required (indicative) for larger plans |
| **Vultr** — Cloud Compute, from $2.50/mo (IPv6-only) / **$3.50/mo** (w/ IPv4) — MEASURED | Base compute — MEASURED | **Windows licence fee: quote-required (indicative)** — commonly cited ~$16/mo on entry plans (third-party only; Vultr docs state fee "varies", no public number) | **≈ $19.50/mo** (indicative, ESTIMATE) | Base: yes. Licence amount: **no** (403 on vultr.com/servers/windows; docs give no figure) | USD, **ex-VAT/tax** (added per jurisdiction at checkout) |
| **Kamatera** — Basic Windows VPS (1 vCPU / 2 GB), Windows Server 2022 Datacenter | **$27/mo** — MEASURED (Kamatera states "no premium" for Windows vs Linux) | Bundled per vendor page; **separate licence line-item: quote-required (indicative)** (third-party sources claim a separate Windows charge, conflicting with vendor's "no premium" claim) | **$27/mo** as advertised (ESTIMATE — reconfirm at checkout) | Base: yes ($27/mo on kamatera.com). Licence split: **no** | USD, **ex-VAT** (VAT/tax added at checkout where applicable) |

**Verdict:** **Contabo is the only vendor whose full Windows-VPS cost is confirmable on public pages** (base €5.50 + licence €4.99, entry ≈ €10.49/mo, EU price incl. VAT). Vultr's base compute is public but its Windows licence fee is **not** — treat the ~$16/mo as indicative pending checkout. Kamatera's $27/mo base is public but the SPLA licence portion is not itemised and vendor vs third-party claims conflict — treat all-in as indicative. For a firm three-way comparison, obtain live checkout quotes; do not treat the ESTIMATE cells as committed pricing.

**Verification notes:**
- **Confirmed (MEASURED, public vendor page):** Contabo Cloud VPS 4 €5.50/mo (24-mo) and Windows licence €4.99/mo (`contabo.com/en-us/vps`, `contabo.com/en-us/windows-licenses`); Vultr base Cloud Compute $2.50 IPv6-only / $3.50 IPv4 (`vultr.com/pricing`, Vultr docs); Kamatera Basic Windows VPS $27/mo incl. Windows Server 2022 Datacenter (`kamatera.com/cloud-vps/windows-vps-hosting`).
- **Downgraded to quote-required (indicative):** Vultr Windows licence ~$16/mo — appears only on third-party pages (checkthat.ai); Vultr's own billing doc says the fee "varies… based on your selected Compute plan" with **no figure**, and the Windows-servers pricing page returned HTTP 403 (not machine-verifiable). Kamatera separate Windows licence charge — vendor page asserts "no premium for Windows," while third-party reviews claim a separate line item; the split is unverifiable without a live quote. Contabo Windows licence for VPS larger than entry — scales with size; €4.99 confirmed only for the entry tier.
- **VAT/tax:** Contabo EU-facing prices **include VAT** (24-month effective rate); non-EU display is ex-VAT. Vultr and Kamatera quote **ex-VAT** USD, tax added at checkout by jurisdiction. Stated on each cell above.
- **RDS SAL:** none priced. All figures are per-instance OS licensing only. If the target deployment needs multi-user Remote Desktop Services (RemoteApp/RDS host pool), an additional RDS SAL — **not** covered by SPLA VPS pricing above — would apply and must be quoted separately. Out of scope here by instruction.
- **Volatility / date:** pricing is promotional and term-dependent (esp. Contabo 24-month rates and Vultr reserved discounts); re-confirm at checkout before any decision. Retrieved this session; no fixed as-of date asserted.

---

**Governance:** This is PLANNING for approval only. It authorises **no** procurement, no paid server, no licence purchase, and no architecture-dependent spend. Onboarding stays CLOSED; `can_deploy_automation` = False. Nuno's existing host, MT5, broker, Guacamole, strategies, routing, lot-sizes and AUTO_DEMO are untouched and out of scope; TI Signals and Wayond unchanged. Any vendor selection or spend requires Nuno's explicit approval (Red).

### OVH (incumbent)

**What it is:** Host A — the existing app VPS (`guvfx-prod`). Ubuntu 25.04, OVH Milan region, ~193 GB disk (49% used), running all 11 containers + the Guacamole stack. This is a **Linux control-plane host** already in production; it is **sunk/already-incurred spend shown for total-cost context, not new beta cost** (source: `OPERATIONS_DASHBOARD.md` §1; capacity doc §5, §18).

| Item | Figure | Basis / status |
|---|---|---|
| Monthly cost (incumbent VPS) | **quote-required (indicative) — ~$40–90/mo** | **ESTIMATE / DOWNGRADED.** Not confirmable on any public OVH page; the true figure is on Nuno's OVH invoice. See notes. |
| Microsoft / RDS licensing on this line | **$0 — none** | MEASURED-by-design: Ubuntu Linux host. **No Windows Server, no RDS SAL/CAL priced here.** No SAL/CAL conflation on this line. |
| Currency | USD ($) as written in source | **Unstated FX basis** — OVH bills EU/Milan in **EUR**; the `$` band carries an undocumented conversion. |
| Tax basis (ex-VAT vs incl-VAT) | **NOT STATED in source → treat as ex-VAT, unconfirmed** | OVH worldwide USD VPS pages do **not** declare a tax basis (verified live); only the UK page shows both. The `$40–90` band must state which it is. |
| Backups / snapshots | Not separately itemised on this line | Current OVH VPS list price now **includes** a rolling 24-hour daily backup (verified live); the capacity doc's separate backups line ($10–30) sits under *new* infra, not this incumbent line. |
| Materiality to beta decision | **Low** | Sunk cost; not part of the new-Windows-infra procurement being approved. |

**Verdict:** **Retain as-is; do not treat the dollar figure as a fact.** The OVH incumbent line is a pre-existing Linux control-plane cost, correctly excluded from new beta spend. Its one load-bearing number (~$40–90/mo) is an internal **ESTIMATE** that **cannot be confirmed against any public OVH page** and is downgraded to **quote-required (indicative)**; replace it with the actual OVH invoice line (region, plan/generation, **EUR** amount, **ex-VAT** basis) before it appears in any approval total. The line is **clean of Windows / RDS SAL / CAL conflation** — that risk lives entirely on the *new* Windows RDSH lines, not here; **no RDS SAL is priced on this line.** **Governance: planning only — authorises no procurement, no purchase, no paid server, no licence buy. Onboarding stays CLOSED; `can_deploy_automation` False; Nuno's existing host / MT5 / broker / Guacamole / strategies / routing / lot-sizes / AUTO_DEMO are untouched and out of scope; TI Signals / Wayond unchanged.**

**Verification notes:**

- **CONFIRMED live (public pages, this pass):** OVH's current worldwide VPS list tiers — **VPS-1 $4.54 / VPS-2 $8.50 / VPS-3 $12.32 / VPS-4 $23.37** per month (2/4/6/8 vCore; 4/8/12/24 GB; 40/75/100/200 GB NVMe) — verified on both `ovhcloud.com/en/vps/` and `us.ovhcloud.com/vps/`. The range is now branded **"VPS 2027"** and includes a rolling 24-hour daily backup in the list price. Both USD pages state **no** ex-VAT/incl-VAT basis.
- **CONFIRMED live:** the **1 April 2026** OVH price adjustment, with legacy **Comfort / Elite / Value / Starter / Essentials** (and Eco/Kimsufi/So You Start) families **excluded** from the increase (cdnsun blog). Increase applied to the newer VPS range, select Bare Metal, and extra IPv4.
- **CONFIRMED live:** the UK page (`ovhcloud.com/en-gb/vps/vps-cloud/`) shows **both** bases; the cheapest listed is **VPS-2 at £6.29 ex-VAT / £7.55 incl-VAT** per month. (The original brief cited these two figures without naming the tier — it is the VPS-2 line.)
- **DOWNGRADED (unconfirmable → quote-required, indicative):** the incumbent **~$40–90/mo** figure. Reasons: (1) source labels it ESTIMATE, not a quote; (2) the **193 GB disk matches no current standard tier** (40/75/100/200 GB), so it is a legacy or optioned config not on today's list pages; (3) $40–90 is **above** standard VPS-4 list ($23.37), implying added options or older-gen pricing only the invoice reveals; (4) the April-2026 repricing can stale any recalled figure. This is the only load-bearing number on the line and it stays downgraded.
- **ex-VAT / VAT:** **not stated in the source** and must be added. Recommend expressing the incumbent as the actual **EUR ex-VAT** invoice amount (VAT shown separately), not a USD band. No figure on this line includes VAT.
- **No RDS SAL priced:** confirmed — this is a Linux host carrying **zero** Microsoft licensing; RDS SAL/CAL is out of scope and appears nowhere on this line.
- **Residual price risk:** LOW materiality (sunk cost, excluded from new beta spend) but the displayed band is unverifiable and directionally soft. Action: substitute Nuno's real OVH invoice line before any approval total is struck.

Sources: [OVH VPS (worldwide)](https://www.ovhcloud.com/en/vps/), [OVH VPS US](https://us.ovhcloud.com/vps/), [OVHcloud/Hetzner 2026 price increases](https://blog.cdnsun.com/ovhcloud-and-hetzner-2026-hosting-price-increases-explained/), [OVH UK Cloud VPS (ex/incl-VAT)](https://www.ovhcloud.com/en-gb/vps/vps-cloud/).

---

Audit result: the existing brief's four "Confirmed" public figures (VPS tier prices, April-2026 increase + exclusions, UK ex/incl-VAT £6.29/£7.55) **all verified live** and were left intact. The single load-bearing ESTIMATE (~$40–90/mo incumbent) remains correctly **downgraded to quote-required (indicative)**. ex-VAT/VAT basis is stated (treat as ex-VAT, unconfirmed; express as EUR ex-VAT). **No RDS SAL is priced** on this Linux line. Two additive clarifications made: the UK figure is specifically the VPS-2 tier, and OVH now includes a daily backup in list price (branded "VPS 2027"). No files written.

### Hetzner

> Auditor's note: the source "Hetzner" brief text was **not supplied** to this session — only the audit instruction was. Per the no-assumption rule I did not invent the original figures; the table below is reconstructed from public Hetzner pages and each number is tagged by how well it is confirmed. All prices are **ex-VAT** (Germany/Finland region); Hetzner's German price list **adds 19% VAT**. Prices reflect the **15 June 2026 price adjustment** (new orders; existing rentals keep legacy pricing). Retrieved this session; "today" undefined so no as-of date asserted beyond the 15 Jun 2026 adjustment.

| Item | Spec | Price (EUR/mo, ex-VAT) | +19% VAT (gross) | Status |
|---|---|---|---|---|
| CCX13 dedicated-vCPU cloud | 2 vCPU / 8 GB / 80 GB | €42.99 | €51.16 | **MEASURED** (2 independent sources) |
| CCX33 dedicated-vCPU cloud | 8 vCPU / 32 GB / 240 GB | €138.49 | €164.80 | Confirmed on Hetzner docs (single-source extraction) |
| CCX43 dedicated-vCPU cloud | 16 vCPU / 64 GB / 360 GB | €275.99 | €328.43 | Confirmed on Hetzner docs (single-source extraction) |
| CCX63 dedicated-vCPU cloud | 48 vCPU / 192 GB / 960 GB | €853.49 | €1,015.65 | **MEASURED** (2 independent sources) |
| Dedicated root server (e.g. AX-class) | varies | quote-required (indicative ~€49+ setup varies) | — | **quote-required (indicative)** — model line-up restructured 15 Jun 2026; exact figure not confirmed on a public page this session |
| Windows Server 2022 OS licence (dedicated servers only) | Standard, per-core | €25.40 (8-core) → €152.80 (48-core) | +19% | Confirmed on Hetzner docs (ex-VAT); **cloud CCX does NOT include this** |
| IPv4 address | per server | ~€0.50/mo | +19% | Indicative (Hetzner states €0.50/mo saving for IPv6-only) |
| **RDS SAL / CAL (RemoteApp seat licensing)** | — | **NOT PRICED — OUT OF SCOPE** | — | Excluded per instruction; must be quoted separately (Red) |

**Verdict — PLANNING ONLY, indicative, not procurement authority.** Hetzner compute for an Option-A Windows host pool is confirmable and cheap ex-VAT, but two load-bearing caveats change the picture: (1) **Hetzner *Cloud* (CCX) does not provide Windows licensing** — Windows on cloud is BYOL via KVM console only, so a **Windows RDS host pool realistically needs a *dedicated* server** (where Hetzner sells the Windows OS licence per-core) rather than a CCX cloud instance; (2) **RDS SAL/RemoteApp seat licensing is not priced here and is not clearly covered** by Hetzner's €9.10/mo "Remote Desktop Service" line — it remains out of scope and quote-required. No procurement, paid server, licence buy, or architecture-dependent spend is authorised by this brief. Onboarding stays CLOSED; `can_deploy_automation` False.

**Verification notes:**

- **ex-VAT / VAT:** Confirmed — Hetzner Cloud and dedicated pricing are quoted **ex-VAT**; German price list adds **19% VAT**. Gross column above is my arithmetic (ESTIMATE), not a Hetzner-published figure.
- **CCX13 €42.99 and CCX63 €853.49:** **MEASURED** — matched on two independent public sources (Hetzner price-adjustment docs + wz-it breakdown), region Germany/Finland, post-15-Jun-2026.
- **CCX33 €138.49 / CCX43 €275.99:** confirmed on the Hetzner price-adjustment docs page only (single small-model extraction). Treat as **confirmed-docs**, not independently double-checked; re-verify on the live `hetzner.com/cloud/general-purpose` calculator before any BoM (the marketing page rendered specs but not live prices to my fetch).
- **Dedicated root-server monthly:** **downgraded to quote-required (indicative)** — the ~€39–€49 figures come from secondary blogs, and Hetzner restructured/renamed the dedicated line-up on 15 Jun 2026 (new "-1/-2/-3" designations), so no single public page confirmed a specific model+price this session.
- **Windows OS licence:** Hetzner sells Windows Server 2022 licences **only on dedicated servers**, per-core, ex-VAT (Std €25.40 at 8 cores). **CCX cloud instances cannot use this** — material for architecture choice.
- **IPv4 ~€0.50/mo:** indicative, inferred from Hetzner's stated IPv6-only saving, not a quoted line item.
- **RDS SAL:** **not priced** per instruction. Hetzner lists a separate "Remote Desktop Service" at €9.10/mo but the docs do **not** confirm it satisfies RDS SAL/CAL requirements — excluded, flagged Red, quote-required separately.
- **Not covered / assumed:** no traffic-overage, backup, snapshot, load-balancer, or setup-fee lines verified; single-region (Germany/Finland) only; USA/Singapore surcharges not checked; no live cart/quote pulled (would require an account — not done).

**Governance:** This is PLANNING for approval — it authorises **no** procurement, paid server, licence purchase, or architecture-dependent spend. Nuno's existing host / MT5 / broker / Guacamole / strategies / routing / lot-sizes / AUTO_DEMO and TI Signals / Wayond are untouched and out of scope.

Sources: [Hetzner price-adjustment docs](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/), [Hetzner general-purpose cloud](https://www.hetzner.com/cloud/general-purpose), [wz-it June 2026 breakdown](https://wz-it.com/en/blog/hetzner-price-increase-june-2026-cpx-ccx-alternatives/), [Hetzner Windows 2022 pricing docs](https://docs.hetzner.com/robot/general/pricing/windows-2022-pricing/).

### Azure / AWS (Windows VM, License-Included, NO RDS)

*Scope: a single-session **Windows Server VM** with the OS licence bundled ("License-Included" / PAYG Windows), giving the built-in **2 administrative RDP sessions only**. This is the **NO-RDS** variant: no RD Session Host role, no multi-user RemoteApp, and therefore **no RDS SAL/CAL is priced here — that licence is out of scope for this brief**. Planning only — authorises no procurement. Reference sizing 4 vCPU / 16 GB (Azure `D4s v5`, AWS `m5.xlarge`). Region anchor: US (Azure US region / AWS `us-east-1`). **All prices are ex-VAT, USD, on-demand PAYG unless noted; a UK/EU deployment adds VAT (UK 20%) and reprices in GBP/EUR.***

| Item | Figure | Basis / source | Status |
|---|---|---|---|
| VM compute — 4 vCPU / 16 GB, **Linux** on-demand, US | **$0.192 / hr** (≈ **$140.16 / mo** @ 730 hr) | Third-party Azure/AWS list mirrors (Vantage; Economize confirms `$140.16/mo` for `m5.xlarge`) | **MEASURED** (mirror, not the vendor's own page) |
| Same VM with **Windows** OS, License-Included, on-demand | ≈ **$0.37–0.38 / hr → ~$270–280 / mo** | Linux base + a ~$0.046/vCPU/hr Windows-licence uplift (**arithmetic only**); the rendered Windows rate could **not** be confirmed on any public Azure/AWS page this pass | **quote-required (indicative)** |
| Windows VM with **1–3 yr Reserved / Savings Plan** | ≈ **$100–150 / mo** | Commitment discount, not modelled from a page | **quote-required (indicative)** |
| What "License-Included" actually grants | **Windows Server OS + 2 admin RDP sessions only** | AWS Microsoft-licensing docs / Windows FAQ; Azure Windows PAYG licensing | **MEASURED** (licensing fact) |
| **RDS SAL / RDS CAL (per-user RDP access)** | **Not priced — OUT OF SCOPE** for a NO-RDS bare Windows VM. Required only if the RD Session Host / multi-user RemoteApp role is added (a different design). | Microsoft Learn (RDS CAL); AWS Microsoft-licensing docs | **N/A — excluded by design** |
| Azure AVD control/broker/gateway plane | **$0** (management plane free; pay only for the VM, storage, networking) | Microsoft AVD pricing model | **MEASURED** |
| OS disk / EBS (80–120 GB gp3-class), egress, backup | Not quoted (~$8–12/mo disk + ~$10–40/mo egress/backup, usage-dependent) | Provider storage/egress tiers | **quote-required (indicative)** |

**Indicative single-Windows-VM monthly cost (ex-VAT, NO RDS):** ~**$280–320/mo** on-demand (Windows compute *indicative* + storage/egress) or ~**$110–190/mo** with a 1–3 yr commitment. Only the **$0.192/hr Linux base ($140.16/mo)** and the **$0 AVD control plane** are MEASURED; every Windows-inclusive figure is an ESTIMATE pending a signed quote. Incl-VAT (UK 20%) ≈ **~$336–384/mo** on-demand at quote time.

**Verdict:** A License-Included Windows VM with its 2 built-in admin RDP sessions is a viable NO-RDS host, and **no RDS SAL/CAL cost applies to it** — that charge only appears if the multi-user RDS role is later added. But **the load-bearing cost (the Windows-OS on-demand rate) could not be validated against any vendor page this pass**: only the Linux base and the free AVD control plane are confirmable, so the ~$270–280/mo Windows figure is arithmetic, not a quoted rate, and is downgraded to **quote-required (indicative)**. Do not present any Windows-inclusive dollar figure as a committed cost. A live Azure Pricing Calculator / AWS quote for the exact region, VM size, disk, egress and reservation term is required before any number here enters an approval total. **Governance: planning for approval only — authorises no procurement, no paid server, no licence buy, no architecture-dependent spend. Onboarding stays CLOSED; `can_deploy_automation` False; Nuno's existing Windows host / MT5 runtimes / broker accounts / Guacamole / strategies / routing / lot sizes / AUTO_DEMO are untouched and out of scope; TI Signals / Wayond unchanged.**

**Verification notes:**

- **Confirmed (MEASURED, public mirror pages):**
  - `D4s v5` and `m5.xlarge` are both 4 vCPU / 16 GB; **Linux on-demand $0.192/hr** — Vantage (`instances.vantage.sh/azure/vm/d4s-v5`, `.../aws/ec2/m5.xlarge`). `m5.xlarge` **$140.16/mo** independently confirmed on Economize. Mirror, not the vendor's own committed quote.
  - Azure AVD **management/control plane is free** — you pay only for the session-host VM, storage and networking (Azure AVD pricing page / corroborating guides).
  - **License-Included / PAYG Windows bundles the Windows Server OS + only 2 administrative RDP connections** — it does **not** include any RDS per-user access right (AWS Microsoft-licensing docs; Azure Windows licensing).
- **Downgraded to quote-required (indicative):**
  - **Windows on-demand rate (~$0.37–0.38/hr, ~$270–280/mo)** — the Windows OS uplift could **not** be confirmed on a public Azure or AWS page this pass; it is Linux-base + estimated adder arithmetic only.
  - **Reserved/Savings-Plan Windows figure (~$100–150/mo)** — commitment discount, not page-confirmed.
  - **Storage / EBS, egress, backup** — usage-dependent estimates, not quoted.
- **Out of scope (not priced, by design):**
  - **RDS SAL and RDS CAL** are deliberately excluded — a NO-RDS bare Windows VM uses only the 2 built-in admin RDP sessions and legally needs no RDS access licence. Any RDS SAL/CAL number (e.g. the ~$10/user/mo EC2 SAL or the ~$4.19 WorkSpaces bundle rate) belongs to a *different, multi-user RDS design* and must not be attached to this brief.
  - AVD external per-user access (~$5/user/mo) is likewise not applicable to the 2-admin-session model and is not priced here.
- **ex-VAT / VAT:** every figure above is **ex-VAT, USD, US region**. A UK/EU deployment adds **VAT (UK 20%)** and reprices in **GBP/EUR** — the US/USD anchor and a UK/GBP deployment must not be mixed. Mirror prices are not the vendor's own binding quote.
- **Residual price risk (High on the one load-bearing line):** the Windows-OS rate dominates and is confirmed only at the Linux base; on-demand vs 1–3 yr commitment swings compute ~2×; region other than the US anchor shifts all compute/storage; storage/egress/backup are excluded from the headline. No number here is a purchase authority.

Sources: [Azure D4s v5 (Vantage)](https://instances.vantage.sh/azure/vm/d4s-v5), [AWS m5.xlarge (Vantage)](https://instances.vantage.sh/aws/ec2/m5.xlarge), [m5.xlarge $140.16/mo (Economize)](https://www.economize.cloud/resources/aws/pricing/ec2/m5.xlarge/), [Azure Virtual Desktop pricing](https://azure.microsoft.com/en-us/pricing/details/virtual-desktop/), [RDS CAL licensing (Microsoft Learn)](https://learn.microsoft.com/en-us/windows-server/remote/remote-desktop-services/rds-client-access-license).

---

Audit summary for the caller: only two numbers survive as MEASURED — the `$0.192/hr` Linux base (= `$140.16/mo`, confirmed on Vantage + Economize for both VM sizes) and the `$0` Azure AVD control plane. Every Windows-OS-inclusive rate (`~$0.37–0.38/hr`, `~$270–280/mo`, the `~$100–150/mo` reserved figure) is **not confirmable on a public vendor page** and is downgraded to *quote-required (indicative)*. ex-VAT/VAT basis is stated throughout (ex-VAT USD, +UK 20% VAT for a UK/EU deploy). Per the brief's NO-RDS scope, **RDS SAL/CAL is excluded and not priced** — the bare Windows VM relies solely on the 2 built-in admin RDP sessions. No files written. Governance banner preserved (planning only; onboarding CLOSED; `can_deploy_automation` False; Nuno's estate untouched).

### Windows Server licensing (no-RDS) assumptions

> **PLANNING / FOR APPROVAL ONLY — authorises NO procurement, NO purchase, NO paid server, NO licence buy, NO architecture-dependent spend.** Onboarding stays CLOSED; `can_deploy_automation` = False. Nuno's existing Windows host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO are untouched and out of scope; TI Signals / Wayond unchanged. This brief prices **only the base Windows Server OS + base Windows Server CAL**. **RDS SAL/CAL (per-user Remote Desktop access rights) is explicitly OUT OF SCOPE and is NOT priced here** — a Windows Server Standard licence grants only 2 administrative RDP sessions and is *not* an RDS SAL/CAL. **All Microsoft figures are USD, ex-VAT (US MSRP basis)**; a UK/EU deployment adds VAT (UK 20%) on top. Hetzner figures are **EUR, ex-VAT** (page-stated). Separate MEASURED from ESTIMATE; no figure below authorises spend.

| # | Line item (no-RDS) | Figure as briefed | Verified figure | Status | Basis / VAT |
|---|---|---|---|---|---|
| 1 | Windows Server **2022 Standard** OS, 16-core pack | $1,069 | **$1,069** | **CONFIRMED (MEASURED)** | US MSRP, **ex-VAT**. Public list. |
| 2 | Windows Server **2025 Standard** OS, 16-core pack | $1,069 (briefed as "2022/2025") | **$1,176** | **CORRECTED** | US MSRP, **ex-VAT**. The $1,069 label is valid **only for 2022**; 2025 Standard is **$1,176**. |
| 3 | Windows Server **2025 Datacenter** OS, 16-core pack (context, not the Standard-based design) | not priced | **$6,771** (2022 Datacenter = $6,155) | **CONFIRMED (context)** | US MSRP, **ex-VAT**. Included for completeness; the approved design uses **Standard**, not Datacenter. |
| 4 | **Base** Windows Server User CAL (per authorised user; **NOT** an RDS CAL) | ~$40/user | **~$40 (Open/list low end); single-unit retail ~$40–62** | **quote-required (indicative)** | US, **ex-VAT**. ~$40 is the Microsoft Open/list figure; single-license reseller listings run to ~$62 (e.g. HSSL $62.20). Treat as an indicative band, not a fixed unit price. |
| 5 | Hetzner per-**core** Windows Server Standard add-on, **8-core / 16-core**, monthly (Robot **dedicated** only) | 8c €27.90 / 16c €55.90 per mo | **8c €27.90 / 16c €55.90 per mo** | **CONFIRMED (MEASURED)** | **EUR, ex-VAT** (page: "prices above do not include VAT"). Dedicated-server only — **not** attachable to a 4-vCPU cloud VM; dedicated hardware is a separate cost. |
| 6 | Hetzner USD equivalents of line 5 | 8c $31 / 16c $63 per mo | conversion only | **quote-required (indicative)** | Hetzner bills in **EUR**; the USD figures are FX conversions, not a published USD price. Downgrade to indicative. |

**Verdict:** The base Windows-Server (no-RDS) licensing structure holds, with **one material correction**: the "$1,069 for 2022/2025" line is right for **2022** but understates **2025 Standard**, which is **$1,176/16-core** — use the version-specific figure in any BoM. The base WS User CAL (~$40) is a real, separate compliance line **ONLY on the BYOL / owned-Windows / dedicated-server route** — an RDS CAL alone does **not** grant the underlying Windows Server access right. **Under the RECOMMENDED SPLA-included VPS route (Contabo), the OS licence AND its user access rights are BUNDLED in the monthly VPS price — there is NO separately-purchased base CAL, and this ~$40 line does NOT apply.** Do not add it to the recommended-route total. Its exact unit price is vendor-spread, so where it does apply (BYOL) it is indicative, not fixed. Hetzner's per-core Standard price is confirmed and ex-VAT, but is **dedicated-server-only** (not a cloud-VM add-on) and its USD figures are conversions. **No RDS SAL/CAL is priced in this brief — that per-user access-rights line remains out of scope and separately quote-required (blocking) elsewhere.** Every figure here is a planning input; **nothing authorises procurement, a paid server, or a licence buy — that remains Nuno's explicit gate.**

**Verification notes:**
- **WS 2022 Standard = $1,069/16-core — CONFIRMED** against Microsoft/reseller list pricing (US MSRP, ex-VAT). Matches the briefed figure.
- **WS 2025 Standard = $1,176/16-core — CONFIRMED, and CORRECTS the brief.** Microsoft raised the 2025 MSRP above the 2022 level; the two versions must not share the $1,069 label. WS 2025 Datacenter = $6,771/16-core (2022 Datacenter = $6,155) — confirmed for context only; the design uses Standard.
- **Base WS User CAL ~$40 — DOWNGRADED to quote-required (indicative).** ~$40 is the Open/list low end; a single-unit reseller listing (HSSL) shows $62.20, and search results conflate this base CAL with RDS User CALs ($96–$200/CAL). The CDW single-User-CAL page (P46191-B21) did not return a machine-readable price within timeout, so the exact single-unit price is not confirmable on one clean public page → band, not point.
- **Hetzner 8-core €27.90 / 16-core €55.90 per mo — CONFIRMED (MEASURED)** on `docs.hetzner.com/robot/general/pricing/windows-2025-pricing/`; page explicitly states prices **exclude VAT**. Caveat flagged: the fetched content described the tier set as 2016/2019/2022 while the URL says 2025 — the per-core figures matched, but the exact edition-year labelling on that page should be reconfirmed before any BoM. **Per-core, dedicated-server only.**
- **Hetzner USD $31/$63 — DOWNGRADED to indicative.** Hetzner publishes and bills in EUR; the USD values are FX conversions, not a published price.
- **ex-VAT / VAT:** stated on every line — Microsoft = US MSRP ex-VAT (UK/EU add VAT on top, UK 20%); Hetzner = EUR ex-VAT (page-stated).
- **RDS scope:** No RDS SAL or RDS CAL is priced anywhere in this brief. The Windows Server Standard OS licence (lines 1–2, 5) grants only 2 admin RDP sessions and is **not** a multi-user RemoteApp entitlement; the per-user RDS access line is a distinct, separately-quotable charge held out of scope here.
- **Not covered / assumed:** additional-core packs beyond 16 (4-core add-on SKUs exist, not priced); volume/OLP/CSP discounts off MSRP; Software Assurance; the dedicated-hardware cost required to host the Hetzner per-core Standard licence; live FX rate for EUR→USD. None spot-checked this session.

**Sources:** [Microsoft Windows Server pricing](https://www.microsoft.com/en-us/windows-server/pricing), [Hetzner Windows 2025 pricing](https://docs.hetzner.com/robot/general/pricing/windows-2025-pricing/), [CDW WS2022 1 User CAL P46191-B21](https://www.cdw.com/product/microsoft-windows-server-2022-license-1-user-cal/6772069)

---

## 7. Cost comparison, recommendation & exact monthly cost

> **GOVERNANCE — PLANNING FOR APPROVAL ONLY.** This subsection authorises **no procurement, no paid server, no licence purchase, and no architecture-dependent spend**. Onboarding stays **CLOSED**; `can_deploy_automation` = **False**. Nuno's existing host, MT5 runtimes, broker accounts, Guacamole, strategies, source-scoped routing, lot sizes and **AUTO_DEMO** are untouched and out of scope; **TI Signals / Wayond unchanged**. Every figure below is separated **MEASURED** (verified on a public vendor/mirror page this session) vs **ESTIMATE / quote-required (indicative)**. No estimate or unverified price is presented as fact. No spend proceeds until Nuno approves.

This revised design is **headless (no RDS)**: each beta broker account runs an autologon-hosted MT5 terminal that the backend supervises over one private management path. There is **no RD Session Host, no RemoteApp, no Connection Broker, no customer Windows ingress** — so **no RDS SAL and no RDS CAL is priced anywhere below ($0)**. That single fact is what collapses the cost versus the prior RDS design.

---

### 1. Comparison across Windows-VM routes — both sizings

Sizings match the capacity/topology analyses: **per-user = 2 vCPU / 4 GB** (topology B, 5 VMs); **shared = 4–8 vCPU / 16 GB** (topology A, single host). All figures are the *all-in Windows-capable* monthly cost (base compute **+** SPLA/PAYG Windows OS licence). **No RDS SAL/CAL is included in any cell — out of scope, $0.**

| Route | Per-user 2 vCPU / 4 GB — all-in/mo | Shared 4–8 vCPU / 16 GB — all-in/mo | VAT basis | Evidence |
|---|---|---|---|---|
| **Contabo** (SPLA Windows VPS) | **≈ €10.49** *(Cloud VPS 4 = 4 vCPU/8 GB, verified — over-spec vs 4 GB; a true 4 GB tier is cheaper but quote-required)* | **quote-required (indicative ~€13–18)** *(16 GB plan; base + SPLA licence scale above the €5.50+€4.99 entry)* | EU display **incl. VAT** (24-mo term); non-EU ex-VAT | Base €5.50 + Win licence €4.99 = €10.49 entry — **MEASURED** |
| **Vultr** (SPLA Windows) | **quote-required (indicative ~$19.50)** *(base $3.50 IPv4 is 1 vCPU/1 GB; 2 vCPU/4 GB compute is higher + ~$16 Win licence)* | **quote-required (indicative)** | USD, **ex-VAT** (tax at checkout) | Base $3.50 **MEASURED**; Win licence "varies", no public number → **indicative** |
| **Kamatera** (Windows VPS, bundled) | **quote-required** *(verified anchor $27/mo is 1 vCPU/2 GB — under-spec; 4 GB plan higher)* | **quote-required (indicative)** | USD, **ex-VAT** | $27 base incl. Windows **MEASURED**; licence split **indicative** |
| **Hetzner Cloud (CCX)** | **not a clean route** *(CCX **does not** include Windows licensing; Windows = BYOL only)* | **not a clean route** — same | EUR, **ex-VAT** (+19% DE VAT) | CCX13 €42.99 **MEASURED** but **Linux/BYOL**; Windows sold only on **dedicated** servers |
| **Hetzner dedicated + per-core Win** | **quote-required** *(dedicated box + per-core Std licence 8c €27.90 / 16c €55.90 ex-VAT)* | **quote-required** | EUR, **ex-VAT** | Per-core Win **MEASURED**; dedicated model+price **indicative** (line-up restructured 15 Jun 2026) |
| **Azure / AWS** (Windows License-Included, 2 admin RDP only) | **quote-required (indicative)** *(no page-confirmed Windows rate at this size)* | **quote-required (indicative ~$270–280 on-demand / ~$100–150 reserved)** *(Linux 4 vCPU/16 GB = **$140.16/mo MEASURED**; Windows uplift is arithmetic only)* | USD, **ex-VAT** (+UK 20% if UK/EU) | Linux base + $0 AVD control plane **MEASURED**; Windows-inclusive rate **indicative** |
| **OVH (incumbent)** | n/a — **Linux control-plane host, no Windows SPLA route** | n/a | not stated → treat ex-VAT | Incumbent ~$40–90/mo **quote-required (indicative)**; **excluded/sunk** |

**Read-out:** the only route whose *full* Windows cost is confirmable on public pages at a beta-appropriate size is **Contabo** (base €5.50 + SPLA licence €4.99 = **€10.49/mo entry, MEASURED**). Every cloud (Azure/AWS) and the other SPLA hosters have a **quote-required** Windows-licence line. Hetzner *Cloud* is disqualified for a bundled-Windows route (BYOL only). All routes are **an order of magnitude below** the prior ~$915/mo RDS design.

---

### 2. Exact monthly cost — the two candidate topologies

Windows licensing note for **both**: **RDS SAL / RDS CAL = $0 (none — headless, no RDS role).** The Windows Server **OS** licence is the **SPLA line already inside the all-in VPS price** (rented monthly, not separately purchased). Existing GuvFX **OVH Linux (~$40–90/mo) is SUNK and EXCLUDED** — it is unchanged and is not part of this incremental spend.

#### Topology A — single shared automation host (4–8 vCPU / 16 GB)

One Windows VPS hosting all ≤10 runtimes (intra-OS isolation).

| Line | Ex-VAT | Basis |
|---|---|---|
| Direct infra cash (1 Windows VPS, 16 GB, incl. SPLA OS) | **~€11–16/mo** *(quote-required; verified 8 GB floor = €10.49 incl-VAT ≈ €8.82 ex-VAT; 16 GB scales up)* | Contabo anchor **MEASURED** at 8 GB; 16 GB **indicative** |
| Windows RDS SAL / CAL | **€0 — none** | MEASURED-by-design (no RDS) |
| Existing OVH Linux (sunk) | *excluded* | unchanged |
| **Total incremental (ex-VAT)** | **~€11–16/mo** | **indicative** |

**Worked incl-VAT (UK 20%) example** on the verified 8 GB floor (€8.82 ex-VAT): €8.82 × 1.20 = **≈ €10.58/mo incl UK VAT**; a true 16 GB host ≈ **€13–19/mo incl UK VAT (indicative)**. **First month** = recurring **+** any one-off Contabo setup fee for monthly billing (**quote-required**). **Recurring ≈ €11–16/mo ex-VAT.**

#### Topology B — per-user VMs (5×, 2 vCPU / 4 GB) — RECOMMENDED

Five small Windows VPS, one per beta user (2 accounts each → 10 runtimes; 10 vCPU / 20 GB aggregate, consistent with the capacity plan).

| Line | Ex-VAT | Incl. EU VAT (as displayed) | Basis |
|---|---|---|---|
| Direct infra cash — 5 × Contabo Cloud VPS 4 (4 vCPU/8 GB, incl. SPLA Win OS) | **≈ €44.08/mo** *(5 × €8.82)* | **≈ €52.45/mo** *(5 × €10.49)* | Per-VM all-in **MEASURED**; ex-VAT split **ESTIMATE** (19% DE VAT) |
| Windows RDS SAL / CAL | **€0 — none** | €0 | MEASURED-by-design |
| Existing OVH Linux (sunk) | *excluded* | *excluded* | unchanged |
| **Total incremental** | **≈ €44/mo ex-VAT** | **≈ €52/mo** | Contabo anchor |

**Worked incl-VAT (UK 20%) example:** ex-VAT base €44.08 × 1.20 = **≈ €52.90/mo incl UK VAT** (≈ **$57/mo**). **First month** = **€52.90 + any one-off setup fee**, so first-month **≈ €53–70 incl VAT (indicative)**. **Recurring ≈ €52.90/mo incl UK VAT (€44 ex-VAT).**

> **⚠️ Term caveat (critic fix):** the €5.50 Contabo base underneath this rests on Contabo's **24-MONTH-TERM** rate. On a **no-commitment MONTHLY-billing** basis the base reprices **upward** and adds a **one-off setup fee per VM**. So the **≈ €52.90/mo fleet figure assumes a 24-month commitment**; a true month-to-month figure will be **higher** and **must be re-quoted at checkout** before it enters any approval total. Do not read €52.90/mo as the no-commitment monthly cost. (A 24-month commitment on 5 tiny VMs is itself a decision for Nuno — it trades flexibility for the low rate.)

> **Dramatically cheaper than the prior RDS design.** The abandoned RDS topology ran **~$915/mo on-demand** (the AWS EC2 + RDS-SAL figure from the prior procurement package [`BETA_ONBOARDING_V1_PROCUREMENT_PACKAGE.md` §5]; ~$450–630/mo even on a 1–3 yr Reserved plan). Topology B (24-month term) lands at **≈ $57/mo** and Topology A at **≈ $12–20/mo** — roughly a **~90%+** reduction — driven almost entirely by removing the RDS SAL/CAL and Connection-Broker/RemoteApp licensing and stack. The remaining cost is bare Windows-VPS SPLA rental only. *(The exact % depends on the AWS commitment term compared against and the Contabo monthly-vs-24-month rate; treat "an order of magnitude cheaper" as the robust claim.)*

---

### 3. Recommended provider + topology

**Recommended: Topology B (5× per-user Windows VMs) on Contabo (SPLA-included Cloud VPS).**

| Criterion | Why Contabo + Topology B wins |
|---|---|
| **Cost** | Only route with a **fully MEASURED** all-in Windows price (€10.49/VM); ≈ €52/mo for the whole beta fleet — the cheapest verified option and ~94% under the RDS design. |
| **Per-user isolation** | Topology B gives each tenant a **separate OS/kernel/VM** — the hard tenant boundary the project's 21-Critical isolation blockers actually require. Intra-OS ACLs (Topology A) do **not** provide an OS boundary. This matches the topology doc's Candidate-B recommendation and the security-proof (c) escalation posture. |
| **Ops simplicity** | 5 small identical VMs, one management path (Tailscale, backend-only ingress), no RDS roles to install/patch. Cleaner per-tenant teardown/restore than a shared host. |
| **Incumbency** | Incumbency is **weak here**: OVH is the incumbent but offers **no clean bundled-Windows SPLA route**, so the Windows pool is greenfield regardless. Contabo is chosen on verifiable cost + isolation, not incumbency. |

**Fallback:** Topology A (single Contabo 16 GB Windows host) if measured density/soak makes 5 VMs unnecessary — the headless topology (single management path, zero customer ingress) is identical; only the isolation substrate differs. Topology A trades the per-tenant OS boundary for ~€40/mo of savings and is **not** recommended while tenant isolation is the gating blocker.

---

### 4. Exact procurement action (when — and only when — Nuno approves)

**Quotes to obtain first (written, before any decision):**

1. **Contabo (primary)** — checkout quote for **5 × Cloud VPS with Windows Server SPLA**, exact 2 vCPU/4 GB tier (or the 4 vCPU/8 GB Cloud VPS 4 if no 4 GB Windows tier), **monthly billing** basis, itemised: base price, Windows SPLA licence line, **any one-off setup fee**, and **UK VAT treatment** for a UK buyer.
2. **Vultr** — confirm the **actual Windows licence fee** at 2 vCPU/4 GB (docs say "varies"; downgrade the ~$16 indicative until confirmed).
3. **Kamatera** — confirm the **4 GB Windows plan price** and whether the Windows licence is a separate line (vendor "no premium" vs third-party split conflict).

**Expected cost when approved (Contabo, Topology B):**

- **First month:** **≈ €52.90 incl UK VAT + any one-off setup fee → ≈ €53–70 (indicative)**.
- **Recurring:** **≈ €52.90/mo incl UK VAT (€44/mo ex-VAT, €52.45/mo incl EU VAT) — on a 24-month term.** On no-commitment **monthly billing** this reprices **higher + setup fees** (quote-required); confirm the month-to-month number at checkout.
- **Windows RDS SAL/CAL:** **€0**.
- **Incremental over the excluded OVH Linux sunk cost:** the full ≈ €53/mo — nothing on the existing estate changes.

**No purchase, no server, no licence buy proceeds until Nuno explicitly approves this procurement package (Red action).** `can_deploy_automation` stays **False** and onboarding stays **CLOSED** regardless of any quote obtained. Every quote-required figure above must be replaced with the signed checkout number before it enters an approval total; only the Contabo €10.49/VM entry, the OVH VPS list tiers, the Azure/AWS Linux $140.16 base, the $0 AVD control plane, and the $0 RDS SAL/CAL are **MEASURED** today.

---

## 8. Implementation sequence (non-procurement software)

> **GOVERNANCE (applies to every increment below).** This is **PLANNING for approval**. It authorises **NO procurement, NO paid server, NO licence purchase, and NO architecture-dependent spend**. Onboarding stays **CLOSED** (`BETA_ONBOARDING_ENABLED` unset); `can_deploy_automation` stays **False**. Nuno's existing host, MT5 runtime, broker accounts, Guacamole, strategies, routing, lot-sizes and continuous **AUTO_DEMO** are untouched and out of scope. **TI Signals / Wayond routing, sizing, isolation and listeners are unchanged.** All code lands additive, fail-closed, behind flags defaulting **off**; nothing is wired to live execution.
>
> **What "NO-RDS headless" changes vs the Option A / RDS plan:** the beta path drops the interactive layer entirely — **no Windows customer login, no RemoteApp, no shared/RDP desktop, no Guacamole-to-Windows for customers**. Each beta broker account gets a **headless, autologon-hosted MT5 terminal** that a supervisor keeps running and the bridge routes to. This removes the RDS Session Host role, the Per-User SALs, the RemoteApp publishing, and the RDCB — i.e. the parts that required licence spend and a Connection Broker — so it is **strictly simpler** than the RDS plan while preserving the same durable state machine and ownership chain.
>
> **Evidence legend:** **[MEASURED]** = observed in this repo / prod. **[ESTIMATE]** = design assumption, not yet verified. Estimates are never presented as fact.

**Foundations already in place [MEASURED]** (reused, not rebuilt): durable 14-state `AccountRuntime` machine + immutable `RuntimeEvent` (Inc2 #150); per-account non-admin identity `guvfx_u_<id>` and isolated runtime tree `C:\GuvFX\accounts\<id>\{terminal,profiles,logs,config,audit}` via `Provision-GuvfxAccount.ps1` (TX-1); truthful per-account Account Status panel (Inc3 #151); beta entitlement with `can_deploy_automation=False` (Inc4 #152); atomic `min(10, plan)` broker-account cap (Inc5 #153).

---

### I1 — Provisioning contracts + durable ProvisioningJob (unchanged from the RDS plan)

- **Scope.** Define the request/response **contracts** (typed schemas) for the provisioning verbs — `materialise_runtime`, `inject_credentials`, `start`, `stop`, `repair`, `status` — and a durable **`ProvisioningJob`** record (queued → running → succeeded/failed, attempt counter, `next_retry_at`, sanitised `reason_code`/`detail`), mirroring the existing `AccountRuntime`/`RuntimeEvent` durability contract. Contracts are transport-agnostic and identical whether the executor is headless or a future RDS host — this is the deliberate seam that keeps RDS a drop-in later.
- **Tests.** Contract schema validation; job state-transition unit tests (no illegal transitions); retry/backoff arithmetic; sanitisation (no raw agent strings persisted); idempotent job creation (one live job per account).
- **Gate.** **Green** — additive models/serializers, no executor wired, no live effect.
- **Does NOT touch.** No Windows agent, no MT5, no bridge, no execution worker, no Nuno runtime, no TI/Wayond.
- **DO-NOTs.** No calling out to any host; no auto-provision on account create; contracts must not embed RDS-only fields as required (keep interactive fields optional/reserved).

### I2 — Windows headless provisioning-agent DESIGN + read-only proof harness

- **Scope.** **Design only** (document + interface stubs, no armed execution) for a headless Windows agent implementing I1's contracts: (a) **autologon setup** of a single dedicated service session that hosts terminals (design of the autologon identity and lock-down; not a per-customer interactive login); (b) **per-account portable-dir materialise** into the existing `C:\GuvFX\accounts\<id>\…` tree; (c) **credential inject** (Fernet-decrypt server-side, hand to the terminal via the isolated profile; never logged, never returned); (d) **scheduled-task / supervisor create** to own the terminal process; (e) **start / stop / repair** verbs. Ship a **read-only proof harness** that inspects an existing runtime (process present, tree materialised, config populated) and returns a structured report — **it observes only, it never starts, stops, injects, or sends an order**.
- **Tests.** Harness unit tests against fixture trees; schema conformance of the report; a dry `--check` mode asserting zero side-effects; redaction tests on any surfaced agent output.
- **Gate.** **Amber** — touches the Windows execution surface conceptually; ships as design + read-only harness, armed execution deferred to a later, separately-gated packet. Flag it in handoff.
- **Does NOT touch.** No live provisioning; no autologon actually configured on any host; Nuno's Administrator runtime, kiosk shell, and existing bridge untouched; no TI/Wayond.
- **DO-NOTs.** No customer Windows login / interactive desktop / RemoteApp / Guacamole-to-Windows in any form; the harness must never mutate state; no decrypted credential ever leaves the server boundary or appears in a job record.

### I3 — Watchdog / supervisor with idempotent restart (no duplicate orders)

- **Scope.** Design + implement the **supervisor loop** that keeps each owned headless terminal running: detect crash/exit, restart with **idempotency guarantees** so a restart can never re-place an in-flight or already-executed order. Reuse the existing execution-idempotency posture (single-flight advisory lock, enqueue-only, reconcile-against-`Trade`-before-act, never re-run a RUNNING job) proven in the TP-watcher / orphaned-PLACE_ORDER reconciler work **[MEASURED]**. Restart transitions drive `AccountRuntime` (`DEGRADED`→`REPAIRING`→`RUNNING`) and emit `RuntimeEvent`s.
- **Tests.** Kill-and-restart simulation asserting **zero duplicate PLACE_ORDER**; reconcile-before-restart unit tests; single-flight lock contention; state-transition emission; "restart storm" backoff.
- **Gate.** **Amber** — restart authority near the execution boundary; ships behind a flag defaulting off, exercised only against a disposable non-Nuno fixture runtime.
- **Does NOT touch.** No change to how orders are placed/sized; no change to Nuno's runtime or AUTO_DEMO; no TI/Wayond execution path.
- **DO-NOTs.** Never restart into an order re-send; never adopt an unowned runtime; no autostart of Nuno's terminal.

### I4 — Per-account bridge routing (owning-runtime only)

- **Scope.** Route each account's signals/jobs to **its own** headless terminal/bridge instance and to **no other**. Enforce owning-runtime binding at every gate (resolve → validate → place), extending the existing fail-closed `_get_user_mt5_instance` (Inc0 #148) so a job whose account does not own a live runtime resolves to **None → 409/inactive**, never to a fallback box.
- **Tests.** Ownership-binding unit tests (account A job never reaches runtime B); fail-closed on missing/unowned runtime; no-fallback assertion; isolation regression mirroring the TI↔Wayond 4-layer isolation checks **[MEASURED]**.
- **Gate.** **Amber** — routing is shared structure; additive, behind flag, no beta account live.
- **Does NOT touch.** Nuno's routing, TI Signals source-scoped routing, Wayond routing — all unchanged and asserted unchanged by regression.
- **DO-NOTs.** **No fallback to any unowned runtime.** No shared bridge across accounts. No per-account sizing wired to production execution (routing carries signals only; sizing stays as designed/unwired per Inc1).

### I5 — Capacity-admission for the 10-runtime cap

- **Scope.** An **admission control** that refuses to start a new headless runtime once the concurrent cap is reached, atomically (same `SELECT … FOR UPDATE` pattern as the Inc5 broker-account cap **[MEASURED]**). Cap driven by config; default set to the design ceiling. **Capacity basis [ESTIMATE — do not treat ~16 as a proven ceiling]:** a **RAM-only** division (host RAM ÷ ~210 MB/runtime) yields a **~16-runtime upper bound**, but per the capacity analysis that division is **NOT a valid per-host ceiling** — for a shared single session the real limit is bounded by **per-session GUI / USER+GDI / desktop-heap** resources, which are **unmeasured** and are the primary soak question. So the admission cap defaults to the **10-runtime beta target** (a safety margin), **not** to 16; the true per-host ceiling is soak-pending and must be load-verified before any increase. (Per-user VMs sidestep this: ≤2 runtimes per session.)
- **Tests.** Concurrent-admission race test (no over-admission past cap); cap-config plumbing; truthful rejection surfaced as a state, not a crash; staff-exempt parity with the broker cap.
- **Gate.** **Green/Amber** — additive guard; Amber only in that it defines an operational ceiling.
- **Does NOT touch.** No infra sizing applied; no host provisioned; Nuno's runtime is not counted against or subject to the beta cap.
- **DO-NOTs.** The cap must never be raised to "fit" load without a measured density result; admission must never evict or contend with Nuno's runtime.

### I6 — Account Status wiring incl. hard-wired "NOT AVAILABLE DURING BETA"

- **Scope.** Wire the headless runtime lifecycle into the existing truthful Account Status panel: `NOT_CONFIGURED / QUEUED / BLOCKED / PROVISIONING / RUNNING / DEGRADED / FAILED` reflect the durable `AccountRuntime` state and **never imply a terminal the user can log into**. Add a **hard-wired, always-shown** interactive-access field reading **"NOT AVAILABLE DURING BETA"** (the canonical string used across this package; "INTERACTIVE TERMINAL ACCESS NOT ENABLED" is the equivalent alternative Nuno approved) that is a constant in the headless model (it does not depend on runtime health): it truthfully tells the beta user their automation runs headless and there is no desktop/RemoteApp/RDP to open — never "failed", never "provisioned". This is the user-facing counterpart of the future `interactive_access_enabled` flag (I8), which stays off.
- **Tests.** Snapshot tests that the interactive-access field renders **"NOT AVAILABLE DURING BETA"** in **every** runtime state incl. `RUNNING` (never "failed"/"provisioned"); assertion the panel never emits a "click to open terminal" affordance; truthful mapping tests (a `FAILED` job never reads green).
- **Gate.** **Green** — read-only presentation, additive stage.
- **Does NOT touch.** No terminal-access mechanism is built; no Guacamole entry for customers; Nuno's own viewer/terminal-access page (PX-7A) unchanged.
- **DO-NOTs.** The stage must be a hard constant, not a feature toggle a user can flip; no path that would render an interactive-access link for a beta account.

### I7 — Observability + admin

- **Scope.** Extend the read-only, staff-only ops surface (correlation-id + lifecycle logging, `RuntimeEvent` timeline, provisioning-job status, per-runtime heartbeat) and the **read-only admin beta-estate** (Inc5 #153, no decrypted creds) to cover headless runtimes: per-account runtime state, last transition, attempt/next-retry, admission-cap utilisation. Log-based metrics only; fail-open observability.
- **Tests.** Endpoint tenant-scoping (non-staff scoped, global ops = `IsAdminUser`, per Inc0); no-secret-leak assertions on estate payload; heartbeat metric emission; correlation-id propagation.
- **Gate.** **Green** — read-only, admin-gated.
- **Does NOT touch.** No public exposure of admin/ops; no mutation endpoints; Nuno's data shown only under existing staff scope.
- **DO-NOTs.** No public admin exposure; no decrypted credential, token, or raw agent string in any payload or log.

### I8 — Preserve data + entitlement models for FUTURE RDS / RemoteApp (do NOT build RDS)

- **Scope.** Keep the RDS-capable data model intact so interactive access can be added later without a migration rewrite: **retain the `terminal_provisioning` identities** (`guvfx_u_<id>`, `AccountProvisioning`, `SessionAssignment`, runtime tree) exactly as they are, and add a single **`interactive_access_enabled` entitlement flag defaulting `off`** (with `can_deploy_automation` semantics unchanged). The flag is the documented seam for a future RDS/RemoteApp packet; in the headless model it is always off and I6 renders the `NOT ENABLED` state accordingly.
- **Tests.** Migration adds flag with `default=False`; no existing row flips on; entitlement gate treats off as "headless only"; the interactive-access path is unreachable while the flag is off.
- **Gate.** **Green** — additive nullable/defaulted flag; behaviour-preserving.
- **Does NOT touch.** No RDS Session Host, Connection Broker, RemoteApp publishing, or Per-User SAL logic is implemented; no Guacamole-to-Windows wiring.
- **DO-NOTs.** **Do NOT build RDS.** The flag stays off for all beta accounts; adding it does not authorise, imply, or begin any RDS work.

### I9 — IaC prep for the chosen topology (no paid infra applied)

- **Scope.** Author **infrastructure-as-code scaffolding** (parameterised, un-applied) for the headless topology: the headless host(s), the autologon service session, network/isolation to the bridge, and config for the capacity cap — as reviewable templates with variables and a documented `plan`/dry-run only. Costs remain **[ESTIMATE]** and are carried by the separate procurement package, not by this code.
- **Tests.** `plan`/lint/validate runs clean; no `apply`; secrets sourced from env/secret-store placeholders (never literals); template review checklist.
- **Gate.** **Amber** — defines target topology; ships as un-applied IaC only, flagged in handoff.
- **Does NOT touch.** No cloud/paid resource is created; no existing prod VPS, Guacamole stack, or Windows box is modified; Nuno's estate untouched.
- **DO-NOTs.** No `terraform apply` / no provisioning of a paid server; no licence-bearing resource declared as auto-created; no secret committed.

---

### Hard DO-NOTs (whole Phase-2, non-negotiable)

1. **No licence purchase** — no MT5, Windows, RDS/CAL/SAL, or any software licence bought.
2. **No paid server** — no cloud/VPS/host provisioned or `apply`-ed; IaC stays un-applied.
3. **No external onboarding** — `BETA_ONBOARDING_ENABLED` stays unset; email-verify stays non-sending; **do not bypass or unblock onboarding**.
4. **No per-account sizing wired to production execution** — the Inc1 lot override stays versioned/audited/**unwired**.
5. **No migration of Nuno's runtime** — his host/MT5/broker/Guacamole/strategies/routing/lot-sizes/AUTO_DEMO are out of scope and untouched.
6. **No change to TI Signals / Wayond** — routing, sizing, isolation, and listeners unchanged; asserted by regression.
7. **NO customer Windows login / interactive desktop / RemoteApp / Guacamole-to-Windows** — the beta model is headless; interactive access stays `NOT ENABLED`.
8. **No fallback to any unowned runtime** — every gate is fail-closed to the owning runtime or `None`.
9. **`can_deploy_automation` stays False** and `interactive_access_enabled` stays off for all beta accounts.
10. **No armed execution from the Windows agent design (I2) or supervisor (I3)** without a separate, explicitly-gated packet and Nuno's approval.

**Standing gate.** These increments implement software only. **No procurement, paid server, or licence spend proceeds** until Nuno approves the separate §5 procurement action (binding quote + one SPLA-hoster benchmark first). Onboarding remains **CLOSED** until Phase-4 isolation gates pass.

---

## 9. Preserving the data & entitlement model for future interactive access

**Purpose.** Make an interactive-access feature (RDS/RemoteApp session, or a Guacamole-brokered desktop) *cheap to add later* by keeping the durable data and entitlement shape in place now — while building **none** of the runtime, brokering, or licensed capacity it would need. This is **model-preservation only**. Nothing below creates an RDS session-host role, a Guacamole-to-Windows connection, a CAL/SAL, or any paid resource, and nothing changes what customers can see or do during beta.

---

### 1. What already exists and must be kept, not re-derived

These records are already the system of record and are additive/dormant. The correct move is to *leave them in place*, not to expand their behaviour.

- **`terminal_provisioning.AccountProvisioning`** — the per-account isolation profile: dedicated non-admin Windows identity (`windows_username`, `guvfx_u_<id>`), dedicated runtime tree (`runtime_root`, `C:\GuvFX\accounts\<id>\`), lifecycle `status`, and materialisation flags. `is_admin` is documented as MUST-remain-False. **[MEASURED — present in `models.py`]**
- **`terminal_provisioning.SessionAssignment`** — the customer-session *routing* record with `eligible` / `enabled` switches, explicitly documented as **dormant infrastructure**: "nothing in the live Guacamole/VNC launch path reads this model yet." **[MEASURED]**
- **`terminal_provisioning.AccountRuntime`** + **`RuntimeEvent`** — the durable per-account provisioning state machine (Option A §8) and its append-only evidence. Every runtime currently sits at `NOT_PROVISIONED`; user-facing state is derived only from this durable record. **[MEASURED]**
- **`account_status.build_account_status()`** — already emits truthful stages and, critically, a hard `"terminal_provisioning_available": False` flag so a green overall can never be read as "a terminal exists." **[MEASURED]**

Keeping these means the *identity → runtime → routing* spine that a future interactive session would attach to is already modelled. A later phase wires a broker into `SessionAssignment.enabled`; it does not have to invent the account/identity/runtime mapping.

---

### 2. The single new, future-facing addition: a defaulted-OFF entitlement flag

Add one nullable/false-defaulted entitlement concept so the *permission* to use interactive access is representable before the *capability* exists. Proposed shape (naming to be finalised in the packet, not asserted as built):

```text
# Illustrative — NOT yet implemented; belongs to a future migration, defaults OFF.
class SessionAssignment(models.Model):
    ...
    # Future interactive access entitlement (RDS/RemoteApp or Guacamole desktop).
    interactive_access_enabled = models.BooleanField(default=False)   # a.k.a. can_use_remoteapp
    remoteapp_entitlement      = models.JSONField(null=True, blank=True, default=None)
    guacamole_connection_id    = models.CharField(max_length=128, null=True, blank=True, default=None)
```

Rules for this addition:

- **Defaults OFF / NULL for every account, including Nuno's.** No account is entitled by default; there is no code path that flips it on during beta.
- **Not surfaced.** The flag is not exposed in any customer-facing serializer, form, or UI control. It is a latent column, readable by staff/admin only.
- **Nullable / unused, not removed.** `remoteapp_entitlement` and `guacamole_connection_id` are kept as nullable placeholders so the eventual mapping (which Guacamole connection, what RemoteApp entitlement) has a home. They stay empty. Keeping them nullable-and-unused is deliberately cheaper and less disruptive than dropping-then-re-adding schema later.
- **No behaviour reads it yet.** Like `SessionAssignment.enabled`, this flag participates in *no* live launch, routing, or provisioning path. It is data only.

This is the whole "build": one boolean's worth of durable intent, plus two nullable mapping placeholders. No service, worker, agent, or connection consumes them.

---

### 3. Account Status panel: render interactive access as NOT AVAILABLE DURING BETA

The panel must present interactive access *honestly* — neither as a failure nor as something provisioned — driven purely by the OFF flag. Add one read-only stage to `build_account_status()`:

- **State:** a distinct, non-alarming value — proposed `NOT_AVAILABLE_BETA` (label: **"Interactive access — NOT AVAILABLE DURING BETA"**), **not** `FAILED` and **not** `RUNNING`/`HEALTHY`/provisioned.
- **Driven by the flag:** while `interactive_access_enabled` is False (always, during beta) the stage renders `NOT_AVAILABLE_BETA` with detail such as *"Interactive terminal access is not offered during the closed beta."*
- **Must not affect `_overall`.** The new state is excluded from the overall-severity computation exactly as it is neither `FAILED` nor `DEGRADED`; it must not push an account to `FAILED` or fabricate `HEALTHY`. `_overall()` already keys off specific stages, so the addition is inert to it. **[MEASURED — current `_overall` logic]**
- **Consistency with the existing honesty guard:** the panel already ships `terminal_provisioning_available: False`; the new stage is the *human-readable* counterpart of that same truth and must never contradict it.

Net effect: a customer (and staff) sees a clear "not available during beta" line rather than a missing feature, a red error, or a misleading "provisioned" — with zero implication that an MT5 terminal or remote desktop exists.

---

### 4. Explicit boundary — what is deliberately NOT created

To keep the governance boundary unambiguous, this model-preservation work does **NOT**:

- create or configure an **RDS Session Host / RemoteApp collection**, connection broker, or any Windows RDS role;
- create, map, or test any **Guacamole → Windows connection** (`guacamole_connection_id` stays NULL and unread);
- issue, reserve, or procure any **RDS CAL / SAL**, per-user or per-device licence, or any other licensed capacity;
- stand up a **session-host VM**, image, or pool, or size one;
- flip `SessionAssignment.enabled`, `interactive_access_enabled`, or `AccountRuntime` off `NOT_PROVISIONED` for any account;
- add any customer-facing control, entitlement UI, or "request access" flow.

If/when interactive access is actually built, the remaining work is: (a) provision licensed session-host capacity (architecture-dependent, Nuno-approved spend), (b) populate the mapping placeholders and enable the flag per account under a human gate, and (c) wire a broker that reads `SessionAssignment.enabled`. Preserving the model now removes the *schema-churn and migration* cost from that later effort — nothing more.

---

### Governance / Git Status

- **Status: PLANNING for approval.** This authorises **no** procurement, **no** paid server, **no** licence purchase (no CAL/SAL), and **no** architecture-dependent spend. It is a model-preservation design note only.
- **Onboarding stays CLOSED.** `can_deploy_automation` remains False. The proposed entitlement flag defaults OFF and is not surfaced.
- **Out of scope / untouched:** Nuno's existing host, MT5, broker, Guacamole, strategies, routing, lot sizes, and AUTO_DEMO. **TI Signals and Wayond unchanged.**
- **MEASURED vs ESTIMATE:** All model/field/behaviour references marked **[MEASURED]** were read from `backend/terminal_provisioning/{models.py, account_status.py, runtime_state.py}` at the current commit. The illustrative `SessionAssignment` field additions in §2 are a **proposed** future migration, **not** implemented — no code, migration, or schema change is made by this note. No cost, price, or capacity figure is asserted here.
- **Files (reference, read-only):** `/Users/nunoamaral/Documents/Programming/Python/trading/guvfx/backend/terminal_provisioning/models.py`, `.../account_status.py`, `.../runtime_state.py`. **No files written.**

---

## 10. What Nuno is asked to approve

This package **returns for approval** the revised, much-lower-cost non-interactive design. Nothing executes any purchase.

1. **The revised headless topology (§1)** — no RDS/RemoteApp/Guacamole-to-Windows/CALs; per-account headless portable MT5 runtimes reached only by the backend over a private management path; Nuno's box outside the pool.
2. **The isolation decision (§4/§7):** **per-user Windows VMs (recommended)** — a real OS/tenant boundary per user at ~€10.49/VM — vs a **single shared host** (cheaper, intra-OS isolation only). Recommendation: per-user VMs.
3. **The recommended provider & exact cost (§7):** **Contabo SPLA-included Windows VPS**, 5× per-user VMs ≈ **€52.90/mo incl UK VAT (~$57/mo) on a 24-month term** (**$0 RDS licensing**), vs the prior ~$915/mo RDS design — an order of magnitude cheaper. Obtain the written monthly-billing checkout quote first (monthly reprices above the 24-month rate + setup).
4. **The implementation sequence (§8)** — the authorised non-procurement software increments and their DO-NOTs.
5. **The proof/soak criteria (§5)** and the **12 technical proofs (§4)** as the gate that must pass before the beta opens.

### The two things to decide

- **Topology:** per-user VMs (recommended, OS boundary) vs single shared host (cheapest, intra-OS only). §4 states the isolation each delivers honestly.
- **Contabo term:** the ~€52.90/mo rests on a **24-month commitment**; a no-commitment monthly rate is higher + setup. Approve the term, or ask for the monthly-billing number before deciding.

### Honest headlines

- **Cost collapses ~90%+** vs the RDS design — removing customer terminal access removes the entire RDS/CAL/SAL/Connection-Broker layer. Remaining cost is bare Windows-VPS SPLA rental (~$50–60/mo for the whole beta).
- **The model is already proven** (Nuno's production box); the residual risk is **density** — how many headless GUI terminals one Windows session sustains (desktop-heap/GDI limit) is the top **soak** unknown (§5). Per-user VMs (≤2 runtimes each) sidestep it.
- **The highest-risk proof is #8** (watchdog restart without duplicate orders); §4 addresses it with reconcile-before-rearm + enqueue-only + single-flight, and §5 makes it the top adversarial soak test — but it is **EVIDENCE-PENDING** until the soak runs.

### Reaffirmation

**No procurement, paid server, or licence purchase happens until Nuno approves.** The §8 software work is non-procurement and proceeds behind flags with onboarding CLOSED and `can_deploy_automation` False — it touches no paid infrastructure and no production estate. Nothing here opens onboarding, enables any customer Windows/interactive path, wires per-account sizing to execution, migrates Nuno's runtime, or alters TI Signals / Wayond.
