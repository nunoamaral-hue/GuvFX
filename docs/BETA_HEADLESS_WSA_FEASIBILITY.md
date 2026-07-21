# GFX-PKT-BETA-ONBOARDING-HEADLESS-MT5 — Workstream A: Existing-Infrastructure Feasibility

> **⚠️ VERDICT CORRECTED (2026-07-20): C → A.** My initial verdict C was **wrong** — it conflated *Windows
> desktop-session* isolation with *GuvFX runtime* isolation. Nuno correctly challenged this. A controlled,
> reversible **experiment on the box** (see the Addendum) proves **runtime-level isolation is viable**:
> **6 concurrent isolated portable MT5 runtimes ran in one interactive automation session (Session 1) —
> ~137 MB each, 10.3 GB RAM still free, CPU ~4%, crash-isolated — with Nuno's production terminal
> (Session 3) and bridge completely unaffected**, and the box returned to its exact baseline afterward.
> **Revised verdict: A — the existing box CAN host ≥5 isolated headless beta runtimes at the runtime level**,
> subject to a dedicated non-admin automation identity/session, the provisioning+watchdog system (WS-E), and
> a soak to fix the exact desktop-heap ceiling. **Read the Addendum first; §1–§3 (inventory) stand; §4/§8's
> original "C/limit=0" conclusion is SUPERSEDED by the Addendum.**

Date: 2026-07-20 · Authority: Nuno · Status: **Workstream A gate — PASSED at the runtime level (verdict A).**

---

## 1. Existing-infrastructure inventory (measured, read-only)

**The entire estate (Tailscale, verified):** 6 nodes — `guvfx-ubuntu` (Linux VPS, OVH), **`guvfx-windows-mt5` (the only Windows host)**, `mac-control-node` (macOS), `n8n-raspberrypi` (Linux), `nas` (Linux), `wayond-ubuntu` (Linux). **There is exactly ONE Windows host.**

**The one Windows host — `WIN-RD8VDS93DK7` / 100.79.101.19 — is Nuno's live production trading box:**

