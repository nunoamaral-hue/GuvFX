# GFX-PKT-BETA-ONBOARDING-HEADLESS-MT5 — Workstream A: Existing-Infrastructure Feasibility

> **Verdict: C — the existing infrastructure cannot safely host isolated external beta users.**
> Measured safe limit of additional **isolated external** headless beta runtimes on the estate as-is = **0**.
> This is a **STOP-CONDITION** result. Per the packet, I have **not** built Workstreams B–O and have **not**
> pivoted to procurement. Everything below is from **read-only** inspection; **nothing was changed** and
> Nuno's production terminal + bridge were verified still running after the inventory.

Date: 2026-07-20 · Authority: Nuno · Status: STOP-and-return (Workstream A gate not passed).

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