| Property | Measured value |
|---|---|
| OS | **Windows Server 2025 Datacenter** (10.0.26100), standalone server (DomainRole 2) |
| CPU | AMD EPYC-Milan, **4 physical / 8 logical cores** |
| RAM | **32 GB total, ~11.8 GB free** |
| Disk C: | 52.8 GB used, **426.6 GB free** (disk is *not* a constraint) |
| RDS features | **None installed** (consistent with the no-RDS direction) |
| Hyper-V | **Not installed** |
| Autologon | **`AutoAdminLogon=1` as `Administrator`** (password stored in registry) |
| Local identities | `Administrator`, `guvfx-rdp`, **`guvfx_u_1`, `guvfx_u_6`, `guvfx_u_7`** (per-account TX-1 identities already exist) |
| Portable runtime dirs | `C:\GuvFX\accounts\{1,6,7}` (~278 MB each; materialised on disk) |
| Running MT5 | **1** terminal64 — `IS6 Technologies MT5` (Nuno's production account) |
| Bridge | 1 python process, **listening on :8788** |

**Ubuntu VPS (for context):** 8 cores, 22 GB RAM, load ~0.06, 12 containers — the web/app/DB/Guacamole-for-the-web-app estate. Unchanged, not a factor in the Windows-hosting question.

## 2. Proven Windows session / startup model (measured via `qwinsta`)

| Session | Id | User | State | What runs there |
|---|---|---|---|---|
| console | 1 | Administrator (autologon) | Active | the signal **bridge** (:8788) |
| rdp-tcp#0 | 3 | Administrator | Active | **the production `terminal64` (IS6)** |
| (guvfx-rdp) | 4 | guvfx-rdp | Disconnected | — |
| services | 0 | — | — | (non-interactive) |

**Startup model, proven:** the box uses **autologon (Administrator) → an interactive console session**, plus interactive RDP sessions. MT5 (`terminal64.exe`) is a **GUI application that requires an interactive session with a desktop** — the production terminal is currently running inside an **interactive RDP session (3)**, and the bridge in the console session (1). Provisioning today is **hand-run** (`Provision-GuvfxAccount.ps1`; `terminal_provisioning/services.py:6`) — there is **no automated `ProvisioningJob` executor** yet.

**The hard Windows constraint (this is the crux — "headless" does NOT remove it):** without the RDS Session-Host role, a Windows Server offers only **1 console session + up to 2 administrative RDP sessions** — a ceiling of ~3 interactive sessions, already largely occupied by production. MT5 needs an interactive desktop, so **you cannot give 5 different beta users 5 isolated interactive sessions on one box without RDS.**

## 3. Capacity read

Disk (427 GB free) and RAM (5 headless terminals ≈ ~1 GB) are **not** the binding constraints. The binding constraints are **(a) the interactive-session ceiling without RDS**, and **(b) the fact that the only Windows host is Nuno's production box**, which the packet forbids touching, binding beta users to, or affecting.

## 4. Feasibility verdict — **C**, with the exact blocker

**Can the existing estate safely host 5 isolated external beta users now? No. Fewer than 5? Also no — the safe number is 0.** The blocker is not capacity or software; it is structural:

> **BLOCKER.** The estate has exactly one Windows host and it is Nuno's live production trading box. Isolated per-user headless MT5 for external users requires a per-user isolation boundary. Without RDS (forbidden), that boundary can only come from:
> - **separate interactive sessions** — impossible for 5 users on one box (≈3-session ceiling, and they'd be on Nuno's box); or
> - **separate VMs** — either **Hyper-V on the production box** (requires installing the role + a **reboot**, which interrupts Nuno's live terminal, and runs 5 guests contending for the box's 32 GB / 4 cores → **affects Nuno's runtime**), or a **separate Windows host** (**new paid infrastructure — forbidden by this packet**).
>
> Running all beta terminals in **one shared session under one identity** provides **no** per-user isolation (fails Workstream B) and co-locates external users on Nuno's production box. **No configuration hosts 5 isolated external beta users on the existing estate without violating a non-negotiable boundary.**

**STOP conditions triggered (any one is sufficient):** *"the existing infrastructure cannot safely host five users"* · *"supporting five requires a new paid server or licence"* · *"the Windows session model cannot safely isolate the runtimes"* · *"a beta user could affect Nuno's runtime."*

## 5. Options considered (and why each fails a boundary — NOT a procurement recommendation)

| Option | Isolation | Why it fails the packet's boundaries |
|---|---|---|
| All beta terminals in one shared session/identity on the prod box | None (intra-OS at best) | No per-user isolation (Workstream B); external users co-located on Nuno's production box; shared desktop-heap → a beta crash can destabilise the session incl. adjacent production. |
| Change autologon to a dedicated automation identity | Still one session | Changing the prod box's autologon + **reboot** = *touching* Nuno's runtime; still one shared session for all beta users; still on Nuno's box. |
| Hyper-V on the prod box → per-user guest VMs (Datacenter licence permits guests) | Strong (OS boundary) | Installing Hyper-V needs a **reboot** (interrupts Nuno's terminal) and 5 guests contend for 32 GB / 4 cores → **affects Nuno's runtime**. Not "untouched," not "safe." |
| RDS multi-session on the prod box | Per-session | **RDS is explicitly forbidden** (and would need CALs/SALs). |
| Separate Windows host | Strong | **New paid infrastructure — explicitly forbidden by this packet.** |

## 6. What the software already has vs. the gap

**Already built (shipped, Phase 0):** the durable ownership model (`AccountRuntime` + immutable `RuntimeEvent`), per-account `guvfx_u_<id>` identities + portable-dir convention (TX-1), the truthful Account Status panel, the atomic ≤10 broker-account cap, per-account bridge on :8788, and the beta-entitlement/closed-gate scaffolding. **The blocker is not the software.**

**Not built:** the automated headless-runtime provisioning executor (async `ProvisioningJob` + Windows agent) — provisioning is still hand-run. This would be Workstream E, but building it is moot until there is a **safe host substrate** to provision onto, which is precisely what Workstream A shows the estate lacks for external multi-tenant use.

## 7. Nuno-non-impact proof

Every command in this workstream was **read-only** (`Get-CimInstance`, `qwinsta`, `Get-Process`, `netstat`, `Get-ChildItem`, `Get-ItemProperty`, `Get-LocalUser`, `Get-PSDrive`, `Get-WindowsFeature`, `tailscale status`). **No state was changed.** Post-inventory verification: Nuno's production `terminal64` = **1 running**, bridge **:8788 LISTENING** — unchanged. No config, no reboot, no process touched. TI Signals / Wayond / AUTO_DEMO untouched.

## 8. Decision required from Nuno (I am NOT recommending procurement)

The milestone as scoped — *5 isolated external beta users on existing infrastructure only, without touching Nuno's production box* — **cannot be satisfied simultaneously on the current estate**, because the only Windows host **is** that production box and per-user isolation without RDS needs a separate session/VM/host the estate doesn't have.

Per the packet ("do not pivot to procurement without a separate instruction"), I am **stopping here**. The choices are Nuno's:

1. **Provide a separate Windows host** for the beta pool (new infrastructure — outside this packet's boundaries; needs a separate instruction). The software is largely ready to provision onto it.
2. **Relax a boundary** — e.g. accept co-hosting beta runtimes on the production box (I would advise against: it forfeits real per-user isolation and risks Nuno's runtime), or accept RDS / a reboot-for-Hyper-V. All are currently forbidden.
3. **Keep beta CLOSED** (status quo) until a separate host is available.

No further build, deploy, or procurement will happen without Nuno's explicit next instruction.

---

*Git status: this document only; no code, migration, or deployment. Onboarding remains CLOSED; `can_deploy_automation` False; production estate untouched.*

---

# ADDENDUM — Runtime-level isolation experiment (verdict corrected C → A)

**Why §4 was wrong.** My original conclusion required *one Windows interactive session per beta user*, and since a non-RDS box has only ~3 interactive sessions, I concluded 0. **Nuno correctly identified the error:** the requirement is five isolated **GuvFX runtimes**, not five Windows desktops. Isolation can live at the **runtime level** (Broker Account → Portable MT5 Runtime → Dedicated bridge → Dedicated ownership → Dedicated routing), with **many** runtimes coexisting in **one** automation session. The burden of proof is experimental — so I tested it.

**Method (controlled, reversible, monitored).** On the production box, using the **clean golden MT5 image** (`C:\GuvFX\golden\mt5\5.0.0.5833`, verified to carry **no saved login** — so test terminals could not connect to any real account), I copied N independent portable instances to `C:\GuvFX\betatest\r{1..6}`, launched them, measured, then **fully cleaned up**. Nuno's production terminal (pid 4336, Session 3) and bridge (:8788) were monitored at every step with an abort trigger. **No live account was logged in; no order was placed.**

**Measured results (2026-07-20):**

| Test | Result |
|---|---|
| Portable MT5 in **Session 0** (non-interactive) | **Starts but does NOT persist** — creates config, no journal, exits within ~10–30 s. Session 0 has no desktop; MT5 needs an interactive one. |
| Portable MT5 in **interactive Session 1** (via scheduled task, `LogonType Interactive`, no password) | **Persists and runs healthily** — journal: `MetaTrader 5 x64 build 5833 started … full recompilation finished`. |
| **6 concurrent** runtimes in Session 1 | All 6 up: **~137 MB each, 821 MB total; 10.27 GB RAM still free; CPU 4%** at rest. |
| **Crash isolation** | Force-killed one runtime → **the other 5 kept running** (separate processes). |
| **Nuno's production** throughout | pid 4336 unchanged (Session 3, 169 MB, 704 handles); bridge :8788 up; box returned to exact baseline (11.13 GB free, 1 terminal = 4336, no test tasks/dirs). |

**Answers to the 10 required determinations:**

1. **Max concurrent MT5 terminals** — **≥6 proven** with vast headroom; RAM-bound ceiling ≈ 10 GB free ÷ ~137 MB ≈ **70+**, but the *real* limit is the **per-session desktop-heap / USER+GDI** budget for many GUI terminals in one session — **soak-pending** (comfortably ≥ the 5–10 beta needs).
2. **Memory footprint** — **~137 MB/terminal** interactive (~78–98 MB at headless start); **5 ≈ 685 MB, 6 = 821 MB** measured. Not a constraint (11 GB free).
3. **CPU footprint** — **~4 % for 6 terminals at rest** (near-idle); tick-burst spikes are bounded and brief.
4. **Stability** — persists indefinitely in an **interactive** automation session (journal healthy); **must not** use Session 0 (starts, doesn't persist).
5. **Recovery** — crash-**isolated** (kill one, others survive). Automatic restart needs the WS-E supervisor/watchdog (the `LogonType Interactive` scheduled task is the proven launch primitive).
6. **Cross-runtime interference** — **none observed**: separate processes, separate portable dirs; one crash did not touch the others or Nuno's terminal.
7. **Upgrade behaviour** — **not yet tested.** Each portable dir is independent → per-runtime upgrade, or a controlled golden-image roll (copy-and-swap). Flagged for the soak.
8. **Broker login isolation** — architecturally per-portable-dir → per-login; clean copies carried no login. **Full login-isolation with real demo accounts is a further proof** (needs demo credentials) — design is sound (this is standard MT5-farm behaviour).
9. **Bridge isolation** — per-runtime bridge (own port/token), as Nuno's box already does on :8788 per account. Multi-bridge fan-out is a WS-E build item; the per-runtime model is proven viable.
10. **Five users on the existing host without exposing Windows** — **YES**: runtimes run headless in an automation session with **no customer access**; 6 coexisted with Nuno's production untouched.

**Revised verdict: A — the existing box can safely host ≥5 isolated headless beta runtimes at the runtime level**, subject to three conditions before beta opens:

- **(i) A dedicated NON-ADMIN automation identity + session for beta** — the experiment ran under the autologon `Administrator` session (Session 1) only to avoid handling passwords; production must use a dedicated automation identity (the `guvfx_u_<id>` identities already exist), in a session **separate** from Nuno's production terminal (proven separable: beta in Session 1, Nuno in Session 3, mutually unaffected).
- **(ii) The provisioning + watchdog system (Workstream E)** — to materialise portable dirs, inject credentials, launch via the proven `LogonType Interactive` task, keep-alive/restart with reconcile-before-rearm, and route per-runtime bridges.
- **(iii) A soak** — to fix the exact desktop-heap ceiling, confirm sustained non-interference with Nuno's terminal, and prove broker-login + bridge isolation with real demo accounts (Workstream L).

**Honest residual for Nuno's judgement:** the beta runtimes would co-reside on the **same physical box** as Nuno's production. The experiment proves they are **session- and process-isolated and non-interfering with ample headroom**, and no beta runtime binds to Nuno's runtime — satisfying "Nuno's runtime remains isolated / no beta binding." But a **box-wide** event (reboot, OS patch, resource exhaustion under a bad soak) affects both. Whether co-hosting beta on the production box is acceptable — versus a dedicated box — is a **risk decision for Nuno**; the experiment shows it is *technically* safe with the three conditions above, not that it is zero-shared-fate.

**Zero-impact proof.** Every mutation was a **test artifact under `C:\GuvFX\betatest\`** (killed by path, never pid 4336) + six `GuvfxBetaTest*` scheduled tasks (all unregistered). Post-cleanup: **only Nuno's terminal (4336) runs, bridge :8788 up, 11.13 GB free, 0 leftover test tasks/dirs** — the box is exactly as found. TI Signals / Wayond / AUTO_DEMO untouched.

**Bottom line:** the runtime-level model is **experimentally proven**, so the earlier STOP/limit-0 is withdrawn. **Workstream A passes at verdict A.** With Nuno's go-ahead I can proceed to Workstreams B/E (isolation design + the provisioning/watchdog system) and then the K/L five-user proof + soak — all on the **existing** box, no procurement.

---

# ADDENDUM 2 — Increment-1 session-mechanism finding (STOP-and-return: a Phase-1 decision is needed)

**Context.** The co-hosted implementation packet's Phase 1 requires *"a dedicated **non-administrative** Windows automation identity and persistent interactive session for beta runtimes … **separate from Nuno's production Windows identity/session**."* Before mutating the production box I investigated the session mechanism **read-only**. Verdict A (runtime coexistence) stands — but the **dedicated non-admin *separate* session** cannot be established on the current box without a change that trips a STOP condition.

**Measured (read-only, 2026-07-20):**
- Identities: `guvfx_u_{1,6,7}` are **non-admin** (good beta candidates); `guvfx-rdp` is an **Administrator**.
- Production already uses `LogonType Interactive` scheduled tasks as **Administrator** (`GuvFX_Autostart`, `GuvFX_SignalBridge`, `GuvFX_BridgeWatchdog`) to start the terminal + bridge.
- **The box has exactly THREE interactive sessions, all Administrator/occupied:** S1 console (autologon **Administrator**) → runs the production **bridge**; S3 RDP (**Administrator**) → runs **Nuno's production terminal**; S4 RDP (**guvfx-rdp**, disconnected) → a full admin desktop (explorer/ServerManager/tailscale+wireguard tray, 24 procs).
- **RDS-RD-Server = not installed** → the non-RDS ceiling is **console + 2 RDP sessions**, and **all three are consumed**. `fSingleSessionPerUser=0`, `fDenyTSConnections=0`.

**The blocker.** MT5 only persists in an **interactive** session (Addendum 1). A **dedicated non-admin beta session would be a 4th interactive session** — impossible without RDS. The three that exist are all Administrator and occupied (one by Nuno's terminal, one by the production bridge). Getting a dedicated beta session would require: **changing the production autologon** (it is `Administrator`, and the production terminal/bridge start under it — changing it breaks production startup) — **or** competing for an RDP slot (both used; a 3rd RDP connection at the 2-session cap risks force-disconnecting **Nuno's terminal in S3**) — **or** a reboot that still cannot create a persistent 4th non-admin session without RDS. **Each is a STOP condition** (*"introducing the dedicated beta session requires changing production autologon or restarting Nuno's session"*; *"a Windows reboot is required outside an approved maintenance window"*; RDS is forbidden).

**The only on-existing-box path (relaxes Phase 1) — Option A.** Run beta terminals in the **existing Administrator autologon session (S1)** as separate processes with per-account portable dirs (`C:\GuvFX\beta\accounts\<id>\`) + per-runtime bridges — **session-separated from Nuno's terminal (S3)** and **empirically proven** (6 terminals coexisted in S1 with the production bridge staying healthy). Its honest cost vs Phase 1: beta runs **as Administrator, not a non-admin identity** (weaker identity isolation — a compromised beta runtime would hold admin rights), and it **shares S1's desktop-heap with the production bridge** (a beta-induced session-heap issue could affect the bridge — held at 6 terminals, but a soak risk). This needs **no box reconfiguration and does not touch Nuno's terminal**, but it is a **material relaxation of Phase-1 isolation** that only Nuno can accept.

**Decision required from Nuno (I will not reconfigure the production session model unilaterally):**
1. **Accept Option A** — co-host beta in the shared Administrator autologon session with per-account process/dir/bridge isolation, accepting the admin-identity + shared-bridge-session residual (soak-bounded). Fastest, £0, no production disturbance.
2. **Approve a maintenance window** to attempt a session-model reconfiguration — but note **no single non-RDS box can provide a persistent 4th non-admin interactive session**, so this cannot fully deliver Phase-1 isolation either.
3. **RDS** — would deliver true per-user non-admin sessions, but is **explicitly forbidden** (no RDS/CALs).
4. **A dedicated box** — delivers clean non-admin isolation, but is **procurement** (de-scoped).

**Recommendation.** If staying on the existing box (no procurement), **Option A** is the only viable path — with the identity-isolation relaxation explicitly accepted and the shared-session-heap risk covered by the Phase-12 soak. Otherwise the clean Phase-1 isolation needs a dedicated box (procurement). **Everything else in the programme (runtime-ownership model, capacity service, provisioning-job state machine, per-runtime bridge routing, watchdog, controlled registration, broker onboarding, multi-tenant fan-out, per-assignment sizing) is session-agnostic backend that I can build + test + review now, behind flags, onboarding CLOSED — once the session model is chosen.**

**Zero mutation.** This addendum is read-only investigation only; nothing on the box was changed; Nuno's terminal (S3) + bridge (:8788) untouched.
