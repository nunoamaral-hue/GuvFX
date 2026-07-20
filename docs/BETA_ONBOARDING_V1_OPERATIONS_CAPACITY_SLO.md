# GFX Beta Onboarding V1 — Operations, Capacity & SLOs (Option A design extension)

> **Status: DESIGN EXTENSION — for approval; authorises nothing.** This document extends the
> approved **Option A** architecture with the three deliverables Nuno requested before Phase-2
> procurement: (A) detailed **capacity planning**, (B) six operational **runbooks**, and (C) target
> **service-level objectives** — then (D) consolidates the **final procurement package** for approval.
> It is a **plan**. It authorises **no** procurement, **no** VM/licence creation, and **no**
> architecture-dependent (Phase 2+) implementation.
>
> **Companion documents (authoritative for their scope):**
> [`BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md`](BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md)
> (the architecture — ownership model, state machine §8, topology §18) ·
> [`BETA_ONBOARDING_V1_PROGRAMME.md`](BETA_ONBOARDING_V1_PROGRAMME.md) (programme + Phase-0 execution log).
> Section references of the form "§N" point into the Architecture doc unless noted.
>
> **Non-negotiable governance (holds for every section):** Nuno's existing Windows host, MT5 runtimes,
> broker accounts, Guacamole access, strategies, routing, lot sizes and AUTO_DEMO operation are
> **out of scope and untouched** (not part of the pool, excluded from all math). Customer onboarding
> stays **CLOSED** until the Phase-4 isolation gates pass (`BETA_ONBOARDING_ENABLED` off, default);
> `can_deploy_automation` stays **False** for the `beta` plan (an independent server-side execution
> block). No email-verify bypass, no provider commands, no signal replay, no forced or automatic
> trade/close. Every number is marked **MEASURED** or **ESTIMATE**; nothing here presents an estimate
> as a measurement. **Nothing proceeds to Phase 2 until Nuno approves the §D procurement package.**
>
> **Provenance.** Authored and adversarially hardened via a multi-agent workflow (2 ground-truth
> readers → 8 sections author+harden → procurement synthesis → cross-section consistency critic;
> verdict *APPROVE WITH FIXES*, all three cross-section numeric defects and both exclusion gaps
> resolved before publication). Capacity anchored on a **read-only production measurement (2026-07-20)**
> of one live MT5 terminal + bridge on the existing Windows box.

---

## Table of contents

- **A. Capacity planning (detailed)** — per-terminal footprint, automated vs interactive, per-host budget, density, scaling formulas.
- **B. Operational runbooks** — onboarding · password/credential changes · broker migration · single-runtime recovery · host failure · customer support.
- **C. Service-level objectives** — provisioning latency, recovery time, availability, provisioning success, interactive/credential propagation.
- **D. Final procurement package** — refined BoM, licensing, topology, sequence, OPEX, scaling model, approval ask.
- **E. Consolidated open items** — the dependencies Phase 2 must close before any SLO can be *measured* or density raised.

---

## A. Capacity planning (detailed)

> **Status:** DESIGN ONLY — no procurement, no host changes, no architecture-dependent implementation authorised. This section **extends** the basic §3 assumptions; it does not supersede them. Governing principle (unchanged from §3): **density is a Phase-4 proof, not an assumption — raise only with evidence.** Every "budget" here is a planning envelope to be validated in the Phase-4 load test (§19 item 8), **not** an operational SLO — no SLOs exist in the programme yet, and nothing here asserts one.
>
> **Measurement provenance.** The only *measured* input is a **single read-only observation (2026-07-20) of one live MT5 `terminal64.exe` process and its signal-bridge** on the existing production Windows box (AMD EPYC-Milan, 4 physical / 8 logical cores, 32 GB RAM): `terminal64.exe` working set ~165 MB / private ~98 MB / 16 threads / near-idle steady CPU; bridge python ~33 MB. All host, per-user, and density figures below are **derived estimates** built on that one sample plus vendor base-OS typicals. Every value is marked **MEASURED** or **ESTIMATE**. This is **one** observation, not a distribution (no variance / p95 / long-run drift / multi-terminal interference figure); its purpose is to anchor the planning bands, which **must be replaced by a measured distribution in the Phase-4 load test** before any density decision. It does **not** by itself justify a density increase.
>
> **Out of scope / untouched.** Nuno's existing production box, its MT5 runtimes, broker accounts, Guacamole access, strategies, routing, lot sizes, and AUTO_DEMO operation are **not** part of this pool and are excluded from all math below. Onboarding stays **closed** (`BETA_ONBOARDING_ENABLED` off, default); `can_deploy_automation` stays **False** for the `beta` plan until Phase-4 gates pass. Nothing in this section places, sizes, or closes a trade, or opens onboarding.

### 3.1 Empirical per-terminal resource model

**MEASURED (single sample — one terminal, one broker, one moment on the current box):**

| Metric | Idle-light band | Charted-active band | Source |
|---|---|---|---|
| `terminal64.exe` working set (RAM) | ~100–165 MB | ~200–350 MB | MEASURED |
| Thread count | ~16 | ~16 | MEASURED |
| Steady-state CPU | near-idle | near-idle | MEASURED |
| Transient CPU | tick-burst spikes (magnitude unmeasured) | tick-burst spikes (magnitude unmeasured) | MEASURED (qualitative) |
| Bridge process (per automated runtime) | ~33 MB | ~33 MB | MEASURED |

**Single-sample caveat (critical).** This is **one observation of one terminal**, not a distribution: no variance, no p95, no long-run drift, no multi-terminal interference figure. RAM is known to climb as history/chart buffers fill (§3.2) and as symbols/timeframes are added. Treat the bands as **rough central tendencies, not bounds.** The purpose of Phase-4 is to replace this point with a measured distribution (see §3.6) before any density increase.

**Planning values derived from the sample (ESTIMATE):**

| Runtime kind | RAM planning value | Basis |
|---|---|---|
| Automated headless (AUTO_DEMO) terminal | **175 MB** typ / **225 MB** worst (**terminal only, bridge excluded**) | Typ = idle-light midpoint (~130 MB) + ~35% margin. Worst = idle-band upper (~165 MB) + margin. Headless EA-only terminal need not render charts. |
| Bridge (per automated runtime) | **33 MB** (MEASURED) | Added **once** per automated runtime. |
| Interactive charted terminal | **300 MB** + session overhead (§3.3) | Charted-active band; charts/indicators loaded. |

> **Per-automated-runtime formula:** `RAM_auto = RAM_terminal + RAM_bridge`
> Typical ≈ `175 + 33 = 208` → **~210 MB planning**. Worst ≈ `225 + 33 = 258` → **~260 MB**.
> (The 175/225 terminal values are bridge-*excluded*; the 33 MB bridge is added exactly once — no double-count.)

### 3.2 Per-terminal disk footprint and IOPS

Per-account portable MT5 directory (§7: `D:\GuvFX\users\<uid>\accounts\<account_id>\mt5\`) holds program files + per-account config + history/logs that **grow over time**.

| Component | Size (ESTIMATE) | Notes |
|---|---|---|
| Portable MT5 program + platform | ~150–300 MB | Per-instance copy (portable mode → **not shared**). |
| Per-account config / profiles / EA | ~5–20 MB | Small. |
| History (bars/ticks cache) | ~50 MB → several hundred MB, **grows** | Scales with symbols × timeframes × time-in-service. **Unbounded** without pruning. |
| Logs (Journal/Experts) | grows ~1–50 MB/day | Scales with tick activity + log verbosity. Rotation/pruning policy required (Phase-4 item). |

> **Per-account disk formula (ESTIMATE):** `disk_account ≈ 300 MB (program) + 20 MB (config) + history_growth + log_growth`. Plan a **steady-state envelope of ~0.5–1.5 GB per account** for beta demo accounts, **with a log/history pruning policy mandatory before density is raised.**

**Host-disk sanity check (ESTIMATE).** At the beta density (§3.6, ≤2 users/host) each user may have up to 10 *provisioned* accounts, so a host may carry up to ~20 portable dirs. Program copies alone ≈ 20 × 300 MB ≈ **6 GB**, plus history/log growth toward the ~0.5–1.5 GB/account envelope (up to ~30 GB at the 1.5 GB ceiling for 20 accounts). This fits the **80–120 GB SSD RDSH volume (§2)** only *with* the pruning policy in force; unbounded history/log growth across 20 dirs is the disk-exhaustion risk to prove against in Phase-4.

**IOPS.** MEASURED CPU is near-idle; MT5's steady disk pattern is small, bursty history/log writes on ticks + periodic flushes — **low sustained IOPS, spiky on tick bursts** (ESTIMATE, consistent with the measured tick-burst CPU profile). Two amplifiers: (a) **N terminals flushing concurrently** on a market-wide tick burst; (b) the **repair/re-materialisation storm** (§3.7) copying whole portable dirs.

> **SSD is a hard requirement.** The 80–120 GB RDSH volume (§2) **must be SSD.** Spinning disk would serialise concurrent tick-burst flushes and multiply portable-dir re-materialisation time during host-failure recovery. Design constraint, not a preference.

### 3.3 AUTOMATED vs INTERACTIVE — modelled separately

Two structurally different load classes (§8.3 step 5), budgeted independently. Per the ownership chain (§0), **the MT5 runtime is owned by the broker account.** Automated is always-on per active account and survives RemoteApp disconnect; interactive is ephemeral, on-demand, and **never drives runtime state** (opening/closing a session never provisions or tears down a runtime).

| Dimension | AUTOMATED (AUTO_DEMO, headless) | INTERACTIVE (RemoteApp) |
|---|---|---|
| Lifecycle | Always-on per **active** account; persists across disconnect | Ephemeral; only while user is viewing/trading |
| Count driver | # active accounts | # concurrent viewing sessions (typically 1/user) |
| RAM | terminal + bridge ≈ **~210 MB** (worst ~260 MB) | charted terminal ~300 MB **+ session/profile/RDP-encode overhead ~200–400 MB** (ESTIMATE) → **500–700 MB, plan 600 MB** |
| CPU | near-idle + tick bursts (MEASURED) | tick bursts **+ RDP frame encode** while user watches (ESTIMATE) |
| Bridge | 33 MB per runtime (order path) | none (view only, no separate bridge) |

> **Automated (per active account):** `RAM_auto ≈ 210 MB` (worst 260 MB).
> **Interactive (per concurrent session):** `RAM_inter ≈ 300 + (200…400) ≈ 500–700 MB` → **plan 600 MB**, plus RDP-encode CPU while active.
>
> An interactive session on an account that is *also* automated does **not** double the automated terminal — the headless terminal is the runtime; the RemoteApp is a separate charted view. Budget them as separate line items; the interactive figure is the *added* cost of a live viewing session.

### 3.4 Per-USER worst case

Per §3, a user may have **up to 10 broker accounts**, but not all active or interactive at once. Interactive concurrency is **typically 1 session** per user (a person watches one terminal). Planning uses 210 MB/automated and 600 MB/interactive.

| Profile | Active automated accts | Concurrent interactive | Automated RAM | Interactive RAM | **User total RAM (ESTIMATE)** |
|---|---|---|---|---|---|
| **Typical** (beta) | 1–3 | 0–1 | 210–630 MB | 0–600 MB | **~0.2–1.3 GB** |
| **Heavy** | 5 | 1 | ~1.05 GB | ~0.6 GB | **~1.65 GB** |
| **Max** | 10 | 1 | ~2.1 GB (10 × 210 MB) | ~0.6 GB | **~2.7 GB** |

> **Per-user formula:** `RAM_user = (active_automated × 210 MB) + (concurrent_interactive × 600 MB)`.
> Budget the beta **typical** user at **~1.3 GB** (3 automated + 1 interactive) and hold the **max (~2.7 GB)** as the sizing stress case. **All-10-automated + interactive simultaneously is an outlier**, not the planning default — but the host budget (§3.5) must not fall over if one user hits it.

### 3.5 Per-HOST budget model

**Base overhead (ESTIMATE — vendor-class typicals, NOT measured on our pool):**

| Component | RAM (ESTIMATE) | Notes |
|---|---|---|
| Windows Server 2022/2025 base | ~2–2.5 GB | Idle OS. |
| RDS Session Host role + services | ~0.5–1.5 GB | Session manager, RDP stack. |
| **RDSH base total** | **~3–4 GB** | Matches §3's "OS/RDS ≈ 4 GB". Use **4 GB** for planning. |

> **Host RAM formula:** `RAM_host = RAM_base(4 GB) + Σ RAM_user + reserve`, with **reserve = max(2 GB, 15% of host RAM)** to absorb concurrent tick bursts (§3.7) and repair storms (§3.7). Reserve = **2.4 GB** on a 16 GB host, **4.8 GB** on 32 GB.

**16 GB RDSH:**

| Scenario | Base | User load | Reserve | Total | Fits 16 GB? |
|---|---|---|---|---|---|
| 2 users, typical | 4 GB | 2 × 1.3 = 2.6 GB | 2.4 GB | **9.0 GB** | ✅ comfortable |
| 2 users, heavy | 4 GB | 2 × 1.65 = 3.3 GB | 2.4 GB | **9.7 GB** | ✅ |
| 2 users, both at max | 4 GB | 2 × 2.7 = 5.4 GB | 2.4 GB | **11.8 GB** | ✅ with storm headroom |
| 3 users, typical | 4 GB | 3 × 1.3 = 3.9 GB | 2.4 GB | **10.3 GB** | ⚠️ eats storm headroom |
| 4 users, heavy | 4 GB | 4 × 1.65 = 6.6 GB | 2.4 GB | **13.0 GB** | ⚠️ thin — no repair-storm margin |

**32 GB RDSH:**

| Scenario | Base | User load | Reserve | Total | Fits 32 GB? |
|---|---|---|---|---|---|
| 2 users, max | 4 GB | 5.4 GB | 4.8 GB | **14.2 GB** | ✅ large headroom |
| 4 users, heavy | 4 GB | 6.6 GB | 4.8 GB | **15.4 GB** | ✅ |
| 6 users, typical | 4 GB | 7.8 GB | 4.8 GB | **16.6 GB** | ✅ (unproven density — see §3.6) |
| 8 users, heavy | 4 GB | 13.2 GB | 4.8 GB | **22.0 GB** | ⚠️ approaching thin; prove first |

> Rows above 2 users/host are **capacity arithmetic only** — they are NOT authorisation to run at that density. Density is fixed at ≤2/host until Phase-4 proves otherwise (§3.6).

### 3.6 Density recommendation and the data-driven path to raise it

**Beta density: ≤ 2 users per RDSH** (matches §2/§3). Justification:

- A 16 GB host at 2 users sits at **~9–12 GB even heavy/max**, leaving **4–7 GB** to absorb (a) simultaneous tick bursts across all terminals and (b) a **repair storm** — the reconciler re-materialising a failed host's runtimes onto this host (§14, §3.7). That headroom is the point, not slack to reclaim.
- The single-sample caveat means the RAM bands may be **understated** under real multi-terminal load; conservative density is the margin against measurement error.
- CPU (§3.7): 2 users' terminals are near-idle, so tick bursts + one RDP encode fit in 4 vCPU with margin.

**Phase-4 measurement plan (prerequisite to any density increase).** Measure across the **beta pool at the approved ≤2 users/host density** (the ~5-user cohort spread over 2–3 hosts — **do NOT co-locate 5 users on one host**, which would violate the density under test), with **≥5 concurrent automated terminals plus representative interactive sessions**, under market-hours load, for a sustained window (≥1 full trading session):

1. **RAM distribution** per automated terminal and per interactive session — capture **p50/p95/max**, not a single point; confirm or correct the 210 MB / 600 MB planning values.
2. **RAM drift** over ≥8h (history/chart buffer growth) — is the idle-light band stable or climbing?
3. **CPU under concurrent tick burst** — peak vCPU across all terminals during a market-wide spike, plus concurrent RDP encode.
4. **Disk** — history/log growth rate per account/day and concurrent-flush IOPS peak; validate the §3.2 host-disk envelope and pruning policy.
5. **Repair-storm cost** — time + CPU + IOPS to re-materialise N portable dirs onto a healthy host after a simulated host failure (§3.7).
6. **Cross-tenant isolation holds under load** (§13) — the gate, **independent of resource numbers**; failure here blocks a density raise regardless of headroom.

**Telemetry prerequisite (important).** Host-level RAM/CPU utilisation and p95 are **not derivable from existing GuvFX telemetry** — the platform is log-based only (`core/observability.py`), with no metrics backend and no Windows host-resource sampling (heartbeats/`ComponentHealth`/`ExecutionJob` do not capture host RAM/CPU). The p50/p95/max figures above must be captured by **Windows perfmon counters / agent-side resource sampling collected during the Phase-4 load test** — a one-off measurement capture, **not** a new standing metrics platform (which would require an ADR per the no-speculative-infrastructure rule). Until that capture exists, the thresholds below are **load-test acceptance criteria, not measurable operational SLOs.**

> **Density-raise rule (governed, fail-closed, reversible):** Raise density **only** when the Phase-4 capture shows p95 host utilisation at the proposed density stays **below ~70% of total host RAM and ~60% CPU**, *with the §3.7 storm reserve remaining as unallocated headroom on top of measured utilisation* — AND isolation gate 6 passes. The raise must be a **documented decision (ADR/Notion record), never an in-passing config change.**
> - **Verification:** after raising, observe p95 utilisation over ≥1 further full session at the new density.
> - **Rollback:** if measured utilisation breaches the threshold or the storm reserve is consumed, **revert to the prior density immediately** and record the breach.
> - **Audit:** the raise decision, the evidence it rests on, and any rollback are recorded (decision record + `RuntimeEvent`-level evidence trail), consistent with the "detect → recommend → human/manual act → audit" staging used elsewhere in the platform.

**Oversubscription guidance.** **Do not oversubscribe RAM** on RDSH (no ballooning/swap reliance) — MT5 tick handling is latency-sensitive and paging a terminal during a tick burst risks missed/late execution (fail-closed: refuse the risk rather than assume it). **CPU may be modestly oversubscribed** (vCPU:pCPU up to ~2:1 is typical for near-idle MT5) **only after** §3.7 tick-burst + repair-storm peaks are measured — never inferred from the near-idle steady state alone.

### 3.7 CPU model

| Element | Cost | Source |
|---|---|---|
| MT5 terminal steady CPU | near-idle | MEASURED |
| MT5 tick-burst spike | short CPU spike per terminal on incoming ticks (magnitude unmeasured) | MEASURED (qualitative) |
| RDP session encode (interactive) | added CPU while user actively views (frame encode) | ESTIMATE |
| Repair/re-materialisation | copy portable dirs + relaunch terminals + broker re-auth | ESTIMATE |

Beta host = **4 vCPU** (§2). Because steady CPU is near-idle (MEASURED), the sizing constraint is the **three concurrent peaks**, which can coincide:

1. **Concurrent tick burst** — a market-wide move ticks *all* automated terminals on the host at once (N terminals × short spike). At ≤2 users this is a handful of terminals; comfortably within 4 vCPU (ESTIMATE).
2. **RDP encode** — 1–2 interactive sessions encoding frames during that same burst.
3. **Repair/re-materialisation storm** — after an RDSH failure (§14), the reconciler re-drives durable `AccountRuntime` state onto surviving hosts (idempotent materialisation → terminal relaunch → broker re-auth, i.e. `PROVISIONING`/`STARTING`/`AUTHENTICATING` transitions per §8) for a batch of accounts, *on top of* that host's existing load.

> **CPU headroom rule:** size so that `peak_tickburst + peak_RDP_encode + one_host's_repair_batch` fits within the host's vCPU with margin. At ≤2 users/16 GB/4 vCPU this holds by inspection; **the storm case is exactly why density stays at 2 until measured (§3.6 item 5).** Repair-batch size is bounded by pool topology — a 2-host pool means one host inherits the other's ≤2 users' runtimes; a 3rd host reduces per-host inherited load (the §2 rationale for a 3rd RDSH once density is proven).

### 3.8 Network

| Traffic | Estimate | Notes |
|---|---|---|
| RDP per interactive session (via RDGW/443) | ~0.1–1.5 Mbps typical, bursts ~5 Mbps on chart redraw (ESTIMATE) | Chart-heavy MT5 UI over TLS; H.264/RemoteFX-class encode. **Not measured on our pool.** |
| Bridge order/API traffic per automated runtime | small, bursty — order/modify calls + health probes; well under ~0.1 Mbps average (ESTIMATE) | Signal-driven, not streaming. Bridge process 33 MB (MEASURED). |
| MT5 ↔ broker market data | broker-side stream into each terminal | Modest per symbol; scales with symbols × terminals (ESTIMATE). |

> **Host egress formula (ESTIMATE):** `egress_host ≈ (concurrent_interactive × ~1 Mbps) + (active_automated × bridge_burst)`. At ≤2 users/beta this is a few Mbps — trivial for the VM NICs, but **RDGW/443 is the single external path (§18)** and must be sized/HA'd before scale (§3.9). Validate real RDP bitrate in Phase-4; the ~1 Mbps figure is an estimate. Thin-client/latency SLOs are out of scope here.

### 3.9 Scaling formulas

Let `active_users` = users with ≥1 active automated account; `density` = validated users/host (beta = **2**).

```
RDSH_count      = ceil(active_users / density)
infra           = 1 infra VM (DC+RDCB+RDGW+RDWeb+Licensing collapsed) — serves well beyond beta
RDSH_pool_RAM   = RDSH_count × host_RAM
CALs/SALs       = active_users        (RDS Per-User CAL/SAL, 1 per user — §16/§17)
```

Worked (beta, §2): `active_users = 5`, `density = 2` → `RDSH_count = ceil(5/2) = 3`. Design starts with **2 RDSH** and adds the **3rd once density is proven** (matches §2), giving failure-tolerance headroom in the interim. Total new Windows VMs = **infra (1) + RDSH (2–3) = 3–4**.

**Infra-VM headroom.** The collapsed infra host (4 vCPU / 8–16 GB, §2) is control-plane, not runtime — its load is per-login/session-broker/licensing, not per-terminal, so it scales far past 5 users on one VM. It becomes the ceiling only via **availability** (it is a SPOF), not capacity.

**When to split roles / add HA (post-beta, §17):**

| Trigger | Action |
|---|---|
| Infra host SPOF blocks availability | Split DC / RDCB / RDGW / Licensing onto separate VMs; add a **2nd DC**, **redundant RDCB** (HA broker), **2+ RDGW** behind a load balancer. |
| `active_users` grows past what one RDGW/443 path can carry | Add RDGW instances + LB (ties to §3.8 external-path bound). |
| Density proven higher in Phase-4 (§3.6) | Recompute `RDSH_count` with the new `density`; RDCB auto-load-balances the pool. |
| App-layer fan-out | Already O(N accounts) and host-agnostic (§17) — **not** a scaling ceiling; the ceiling is host capacity + RDS infra HA. |

### 3.10 Assumptions & Limitations

**MEASURED (trust as far as one sample allows):**
- One MT5 `terminal64.exe`: idle-light ~100–165 MB, charted-active ~200–350 MB, ~16 threads, near-idle steady CPU with tick-burst spikes; bridge ~33 MB. **One process, one broker, one moment.** Spike magnitudes are qualitative (observed, not quantified).

**ESTIMATE (planning envelopes, not measurements):**
- All per-host base-OS/RDS figures (~3–4 GB), RDP session/profile/encode overhead (~200–400 MB), RDP bitrate (~0.1–5 Mbps), disk/history/log growth and IOPS, repair-storm cost, and every per-user/per-host/density total derived above.
- Windows base-OS/RDS numbers are vendor-class typicals, **not measured on our pool**.

**Explicit limitations / NOT covered (must be proven in Phase-4, §19 item 8):**
1. **Single-terminal sample** → no variance, no p95, no multi-terminal interference, no long-run drift. All bands may be understated under concurrent load.
2. **Charts inflate RAM.** The charted-active band (~2× idle-light) means interactive concurrency inflates host RAM directly and non-linearly; the 600 MB interactive figure is an estimate.
3. **Beta = demo accounts only.** Load, symbol counts, and tick rates on live accounts may differ; these numbers are for demo AUTO_DEMO beta usage.
4. **No measured repair-storm.** The §3.7 storm reserve is sizing prudence, not a measured recovery cost.
5. **No host-resource telemetry exists today.** Host RAM/CPU utilisation and p95 are not derivable from existing GuvFX telemetry (log-only, no metrics backend); the §3.6 thresholds are **load-test acceptance criteria requiring purpose-built Phase-4 perfmon/agent sampling**, not measurable operational SLOs.
6. **Density (2/host) is deliberately conservative and unproven** — a Phase-4 proof (§3.6), not an operational SLO. No SLOs exist yet (per programme docs); nothing here asserts one.
7. **RDP bitrate, external-path capacity, and disk pruning policy** are unspecified and must be measured/decided before density is raised.
8. **Governance.** All of the above is a **plan awaiting Nuno's approval** (§20). No procurement, no host changes; Nuno's production box, runtimes and AUTO_DEMO operation are untouched and excluded; onboarding stays **closed** (`BETA_ONBOARDING_ENABLED` off); `can_deploy_automation` stays **False** for the `beta` plan until Phase-4 gates pass. Nothing here places, sizes, or closes a trade.

---

## B. Operational runbooks

> Each runbook is **design documentation** for the Phase-2+ control-plane (provisioning worker +
> Windows agent + `ProvisioningJob`), which is **unbuilt**: in production today every `AccountRuntime`
> is `NOT_PROVISIONED`. Diagnostic steps that read the shipped Phase-0 durable records
> (`AccountRuntime`/`RuntimeEvent`) are real; actioning steps are target-state. All are fail-closed,
> single-tenant-scoped, and never place or close a trade.

### Runbook — Beta user onboarding

> **Status: DESIGN / DOCUMENTATION ONLY.** This runbook describes the *target* end-to-end onboarding of a beta user (register → entitled → provisioned → `RUNNING`) for the Option A RDS pool. The async provisioning control-plane it depends on (`ProvisioningJob`, provisioning worker, Windows WinRM agent endpoints) is **Phase 2+ and NOT yet shipped**, and the RDS pool it targets is **NOT procured**. In production today every `AccountRuntime` is `NOT_PROVISIONED`, the onboarding gate is **closed**, and no beta user has ever been provisioned. **There is no working provisioning path in production today — automated *or* manual — because neither the pool (P1) nor the control-plane/agent (P2) exists.** Nothing here authorises procurement, infrastructure changes, or opening the gate. All Phase 2+ work requires Nuno's approval of the §19/§20 sequence and BoM.
>
> **Fact discipline (evidence rule).** The following are **shipped and MEASURED-in-code**: the Phase‑0 data model (`AccountRuntime` / `RuntimeEvent` / `RuntimeState`, entitlements, Account Status panel) **and the full self-service onboarding step chain** `plan_selected → email_verified → two_factor_enabled → risk_accepted → account_connected → strategy_assigned` (`onboarding/services.py`, `STEP_ORDER` / `REQUIRED_STEPS`) — i.e. **Steps 1–8 below, including broker-account connect (which auto-creates the durable `AccountRuntime` record at `NOT_PROVISIONED`) and strategy-assign.** The following are **designed, NOT deployed** — treat every operational claim about them as target behaviour, not observed behaviour: **Steps 9–13** (enqueue `ProvisioningJob` → provisioning worker → state-machine drive → AD identity → cred inject → Guacamole). All latency/SLO numbers in §10 are **ESTIMATES / targets to be proven in Phase 4**; no measured provisioning-latency baseline exists. Do not present any Step‑9+ behaviour or any §10 number as a measurement.

---

#### 1. Purpose / trigger

Bring one new beta user from account registration to a healthy, per-account automated MT5 runtime in state `RUNNING`, under the canonical ownership chain:

```
User → Broker Account → MT5 Runtime (1:1, owned by the account) → Strategies → Positions → Notifications
User → Windows identity guvfx_u_<uid>
```

**Trigger (target, Phase 4 self-service):** a broker-account validation succeeds **AND** the user is beta-entitled **AND** the onboarding gate is open — at which point exactly one durable `ProvisioningJob(op=PROVISION)` is enqueued per broker account and the async worker drives the state machine.

**No trigger fires today.** The Phase‑4 self-service trigger cannot fire until the gate is opened by Nuno (Red). The alternative admin-driven controlled-pilot path (§5) is **not available in production today** either: it depends on P1 (pool procured — Red) and P2 (control-plane / WinRM agent — not shipped). Do not treat controlled-pilot as a "current" capability.

---

#### 2. Scope & preconditions

**In scope:** one user, one or more broker accounts, each getting its own per-account MT5 runtime on the RDSH pool, reachable via a per-user Guacamole/RemoteApp entitlement.

**Preconditions:**

| # | Precondition | Where verified | Current status |
|---|---|---|---|
| P1 | Windows RDS pool exists (1 infra host + ≥2 RDSH) | Option A §1–2 | **NOT procured** (Red — awaits Nuno) |
| P2 | Provisioning control-plane deployed (`ProvisioningJob`, worker, WinRM agent) | §19 items 1–3 | **NOT shipped** |
| P3 | `BETA_ONBOARDING_ENABLED=1` | `billing/beta.py:beta_onboarding_open()` | **closed (default off)** |
| P4 | User has `beta` entitlement | `billing/entitlements.py` | shipped (auto-granted at Step 2) |
| P5 | Isolation gates passed (red-team, 5-user load, no cross-tenant leak) | §19 item 8 (Phase 4) | **NOT run** |
| P6 | GUVFX/provisioning DB backed up (recovery + rebuild depend on it — §7/§14) | estate backup gap | **RED gap** |

A beta user **cannot be provisioned for automation while any of P1, P2, P3, P5 are unmet.** P4 is auto-granted (Step 2). P6 is a hard recovery/offboard-safety dependency (the §7 "runtime rebuildable from config + Fernet creds" claim assumes an intact, backed-up DB) — it must be closed before any real provision or the recovery guarantees do not hold. `can_deploy_automation` stays `False` regardless until Phase 4 (Step 12 note).

---

#### 3. Roles & authorisation (Green / Amber / Red)

| Action | Band | Who |
|---|---|---|
| Register, verify email, enable 2FA, accept risk (self-service) | **Green** | User |
| Add + validate a broker account (non-secret metadata) | **Green** | User |
| Grant/refresh `beta` entitlement (idempotent, never clobbers paid plan) | **Green** | System (`grant_beta_entitlement`) |
| Enqueue `PROVISION` job; drive state machine to `RUNNING` | **Green** — *but only once P1–P3 & P5 met (not today)* | Provisioning worker |
| Controlled-pilot provision of a *named* user while gate closed (staff bypass) | **Amber** — documented decision + handoff flag; **requires P1+P2 to exist first** | Ops admin, on Nuno's instruction |
| Open the onboarding gate (`BETA_ONBOARDING_ENABLED=1`) | **Red** | Nuno only, after Phase-4 gates |
| Flip `can_deploy_automation=True` for `beta` plan | **Red** | Nuno only, Phase 4 |
| Any RDS/RDSH procurement or infra build (P1) | **Red** | Nuno only |
| Bypass email verification | **Prohibited** | — nobody |

---

#### 4. Isolation & safety invariants

These must hold at **every** step; a violation aborts the procedure.

1. **Fail-closed instance resolution.** `_get_user_mt5_instance` (Phase-0, shipped) resolves *only* the user's own leased per-account runtime, or returns `None` with a sanitised message. It never falls back to a shared or another user's box.
2. **Never bind to Nuno's production instance.** Nuno's existing Windows host, MT5 runtimes, broker accounts, strategies, routing, lot sizes and AUTO_DEMO operation are **outside the pool** and untouched. No beta runtime, identity, Guacamole connection, or routing target may resolve to that box.
3. **Onboarding gate is authoritative.** `mark_account_connected()` and `mark_strategy_assigned()` both hard-block a non-staff user when `beta_onboarding_open()` is false. Do not weaken this. No email-verify bypass, no signal replay, no forced trade, provider commands disabled.
4. **Execution stays independently gated.** `can_deploy_automation=False` on the `beta` plan is a server-side block on order placement that is **independent** of whether a terminal is provisioned — a `RUNNING` runtime still cannot place live orders until Phase 4.
5. **Per-user / per-account isolation.** Non-admin `guvfx_u_<uid>` identity (`is_admin` must stay `False`); per-account NTFS-ACL'd portable MT5 dir; one Guacamole connection per (user, account-runtime); no shared connection object; RDSH reachable only via RD Gateway TLS.
6. **Sanitised errors only.** Raw agent/bridge strings never reach `AccountRuntime.last_error`, `RuntimeEvent.detail`, or any user/admin API — only mapped, user-safe reasons (e.g. *invalid broker credentials*, *wrong server*, *host at capacity*, *broker unreachable*).
7. **Credential boundary.** Broker password stays Fernet-encrypted at rest; decrypted only transiently at injection time inside the ACL'd runtime dir; never in a shared handoff path, never in logs. (Legacy shared-handoff exposure C16 is why the RDS portable-dir model exists and why non-staff MT5 desktop launch stays disabled on the current shared box.)

---

#### 5. Step-by-step procedure

**Steps 1–8** are shipped self-service onboarding (`onboarding/services.py`, `STEP_ORDER`) — MEASURED-in-code. **Steps 9–13** are the designed, undeployed provisioning flow (Option A §7–10, §8 state machine) — target behaviour only.

**The gate branch (read before Step 3):** the onboarding gate is **closed in Phase 0/1** and only opens at **Phase 4**. Two paths exist:

- **Phase-4 self-service path (target):** `BETA_ONBOARDING_ENABLED=1`; the user drives Steps 1–8 themselves; provisioning (Steps 9–13) runs automatically off the validated broker account. **Not reachable today.**
- **Admin-driven controlled-pilot path (Amber, not available today):** the gate stays closed and a non-staff user is structurally blocked at Steps 8/9 (`account_connected` / `strategy_assigned`). To pilot *one named user* before Phase 4, an ops admin (Amber, on Nuno's explicit instruction, **and only after P1+P2 exist**) acts on the user's behalf or under a staff account (staff bypass the gate) and — because Steps 9+ are undeployed — completes provisioning by the **manual** control-plane (`Provision-GuvfxAccount.ps1` run by hand over WinRM), recording the same durable evidence via `get_or_create_runtime` + `record_transition`. This path does **not** enqueue a `ProvisioningJob` (that model is unshipped). No gate flip, no bulk onboarding.

Numbered procedure:

1. **Register.** User creates an account (email-based login, `users` app). A `UserSubscriptionState` exists or is created.
2. **Auto beta entitlement.** `billing/beta.py:grant_beta_entitlement(user)` idempotently assigns the `beta` plan (`is_beta=True`, `max_trading_accounts=10`, `max_active_strategies=50`, **`can_deploy_automation=False`**). It never clobbers an existing paid plan. `resolve_entitlements()` returns beta capabilities.
3. **Gate check.** `beta_onboarding_open()` is evaluated at the two irreversible progression points (Steps 8 & 9). If closed and user is non-staff → `OnboardingStepError("Beta onboarding is not open yet.")`. Follow the **gate branch** above.
4. **Email verify.** `email_verified` step. **No bypass** — this is the current hard block that keeps onboarding safely closed. (Known issue: the verify email does not send today; do **not** work around it. Fix, don't bypass.)
5. **2FA (optional).** `two_factor_enabled` — `setup_2fa()` stores the TOTP secret Fernet-encrypted (`TwoFactorSecret.secret_enc`). Optional; never blocks completion.
6. **Risk accept.** `risk_accepted` step recorded.
7. **Add broker account + validate.** `mark_account_connected()` (gated by Step 3). On success it **auto-creates the durable runtime record without swallowing failures**: `get_or_create_runtime(account)` → `AccountRuntime` at `NOT_PROVISIONED`; then attempts `provision_terminal_for_account(user, account)`. Terminal provisioning here is a *convenience, not a gate* — on exception it writes `record_transition(runtime, FAILED, event_type="FAILURE", reason_code="provision_terminal_error", detail=<sanitised>)` and logs; onboarding still proceeds. Broker credentials are Fernet-encrypted at rest at this point.
8. **Assign AUTO_DEMO strategy.** `mark_strategy_assigned()` (also gated by Step 3) records the account's AUTO_DEMO `StrategyAssignment`. This is a **required** shipped step (`STEP_ORDER`/`REQUIRED_STEPS`) and the second gate enforcement point; a green *overall* status (§6) depends on an assigned + active assignment here.
9. **Enqueue `ProvisioningJob(op=PROVISION)`** *(designed, not shipped).* On validated broker account + entitled user + gate open, one durable job is enqueued (same pattern as `ExecutionJob`). A provisioning worker claims it (lease + single-flight) and drives the state machine **one durable step per iteration** over WinRM/PowerShell.
10. **State machine to `RUNNING`** *(designed, not shipped).* Persist-then-act on `AccountRuntime` (a `RuntimeEvent` is written **before** each side-effect, reconciled after; no exception swallowed):
    ```
    Forward:  NOT_PROVISIONED → QUEUED → PROVISIONING → STARTING → AUTHENTICATING → RUNNING
    Capacity: QUEUED → BLOCKED (no host capacity / entitlement / gate closed) → QUEUED (cleared)
    Provision err: PROVISIONING → retry×N(backoff) → exhausted → FAILED
    Start err:     STARTING → DEGRADED
    Auth:          AUTHENTICATING → FAILED (bad creds/wrong server)
                   AUTHENTICATING → retry (transient) → exhausted → DEGRADED
    Health loop:   RUNNING → DEGRADED → REPAIRING → RUNNING ;  REPAIRING → exhausted → FAILED
    ```
    Retries are bounded exponential backoff (`attempt`, `last_error`, `next_retry_at`). **Open item:** the retry count N and backoff schedule are **not numerically specified** in the design or shipped code — must be defined in Phase 2 and should reuse the existing threshold vocabulary (age vs `expected_interval × grace-multiplier`) rather than a new style.
11. **Materialise identity + runtime + creds** *(inside `PROVISIONING`, idempotent — create-if-absent / overwrite / ensure-task, not shipped):*
    a. AD user **`guvfx_u_<uid>`** (per-user, non-admin, member of `GuvFX-BetaUsers`, RemoteApp-only, no full desktop). Strong random password, Fernet-stored.
    b. Per-account **portable MT5 directory** on an RDSH (e.g. `D:\GuvFX\users\<uid>\accounts\<account_id>\mt5\`), a distinct `terminal64.exe`, **NTFS-ACL'd to `guvfx_u_<uid>` + SYSTEM/admin only**.
    c. **Fernet cred inject** — broker password decrypted only at injection time, transported over the authenticated WinRM/TLS channel, written **only** into the ACL'd runtime dir. No shared handoff dir.
    d. **Logon/scheduled task** launches the automated terminal (AUTO_DEMO) under `guvfx_u_<uid>`, kept alive across RemoteApp disconnects.
12. **Guacamole per-user RemoteApp entitlement** *(designed, not shipped).* Create **one** Guacamole connection for (user, account-runtime): user's Windows identity + MT5 RemoteApp + RD Gateway, granted only to that GuvFX user. GuvFX is source of truth. Enforces the 1:1 chain: GuvFX user ↔ broker account ↔ `guvfx_u_<uid>` ↔ MT5 runtime ↔ RemoteApp ↔ Guacamole connection. **Execution note:** provisioning to `RUNNING` does **not** authorise automation — `can_deploy_automation` remains `False` (Red / Phase 4) so no orders are placed.
13. **User sees Account Status `RUNNING`.** `account_status.py:build_account_status()` reads `AccountRuntime.state` + latest `RuntimeEvent`; the `hosted_terminal` stage reads "RUNNING" **only** when `runtime.state == RuntimeState.RUNNING` exactly. Truthful throughout — undeployed stages show `NOT_CONFIGURED`/`BLOCKED`, never false success.

---

#### 6. Verification (exact states / health that confirm success)

Provisioning is **successful** when **all** hold:

| Check | Exact condition | Source |
|---|---|---|
| Runtime state | `AccountRuntime.state == RUNNING` | `terminal_provisioning.models` |
| Latest event | most recent `RuntimeEvent.to_state == RUNNING`, `event_type == TRANSITION`, no trailing `FAILURE` | `RuntimeEvent` (append-only) |
| Panel: hosted terminal | `hosted_terminal` stage state == `"RUNNING"` (exact-match, not derived) | `account_status.py` |
| Panel: runtime | `mt5_runtime` stage HEALTHY via `user_facing_state(runtime)` | `runtime_state.py` |
| Overall | `_overall(stages)` HEALTHY — requires **both** `hosted_terminal=="RUNNING"` **and** `strategy_enabled==HEALTHY` | `account_status.py` |
| Isolation | `_get_user_mt5_instance` resolves to *this* account's runtime; Guacamole grant is single (user, runtime); NTFS ACL excludes other users | §10, §13 |
| No cross-tenant leak | Nuno's prod box unreferenced; no shared connection object | invariant 2/5 |
| Entitlement (expected) | `can_deploy_automation == False` (correct pre-Phase-4) | `entitlements.py` |

Success = runtime `RUNNING` **and** isolation intact. A green *overall* additionally needs an active AUTO_DEMO strategy assignment (Step 8; `strategy_enabled` HEALTHY). The return dict always carries `terminal_provisioning_available: False` today, so the UI never infers a terminal from a green overall while provisioning is undeployed. **Today this verification cannot yield success for any account — every runtime is `NOT_PROVISIONED` and every provisioning-dependent stage reports `NOT_CONFIGURED`.**

---

#### 7. Rollback / abort (idempotent DEPROVISION)

A partial or failed provision is backed out via the **same durable state machine** — never by ad-hoc file/identity deletion.

1. Enqueue `ProvisioningJob(op=DEPROVISION)` (or, controlled-pilot path, run the manual teardown and record it via `record_transition`).
2. State: `any → DEPROVISIONING → REMOVED`. Each step idempotent (safe to re-run) and driven from the durable state, so a crash mid-teardown resumes cleanly.
3. Teardown order (reverse of build): revoke the Guacamole connection + grant → stop/remove the logon task → delete the per-account portable MT5 dir → (on full offboard only) disable then delete the AD user `guvfx_u_<uid>` and revoke `GuvFX-BetaUsers` membership.
4. Because create steps are create-if-absent and teardown is delete-if-present, a **re-run of `PROVISION` after an aborted attempt converges** to the target with no duplicate identity/dir/connection.
5. **Suspend (not remove):** for a temporary hold, use `STOP` (`RUNNING → STOPPING → STOPPED`) or disable the AD account + collection membership (fail-closed: disabled ⇒ no launch); resume via `START` (`STOPPED → STARTING → …`). No user data loss — positions live broker-side; the runtime is rebuildable from config + Fernet creds **provided P6 (DB backup) is satisfied** (rebuild reads the durable `AccountRuntime`/creds from the GUVFX DB — an unbacked DB voids this guarantee).
6. Every rollback transition writes a `RuntimeEvent` (`from_state`/`to_state`/`reason_code`), so the abort is itself auditable.

---

#### 8. Escalation

| Situation | Escalate to | Note |
|---|---|---|
| `FAILED` after retries exhausted | Ops admin → user "Retry" (`FAILED → QUEUED`) | sanitised reason surfaced; raw text admin-only |
| `BLOCKED` = host at capacity | Ops admin | add/confirm RDSH slot — density is a Phase-4 proof; raise only with evidence, not by assumption |
| `AUTHENTICATING → FAILED` (bad creds/wrong server) | User re-enters broker credentials | never expose raw agent string |
| Suspected credential exposure (e.g. plaintext in a shared path/log) | **Stop, do not commit, report with redacted detail** (security rule) | rotate `GUVFX_FERNET_KEY` and re-encrypt affected broker cred |
| Any request to open the gate or flip `can_deploy_automation` | **Nuno (Red)** | Phase-4 gate only |
| Cross-tenant leakage or a beta artefact resolving to Nuno's prod box | **Nuno + halt provisioning immediately** | invariant 2 breach — fail closed |
| Alert delivery | `reliability` alerting: Telegram bot → generic webhook → persist-only `SKIPPED` | delivery never raises; no external pager platform (no PagerDuty/Opsgenie) |

Remediation follows the existing **detect → recommend → human/manual act → audit** staging (`RecoveryRecommendation` is advisory-only; nothing auto-executes in Phase 1). Do not add automatic remediation.

---

#### 9. Audit / evidence

- **`RuntimeEvent`** — the immutable, append-only, chronological (`ordering=["id"]`) evidence trail per runtime. Every transition/retry/failure writes one **before** the side-effect (`event_type` ∈ `TRANSITION`/`RETRY`/`FAILURE`; `from_state`, `to_state`, `reason_code`≤64, `detail`≤2000 sanitised). Immutability enforced at **both** the app layer (`save()`/`delete()` raise `ValueError`) **and** the DB layer (BEFORE-UPDATE trigger, migration `terminal_provisioning/0005`). This is the source for a provisioning-latency SLI (time between consecutive events) and a failure-rate SLI (count of `FAILURE` per window).
- **Onboarding audit** — `core.audit.log_onboarding_completed` on completion; step progression is logged and idempotent.
- **Admin visibility** — `AdminBetaEstateView` (`/api/admin/beta-estate/`, `IsSuperOrOpsAdmin`, non-staff users capped `[:200]`) surfaces per-user `is_beta`, `account_count`, `max_accounts` (`min(10, ent.max_trading_accounts)`), and per-account `account_number` (**non-secret identifier only, never password**), `runtime_state`, sanitised `runtime_last_error`, plus last 5 `FAILURE` events. This is the canonical read-only, sanitised, staff-gated ops surface — **never** decrypted credentials.
- **Evidence discipline (Phase 4 acceptance):** record exact commands + actual results; mark `PASS` only when the criterion actually ran; a two-stage broker/isolation proof for a real provisioned beta account is currently **EVIDENCE-PENDING** (no beta user provisioned). State this as PENDING, not PASS.

---

#### 10. SLO linkage (provisioning latency)

**No SLO/SLI model exists in the codebase yet** — these are proposed targets, and **all latency numbers here are ESTIMATES to be proven in Phase 4**, not measurements. There is no measured provisioning-latency baseline. Any SLO must reuse the existing threshold vocabulary (`age > expected_interval × grace-multiplier`) rather than invent a new one, and must be derived from `RuntimeEvent` timestamps / `ExecutionJob` timing primitives — no new metrics backend (adding Prometheus/StatsD would need an ADR under the no-speculative-infrastructure rule). SLO endpoints must follow the established fail-safe read discipline (`operations_summary.py` never 500s; degraded shape on error).

| SLI | Definition (measurable from existing telemetry) | Proposed objective (ESTIMATE — prove in Phase 4) |
|---|---|---|
| Provisioning latency | `RuntimeEvent(to_state=RUNNING).created_at − RuntimeEvent(to_state=QUEUED).created_at` | p95 ≤ **N** min — **N unset; measure first** |
| Queue wait | time in `QUEUED`/`BLOCKED` before `PROVISIONING` (from `RuntimeEvent` timestamps) | bounded by RDSH slot availability |
| Auth latency | `AUTHENTICATING → RUNNING` span (from `RuntimeEvent` timestamps) | dominated by broker login |
| Provisioning success rate | `RUNNING` reached without a terminal `FAILED` per window (count `FAILURE` events) | target set after baseline |
| Stuck-job detection | reuse `ExecutionJob.lease_expires_at` + `EXECUTION_LEASE_TTL_SECONDS=300` (RX-2E) for `ProvisioningJob` | no new orphan mechanism |

**Explicitly NOT covered / open items to close in Phase 2–4:**
- All five SLIs above are **derivable in principle** from `RuntimeEvent`/`ExecutionJob`, but yield **zero data today** — no runtime has ever left `NOT_PROVISIONED`, so no baseline can be computed until after first real provision.
- Retry count N and backoff schedule are undefined in design and code — must be specified (Phase 2).
- Two heartbeat-staleness formulas coexist (`heartbeat.py` grace-multiplier 2.5 binary vs `operations_summary.py` two-tier `_STALE_FACTOR=2.0` / `_CRITICAL_FACTOR=4.0`) — a new provisioning SLO must **flag and reconcile** this with the packet owner, not silently pick one.
- Density (`≤~2 users/RDSH`, the 16 GB RAM budget) is an **unproven assumption**, not an SLO input — "density is a Phase-4 proof, raise only with evidence."
- No `/operations` frontend page and no `SLO` config model exist today — both would be new work requiring their own gated increment.

### Runbook — Password & credential changes

> **Status of this runbook.** This section documents the *designed* procedures for GuvFX Beta Onboarding V1 (Option A). **Only sub-procedure (a) is operational today.** Sub-procedures **(b)** and **(c)** describe the Phase 2+ provisioning-worker and RemoteApp/MT5 runtime path, which is **DESIGN FOR APPROVAL** — not deployed, not authorised for procurement or implementation. In production today every `AccountRuntime` is `NOT_PROVISIONED`; the Phase-0 state machine (`RuntimeState`/`AccountRuntime`/`RuntimeEvent`) *records* state but does **not** drive provisioning. Nothing here changes Nuno's existing Windows host, MT5 runtimes, broker accounts, Guacamole access, or AUTO_DEMO operation. Steps marked **[DESIGNED — Phase 2+]** are not live procedure and must not be executed until the §19 sequence / §20 BoM are approved by Nuno, the Phase-4 isolation + load gates pass, and `BETA_ONBOARDING_ENABLED` is opened.

#### Security invariant (applies to all three types — non-negotiable)

- **No agent ever handles a raw secret.** Claude and any automated agent never generate, read, transport, log, or display a plaintext password. Password changes are performed by the **human** (app/broker web UI) or by the **provisioner** (Windows control-plane identity). Plaintext exists *only transiently inside the isolated provisioner/runtime boundary* and is never returned to, logged by, or displayed to an agent.
- **Secrets never appear in logs, Notion, Git, prompts, or error surfaces.** Broker and Windows-identity secrets are Fernet-encrypted at rest (`GUVFX_FERNET_KEY`; `trading/crypto.py`). User- and admin-facing surfaces (`AccountRuntime.last_error`, `RuntimeEvent.reason_code`/`detail`, `AdminBetaEstateView`) carry **sanitised strings and non-secret identifiers only** (e.g. `account_number`) — never a decrypted password, never raw agent/bridge exception text. Sanitisation is at the boundary: catch → store a mapped, user-safe reason (*invalid broker credentials*, *wrong server*, *directory unreachable*, *host at capacity*) → raw text admin-only.
- **Encryption boundary is fail-closed:** decrypt only at injection time; write only into the per-account runtime dir NTFS-ACL'd to `guvfx_u_<uid>` (+ SYSTEM/admin); **no shared handoff directory** (closes issue C16). If a secret may have leaked: **stop, do not commit, report with redacted detail so it can be rotated** (security rule).

#### The three credential types (do not conflate)

| # | Credential | Layer | Changed by | Encrypted store | Terminal impact of a change |
|---|---|---|---|---|---|
| (a) | GuvFX **app account password** | Application (Django `users`, JWT/cookie auth) | The customer (self-service) | Django one-way hash (PBKDF2) — *not* Fernet; GuvFX cannot read it back | **None.** Unrelated to Windows/MT5. |
| (b) | **Windows identity** `guvfx_u_<uid>` password | OS / AD / RDS RemoteApp | The **provisioner** (control-plane) — human *requests*, never types | Fernet (`password_enc`) | *Designed expectation (ESTIMATE, unproven):* running automated terminals keep their live logon session; next interactive launch + the per-user task credential must pick up the new password. See caveats in (b). |
| (c) | **Broker account** password (MT5 login) | Broker | The customer (**at the broker**) → provisioner re-injects | Fernet (broker cred) | Runtime re-authenticates: converges to `RUNNING` via the durable state machine on re-login. |

These are **distinct authority chains** — changing one never rotates another. (a) is per-*user* app auth; (b) is one per-*user* Windows identity; (c) is per-*broker-account*. The ownership chain `User → Broker Account → MT5 Runtime (1:1) → Strategies → Positions → Notifications`, plus `User → guvfx_u_<uid>`, is preserved throughout. Nuno's production estate is out of scope for all three.

---

### (a) GuvFX app account password — self-service, app-level *(MEASURED: live today)*

- **Trigger:** customer wants to change their GuvFX login password, or a forgotten-password reset.
- **Authorised:** the customer themselves, authenticated. Admins do **not** set customer passwords; a staff reset only issues a reset link.
- **Steps:**
  1. Customer signs in and uses the in-app change-password flow (current + new password), or the forgotten-password reset link.
  2. Django validates and stores the new PBKDF2 hash. No plaintext retained; GuvFX cannot read it back.
  3. Existing JWT/cookie sessions continue until normal expiry/refresh unless a global session invalidation is explicitly requested.
- **Encryption boundary:** one-way hash only — no Fernet, no recoverable secret.
- **Verification:** customer signs out and back in; `/api/auth/cookie/` login succeeds with the new password.
- **Failure handling:** validation errors (too short / reused / mismatch) surfaced by the form; no runtime side-effects. A failed reset link is re-issued, never emailed in plaintext.
- **Escalation:** none — customer self-service; repeated reset failures follow standard support, no ops action.
- **Rollback:** none required — re-run the change flow with a new password.
- **Audit:** standard auth/audit log entry (event type only — **never** the password value).
- **Terminal/broker impact:** **none.** Does not touch `guvfx_u_<uid>`, the MT5 runtime, or the broker login. **Do not initiate any provisioning action for an (a) change.**

---

### (b) Windows identity `guvfx_u_<uid>` password — provisioner-managed rotation **[DESIGNED — Phase 2+]**

- **Trigger:** scheduled/periodic rotation of the per-user AD identity password, an operator-requested rotation, or suspected exposure of the Windows credential.
- **Authorised:** the **provisioning control-plane only** (AD cmdlets over WinRM). A human operator *requests* the rotation via an admin op and **never sees or types** the password; the strong random password is generated by the provisioner.
- **Ordering discipline (read before the steps):** the durable state machine follows **persist-then-act** for *state* — the `RuntimeEvent`/state row is written **before** each side-effect and reconciled after (§8.3 step 3, `record_transition`). The *credential store* is deliberately handled differently — **commit-on-success**: the Fernet `password_enc` is overwritten **only after a confirmed AD success**, so a failed AD rotation never advances the store. (Do not describe the store write as "persist-then-act" — it is intentionally act-then-commit to avoid storing a password AD rejected.)
- **Steps (idempotent, driven by a `ProvisioningJob` op — same pattern as `ExecutionJob`; `ProvisioningJob` is not yet shipped):**
  1. Provisioner generates a new strong random password (transient plaintext, in-process only, inside the isolated provisioner).
  2. Reset the AD account password via the WinRM AD cmdlet (`Set-ADAccountPassword -Reset`, which needs no prior password) against the infra-host DC.
  3. **On confirmed AD success only:** overwrite the Fernet `password_enc` for that identity; discard the plaintext immediately after encryption.
  4. **Update the per-user automated-terminal launch credential.** The AUTO_DEMO terminal runs as a per-user Scheduled/logon task (§7). **If that task is registered with stored credentials ("run whether user is logged on or not"), its stored password embeds the rotated secret and the task will fail to start after rotation unless updated in the same op.** Either (i) update the task's stored credential as part of this op, or (ii) use a launch model that does not embed the rotated password (logon-triggered task under the live session / gMSA). **OPEN — must be resolved in design before (b) is armed.**
  5. Rebuild the per-user Guacamole/RemoteApp connection object bound to that identity so the next interactive launch authenticates with the new credential.
  6. Record the rotation as a durable `RuntimeEvent` (event type only — no secret) and, if a per-account op is used, a `ProvisioningJob` result.
- **Downtime classification (ESTIMATE, not measured — do not assert as an SLO):**
  - *Currently-running automated terminals:* the AUTO_DEMO MT5 process logs into the **broker** account, not the Windows identity, and runs inside an already-established logon session/token; a Windows password reset does not tear down a live session, so the running process is expected to be unaffected. **However**, its *next* (re)start depends on step 4 being correct — if the task credential is stale, the terminal will not relaunch after any reboot/host-failover.
  - *Interactive RemoteApp:* at most a single re-authentication on the next launch; the Connection Broker reconnects existing interactive sessions. Interactive sessions are ephemeral views and never drive runtime state (§0/§8.3), so an interactive re-auth prompt is a UI event, not a `RuntimeState` transition.
  - This whole classification is a **design expectation grounded in the logon-session and task-credential models — not measured.** It must be demonstrated under the Phase-4 load/isolation gate (including a host-failover restart) before being stated as an SLO. The Windows/broker credential-change paths are **unbuilt**, so there is no telemetry today to assert a downtime number.
- **Crash-window reconciliation (fail-closed gap to close):** if the op is interrupted **between** a successful AD reset and the Fernet commit, AD (new) and the store (old) diverge. The live session is unaffected, but the next (re)start would fail auth. The reconciler (extends `execution_health`) must detect store-vs-AD divergence and **re-drive an idempotent `Set-ADAccountPassword -Reset` with a fresh password, re-committing the store** — never attempt to "restore" an old plaintext (none is retained).
- **Encryption boundary:** plaintext exists only transiently inside the provisioner; written into AD, the Fernet store, and (step 4) the task/connection credential vaults — never into a shared path, log line, Notion, or Git.
- **Verification:** provisioner confirms `Set-ADAccountPassword` returned success; a new interactive RemoteApp launch authenticates; the task credential update is confirmed (a task-run test, not just registration); and, **once heartbeat wiring is deployed** (today `last_heartbeat` = `NOT_CONFIGURED`), the running automated terminals show uninterrupted heartbeat. Confirm `password_enc` decrypts to a value that authenticates — checked **inside** the provisioner, result reported as a boolean only.
- **Failure handling:** if the AD reset fails, the Fernet store is **not** updated (commit-on-success), the old credential remains valid and consistent, and the failure is recorded as a durable `RuntimeEvent`/`FAILURE` with a **sanitised** reason (e.g. *directory unreachable*); raw text admin-only. No half-rotated state on AD failure. (The AD-success/store-fail window is covered by the reconciler above.)
- **Escalation:** repeated rotation failures open an `AlertEvent` through the existing reliability alerting cascade (Telegram → webhook → persist-only), severity WARN, escalating to CRITICAL only if the component is a critical one; admin ack/resolve lifecycle. No new alerting infrastructure.
- **Rollback:** re-drive the idempotent rotation op with a freshly generated password; because the store advances only on confirmed success, a failed attempt leaves the prior working credential in place. No old plaintext to restore.
- **Audit:** durable `RuntimeEvent` (rotation requested / succeeded / failed) plus the admin-op audit trail; **value never recorded.**

---

### (c) Broker account (MT5 login) password — customer-changed, provisioner re-injected **[DESIGNED — Phase 2+]**

- **Trigger:** the customer changes their **broker** account password at the broker, then updates the stored credential in GuvFX — or GuvFX detects `AUTHENTICATING → bad creds → FAILED` and prompts the customer to update it.
- **Authorised:** the customer changes it **at the broker** (GuvFX never changes a broker password on the customer's behalf). The customer submits the new broker password into GuvFX, which Fernet-encrypts it at rest; the **provisioner** performs the re-injection into the runtime. No agent handles the raw value.
- **Steps:**
  1. Customer changes the password in the broker's own portal (outside GuvFX).
  2. Customer enters the new broker password in the GuvFX account credential form → GuvFX **Fernet-encrypts** it into the broker-credential store (the prior cipher is superseded, never appended to logs).
  3. A `ProvisioningJob` `REPAIR`/re-inject op drives the runtime: provisioner decrypts **only at injection time**, transports over the authenticated WinRM/agent TLS channel, and writes it **only** into the per-account runtime dir ACL'd to `guvfx_u_<uid>` (no shared handoff dir).
  4. MT5 re-authenticates with the broker. The durable state machine (persist-then-act, reconciled after) converges to `RUNNING`. **Note the transition path is not a single hop:** from a `FAILED`/attention state the customer Retry drives `FAILED → QUEUED → PROVISIONING (re-materialise creds) → STARTING → AUTHENTICATING → RUNNING`; a re-inject on a `DEGRADED` runtime drives `DEGRADED → REPAIRING → RUNNING` (§8.2). The success hop is always `AUTHENTICATING ─login ok─▶ RUNNING`; do not describe re-injection as jumping straight into `AUTHENTICATING` without the intervening durable states.
- **Encryption boundary:** the decrypted broker password lives transiently only inside the per-account isolated runtime during injection; never in a shared path, log, Notion, or Git.
- **Verification:** `AccountRuntime.state == RUNNING` after re-inject; the Account Status panel `mt5_runtime` reads `RUNNING` and `hosted_terminal` reads `RUNNING` (the latter only when `runtime.state == RuntimeState.RUNNING` exactly). A live broker check may be confirmed via the **fail-safe `/mt5/order_check` probe (no order placed, best-effort, never raises)** — **never by placing or closing a trade.**
- **Failure handling:** a **wrong/stale broker credential** drives `AUTHENTICATING ─bad creds─▶ FAILED` (terminal — no *automatic* retry; manual Retry only) with a **sanitised** `last_error` (e.g. *invalid broker credentials*, *wrong server*); raw agent string admin-only. Transient broker/network faults follow `AUTHENTICATING ─transient─▶ retry ─exhausted─▶ DEGRADED`. Retry count `N` and the backoff schedule are **named but not numerically specified** in the current design (`retry×N(backoff)`) — **OPEN: source from the provisioning worker or fix in the ADR before (c) is armed.** A `FAILED` state offers the customer a **Retry** (`FAILED ─user/admin Retry─▶ QUEUED`) after they correct the broker password.
- **Escalation:** durable `FAILURE` `RuntimeEvent`s surface in `AdminBetaEstateView` (`runtime_state` + sanitised `runtime_last_error` + last-5 `FAILURE` events, `account_number` only, staff/superuser-gated); repeated/critical failures open an `AlertEvent` via the existing alerting cascade for admin ack/resolve. Source-scoped — `ti_signals` and `wayond` metrics never combined.
- **Rollback:** the previously working broker credential is **not** auto-restored (the customer changed it at the broker, so the old value is invalid by definition). Recovery: customer re-enters the correct current broker password → re-inject → re-authenticate. The stored cred is idempotently overwritten; runtime re-materialisation is idempotent (create-if-absent / overwrite config / re-inject).
- **Audit:** durable `RuntimeEvent` transitions with sanitised reason codes; **broker password value never recorded anywhere.**

---

### What is NOT covered / must be proven in Phase 4 (evidence rule)

- **MEASURED today:** only sub-procedure (a) is live — Django PBKDF2 hashing and JWT/cookie auth are operational. There are **no measured facts** for (b) or (c) — those subsystems (per-user Windows identity + broker-credential re-injection) are **unbuilt** — so no downtime, latency, or failure-rate number in this section is a measurement.
- **DESIGNED, not deployed:** (b) and (c) depend on the not-yet-shipped `ProvisioningJob`, the provisioning worker, the WinRM AD-cmdlet path, the RemoteApp/Guacamole entitlement rebuild, and per-account MT5 re-injection — none shipped. Gated on Nuno's approval of the §19 sequence / §20 BoM and the Phase-4 isolation + load gates. Today every runtime is `NOT_PROVISIONED`; the Phase-0 models record state only.
- **ESTIMATE, not measurement:** the "automated terminals keep running with minimal/zero downtime during a Windows-identity rotation" claim is a design expectation grounded in the logon-session model, **not measured**, and is conditional on the per-user launch-task credential being handled (see (b) step 4). It must be demonstrated — including a host-failover restart — under the Phase-4 load test before being stated as an SLO.
- **OPEN — launch-credential coupling (b):** if the AUTO_DEMO task stores the Windows password, rotation must update it (or a non-embedding credential model must be used). Undefined in the current design; must be fixed before (b) is armed.
- **OPEN — crash-window reconciliation (b):** the AD-success/store-commit window can diverge AD and the Fernet store; the reconciler must detect and re-drive. Must exist before (b) is armed.
- **OPEN — retry/backoff (c):** retry count `N` and backoff schedule for the `AUTHENTICATING`/`REPAIRING` transitions are undefined; fix in code or ADR before (c) is armed.
- **Divergent staleness/heartbeat formulas (observability):** two heartbeat-staleness formulas coexist (`heartbeat.py` grace-multiplier 2.5× vs `operations_summary.py` 2.0×/4.0× tiers). Any health-based verification of (b)/(c) must pick one explicitly and flag the divergence to the packet owner — not silently reconcile it.
- **Constraints upheld (fail-closed):** onboarding stays CLOSED (`BETA_ONBOARDING_ENABLED` default off, enforced at `mark_account_connected`/`mark_strategy_assigned`; no email-verify bypass); the `beta` plan's `can_deploy_automation` stays **`False`** (independent of provisioning); no forced trade, no `order_send`, no signal replay, no auto-remediation (detect → recommend → human/manual act → audit). Nuno's existing host, runtimes, broker accounts, Guacamole access, and AUTO_DEMO operation are untouched and out of scope.

### Runbook — Broker / account migration

> **Scope.** A user moves an existing `TradingAccount` to a different broker/server, or changes the login (`account_number`) on the same broker. This re-points the account's MT5 runtime at new broker connectivity. It does **not** move, mirror, close, hedge, or re-open any open market position or pending order.
>
> **Status — DESIGNED, NOT OPERATIONAL.** This procedure runs on the Option A §8 provisioning state machine (`AccountRuntime` + `RuntimeEvent` + `ProvisioningJob`), which is Phase 2+ and **not deployed**. Today every runtime is `NOT_PROVISIONED`, no `ProvisioningJob` model or provisioning worker exists, and there is no `MIGRATE` op — see §"What exists today vs. what this assumes". Nothing below is live behaviour until the Phase-2/3 worker ships and Phase-4 isolation gates pass. This runbook is authored for review, not execution.
>
> **Applies only to pooled beta runtimes.** Nuno's existing production Windows host, MT5 runtimes, broker accounts, Guacamole access, strategies, routing and AUTO_DEMO operation are **out of scope and must never be touched** by this procedure.
>
> **Onboarding gate is not involved.** Migration operates on an already-provisioned account. It must **never** require, open, or depend on `BETA_ONBOARDING_ENABLED` (default off), and must never bypass email verification. If the runtime is `NOT_PROVISIONED`, this is a provisioning task, not a migration — stop.

---

#### 1. Governance classification — **Red. Explicit, per-account approval required.**

| Aspect | Band | Why |
|---|---|---|
| Re-point `TradingAccount → AccountRuntime` routing | **Amber** | Alters an established execution/routing path (`architecture.md`: "No silent architecture replacement… requires an approved decision"). |
| A `STOPPED → QUEUED` re-provision transition (see §4.4) does **not** exist in §8.2 | **Amber** | Adding a state transition is an architecture change — needs an ADR/Notion decision before build, not an in-passing edit. |
| Change/re-inject broker credentials, re-provision runtime | **Red** | Credential + production execution access (`security.md`). |
| Any interaction with open positions | **Red** | Irreversible, broker-side, real money. |

**Gate:** Do not begin without Nuno's explicit, recorded approval for **this specific `account_id`**, naming source and target broker/server/login. Approval for one migration does not generalise (per-action, per-session). Record the sanitised approval reference in the migration `RuntimeEvent.detail` (never a credential).

---

#### 2. Trading-safety pre-conditions (MANDATORY — read to the user before any state change)

- **Open positions are broker-side and DO NOT transfer.** A position opened under broker A / login X lives in broker A's account. Changing `broker_server`/`account_number` points the runtime at a *different* account with an independent position book. This runbook **must not** auto-close, auto-flatten, auto-hedge, or auto-re-open any position, and **must not** "migrate" a position between brokers. No such code path exists and none may be added (`data.md`: raw broker records immutable).
- **Required explicit user action + written acknowledgement, before proceeding:**
  1. State the current open-position / pending-order count on the **source** account. This read is **best-effort and must happen while the runtime is still `RUNNING` and connected to the source broker — i.e. before STOP (§4.2) and before the config change (§4.3).** If no fail-safe read path exists, **fail closed to explicit user attestation** — do not assume "flat".
  2. Warn, verbatim: *"Migrating this account will disconnect its automated MT5 runtime from `<source broker/login>`. Any position or pending order still open on `<source broker/login>` will remain there, unmanaged by GuvFX automation, until you close it yourself in that broker account. GuvFX will not close, move, or reopen it."*
  3. Obtain the user's explicit instruction: either (a) they have flattened / will manage the source book themselves, or (b) they accept leaving it open and unmanaged. Do not infer consent.
- **Automation must be disarmed first** (§4.1) so no new order is placed on the source account mid-migration and none on the target before symbol availability is verified.

---

#### 3. Preconditions & isolation invariants (assert before starting)

**Preconditions**
- Governance approval recorded (§1); user trading-safety acknowledgement recorded (§2).
- Runtime is in a clean state to migrate: `RUNNING` (normal case) or `STOPPED`. If `DEGRADED`/`FAILED`/`REPAIRING`, reconcile to `STOPPED` or `RUNNING` first — §8.2 defines no STOP edge out of those states (see §4.2 note).
- Target broker/server exists as a reachable `BrokerServer`. Target `account_number` + credentials are supplied **by the user through the normal Fernet-encrypted path** (`GUVFX_FERNET_KEY`). **The operator never handles raw broker secrets** — never pasted into logs, chat, tickets, or a shared path.
- The **prior** broker config is captured for rollback (§8) **before** any field changes: `{broker_server, account_number, credential ciphertext ref, runtime_root, runtime_version}`.

**Isolation invariants (verify, do not assume)**
- **Exactly one runtime is touched.** `AccountRuntime` is the 1:1 (`OneToOne`) record owned by this `TradingAccount` (`related_name="runtime"`). Only this account's runtime row, its per-account dir `D:\GuvFX\users\<uid>\accounts\<account_id>\mt5\`, and its Guacamole/RemoteApp connection are modified.
- **No other tenant/account is read or written.** Resolution goes only through `_get_user_mt5_instance` (Phase-0, fail-closed) → the user's own per-account runtime or `None`.
- **NTFS ACL preserved.** Re-materialised dir stays ACL'd to `guvfx_u_<uid>` (+ SYSTEM/admin) only. The Windows identity `guvfx_u_<uid>` is **not** recreated or renamed — it is per-user and outlives the broker account.
- **Credential boundary preserved.** New credentials decrypted only at injection time, written only into the per-account ACL'd dir, never a shared handoff directory (kills C16 for this new path), never logged. Note: the legacy shared-handoff exposure (C16) persists on the un-migrated single-box path until Phase 2+ ships — this runbook does not resolve it there.

---

#### 4. Procedure — step by step (state-machine grounded)

Migration is **STOP → re-materialise config in place → resume (START)**, each a durable `ProvisioningJob` driven by the provisioning worker over WinRM/PowerShell, persist-then-act. There is **no dedicated `MIGRATE` transition**, and — critically — **no `STOP­PED → PROVISIONING/REPAIRING/QUEUED` transition exists in §8.2** (see §4.4).

##### 4.1 Disarm automation (app layer, reversible)
- Pause the account's AUTO_DEMO `StrategyAssignment`(s) so nothing is planned onto the runtime during migration. Strategy-layer toggle, **not** a runtime teardown — the runtime keeps its state.
- Confirm no in-flight `ExecutionJob` for this account is `RUNNING` (respect `EXECUTION_LEASE_TTL_SECONDS = 300`; wait for it to finish or lease-expire — **do not force-fail a live order job** as part of migration).

##### 4.2 Stop the runtime — `RUNNING → STOPPING → STOPPED`
- Enqueue a `STOP` `ProvisioningJob`. Worker drives `RUNNING ─(pause/deactivate)─▶ STOPPING ─▶ STOPPED`, writing a `RuntimeEvent` **before** each side-effect.
- **Note:** §8.2 defines the STOP edge only from `RUNNING`. If the runtime is not `RUNNING`, it must first be reconciled (per §3) — this runbook does not invent a STOP edge from `DEGRADED`/`FAILED`.
- Outcome: automated terminal stopped, runtime row persists in `STOPPED`. Interactive RemoteApp/Guacamole sessions are ephemeral and do not gate this.

##### 4.3 Update broker config (GuvFX DB — the routing/credential change)
- Update `broker_server` (and/or `account_number`) and re-inject the **new** Fernet-encrypted credentials on the `TradingAccount`. Treat as a **new configuration record** for audit; the prior ciphertext is retained per §3/§8, not destroyed.
- This is the Amber/Red config edit. Its `RuntimeEvent` (`event_type="TRANSITION"`, sanitised `reason_code="broker_migration"`, `detail` = approval ref + non-secret `old→new` server/login, **never** a credential) is the audit anchor.

##### 4.4 Re-materialise the portable runtime with the new server config — **state stays `STOPPED`**
- **State-machine constraint (do not violate).** §8.2 has **no** edge from `STOPPED` into `PROVISIONING`, `REPAIRING`, or `QUEUED`. The only edge out of `STOPPED` is `STOPPED ─(resume)─▶ STARTING`. Therefore the config re-materialisation is performed as an **idempotent side-effect of a `REPAIR`-op `ProvisioningJob` while the durable state remains `STOPPED`** — it is **not** a state transition. Overwrite the portable MT5 config with the new server, re-inject the new credential into the per-account ACL'd dir, ensure the per-user scheduled/logon task (all create-if-absent / overwrite, safe to re-run); bump `runtime_version`.
- **If a distinct visible provisioning state is wanted for migration** (e.g. surfacing "re-provisioning" to the user), that requires an **approved `STOPPED → QUEUED` (or equivalent) extension to §8.2** — an Amber architecture decision (ADR/Notion), never an in-passing edit. Do not assert such a transition until approved.
- Isolation check (§3) re-asserted: writes land only in `…\accounts\<account_id>\mt5\`.

##### 4.5 Resume & authenticate — `STOPPED → STARTING → AUTHENTICATING → RUNNING`
- Enqueue `START` (the `resume`). Worker drives `STOPPED ─(resume)─▶ STARTING ─launched─▶ AUTHENTICATING`.
  - `AUTHENTICATING ─login ok─▶ RUNNING` → proceed.
  - `AUTHENTICATING ─bad creds─▶ FAILED` → surfaced as sanitised *invalid broker credentials* / *wrong server* (raw agent text admin-only). **Stop and go to Rollback (§8)**; do not retry blindly against the wrong server.
  - `STARTING ─err─▶ DEGRADED`, or `AUTHENTICATING ─transient─▶ retry` (bounded exponential backoff via `attempt`/`next_retry_at`) `─exhausted─▶ DEGRADED`. Retry N + backoff schedule are **named but not numerically specified** in the design (open item — §"gaps"); do not invent values.

##### 4.6 Verify broker symbol availability (per-account gating) — **do not resume automation before this passes**
Symbol names/availability are **broker/account-specific** (e.g. Wayond gates a `WAY…`-prefixed set). Migration to a new broker can silently make a strategy's instrument unavailable. Re-verify against the broker/account-aware registry, on the **target**:
- Refresh/repopulate the **target-scoped**, fail-closed `BrokerInstrument` cache so gating keys off the new broker, not the stale source cache.
- Bridge-side MT5-native `validate_broker_symbol` (`symbol_info`/`symbol_select`) must confirm, on the **new** runtime, that every symbol used by the account's assigned strategies is present and selectable on the target broker.
- Any required symbol missing on target → leave automation **disarmed**, surface a clear per-account message, treat as a blocking finding (§7). Do not force-arm.

##### 4.7 Resume automation
- Only after 4.5 (`RUNNING`) **and** 4.6 (symbols verified on target) pass, re-enable the AUTO_DEMO `StrategyAssignment`(s). Routing resolves via the fail-closed per-account `_get_user_mt5_instance`. One user's arming never affects another's.

---

#### 5. Verification (must actually run — evidence rule; PASS only when criteria executed)

| # | Check | Source | Pass condition |
|---|---|---|---|
| V1 | Runtime state | `AccountRuntime.state` | `RUNNING` (exact enum, not a collapsed user-facing label) |
| V2 | Terminal + login | Account Status panel `hosted_terminal` | `"RUNNING"` only when `state == RuntimeState.RUNNING` exactly |
| V3 | Broker config applied | `TradingAccount.broker_server`/`account_number` | equal target values |
| V4 | Symbols available on target | `BrokerInstrument` (target-scoped) + bridge `validate_broker_symbol` | every strategy symbol present/selectable on the new broker |
| V5 | No cross-tenant touch | `RuntimeEvent` set for the window | events exist **only** for this `runtime_id`; no other `AccountRuntime` row changed |
| V6 | First post-migration execution | `last_execution` panel stage / `ExecutionJob.status` for this account | panel `HEALTHY` iff most recent `ExecutionJob.status == "SUCCESS"`; if none forced, stage stays `NOT_CONFIGURED` (no jobs) or `WARNING` (PENDING/RUNNING) — **do not force a trade to prove this** (`ExecutionJob` has no `NOT_CONFIGURED` status) |
| V7 | Credentials not leaked | logs, `RuntimeEvent.detail`, `AdminBetaEstateView` | only non-secret `account_number` / sanitised strings; **0 plaintext credential rows/log lines** |

State explicitly what was **not** covered — e.g. "no forced trade — V6 evidence PENDING", "source-account open positions not verified closed — left to user per §2, attestation only".

---

#### 6. Isolation verification (defence-in-depth, re-confirm post-migration)
1. Windows: runtime dir still ACL'd to `guvfx_u_<uid>` only; `is_admin == False`.
2. RDS/Guacamole: the account's connection maps only to this user's RemoteApp collection + this runtime; no shared connection object created.
3. GuvFX app: `_get_user_mt5_instance(user, account)` resolves to the migrated runtime for the owner, `None` for anyone else.
4. No other `AccountRuntime` row changed `updated_at` during the window; no production-box runtime touched.

---

#### 7. Escalation
- `AUTHENTICATING → FAILED` (*invalid broker credentials* / *wrong server*): stop, re-verify target creds/server with the user via the encrypted path, or **Rollback (§8)**. Do not iterate against production broker auth.
- Repeated `DEGRADED`/retry-exhaustion → `FAILED`: raw agent detail is admin-only in `RuntimeEvent`; escalate to ops with the sanitised reason + `RuntimeEvent` chain. Follow **detect → recommend → human/manual act → audit** (`RecoveryRecommendation` advisory only; no auto-remediation).
- Symbol gap on target (V4 fail): blocking; escalate to Nuno with the missing-symbol list — do not open automation.
- Suspected credential exposure (`security.md`): **stop, do not commit, report with redacted detail so the credential can be rotated.**
- Any doubt about open positions on the source account: escalate to the user; never auto-act.

---

#### 8. Rollback (restore prior broker config + restart) — never a position action
1. `STOP` the runtime → `STOPPED` (if at `FAILED`, first restore config, then use `FAILED ─Retry─▶ QUEUED ─(host slot)─▶ PROVISIONING → STARTING → AUTHENTICATING → RUNNING` — the one in-machine route that re-materialises config).
2. Restore the **captured prior** `{broker_server, account_number, credential ciphertext}` onto the `TradingAccount` (§3) as a **new configuration record / new `runtime_version`**, not a silent in-place edit (`data.md`).
3. Re-materialise (idempotent) with the restored config — same `STOPPED`-side-effect constraint as §4.4 (state stays `STOPPED`; no invented transition) — then resume `STOPPED → STARTING → AUTHENTICATING → RUNNING`.
4. Re-run V1–V4; only then, if previously armed, re-arm.
5. Write a `RuntimeEvent` `reason_code="broker_migration_rollback"` with the approval/incident reference.

> Rollback re-points the runtime at the original broker/login. It does **not** recover or reopen any position the user closed in the interim — positions are broker-side, outside this runbook.

---

#### 9. Audit
- Every state transition (4.2/4.5), the in-place config re-materialisation (4.3/4.4), and rollback (§8) writes an **immutable, append-only `RuntimeEvent`** (app-layer `save()`/`delete()` refuse mutation; DB BEFORE-UPDATE trigger, migration `terminal_provisioning/0005`). Ordering `["id"]`, chronological.
- Records carry **sanitised, non-secret** fields only: `reason_code` (≤64), `detail` (≤2000; no raw agent strings, no credentials). Approval reference (§1) and non-secret `old→new` server/login belong here.
- Admin visibility via `AdminBetaEstateView` (`IsSuperOrOpsAdmin`, capped, read-only): `runtime_state`, sanitised `runtime_last_error`, last-5 `FAILURE` events, non-secret `account_number` — **never** decrypted credentials.
- Handoff: record branch, base commit, resulting commit(s), the exact §5 verification commands run and their **actual** results (not "should pass"), and any deviation from this runbook.

---

#### 10. SLO linkage (reuse existing vocabulary — do not invent thresholds)

No SLO/SLI model exists yet (SLOs would be new — flagged, not assumed). Until one is defined, key this runbook to **already-codified** primitives:

| Signal | Existing primitive | Note |
|---|---|---|
| Migration provisioning latency | inter-event Δt across the migration `RuntimeEvent` chain (`created_at`) | Becomes MEASURED per run **once the Phase-2+ worker ships and emits events**; today no migration is measurable (all runtimes `NOT_PROVISIONED`). No target SLO exists. |
| Stuck migration job | `ExecutionJob`/`ProvisioningJob` lease vs. `EXECUTION_LEASE_TTL_SECONDS = 300` (RX-2E) | Reuse existing orphan detection; do not add a new mechanism. |
| Runtime heartbeat after migration | *(no per-runtime heartbeat exists yet)* | **There is no per-account MT5 runtime heartbeat today** — `account_status` stage 8 `last_heartbeat` is hardcoded `NOT_CONFIGURED`. The worker-heartbeat formula `expected_interval_s (60s) × HEARTBEAT_GRACE_MULTIPLIER (2.5)` → binary OK/FAILED (`heartbeat.py`) is the **vocabulary to reuse when a runtime heartbeat is later added**, not a currently-measurable migration signal. Also note the divergent `operations_summary` two-tier formula (`_STALE_FACTOR 2.0` / `_CRITICAL_FACTOR 4.0`) — flag, do not silently reconcile. |
| No silent signal loss post-migration | `operations_summary._signal_execution_block` (settle 300s, `all_accounted = pending_stuck == 0`), **source-scoped** | Never combine `ti_signals` + `wayond`. |

**Density / capacity:** the "≤~2 users per RDSH" and per-RDSH RAM budget remain **unproven assumptions** — *"Density is a Phase-4 proof, not an assumption — raise only with evidence."* This runbook asserts **no** migration throughput/latency SLO as proven; those are Phase-4 measurements.

---

#### What exists today vs. what this assumes (MEASURED vs. designed)
- **MEASURED / shipped (Phase 0):** `RuntimeState` (14-value enum: `NOT_PROVISIONED, QUEUED, BLOCKED, PROVISIONING, STARTING, AUTHENTICATING, RUNNING, DEGRADED, REPAIRING, STOPPING, STOPPED, DEPROVISIONING, REMOVED, FAILED`), `AccountRuntime` (1:1, records **state only**, does not provision), `RuntimeEvent` (immutable), `runtime_state.record_transition` (persist-then-act, atomic, `select_for_update`), `account_status.build_account_status`, fail-closed `_get_user_mt5_instance`, `AdminBetaEstateView`, `beta` plan `can_deploy_automation=False`, closed gate `BETA_ONBOARDING_ENABLED` (default off).
- **DESIGNED / not shipped (Phase 2+, needs Nuno approval of §19/§20):** `ProvisioningJob`, the provisioning worker, WinRM/PowerShell agent endpoints, per-account RDSH portable-MT5 materialisation, RemoteApp/Guacamole per-account connection, live state-machine driving. **There is no `MIGRATE` op, and no `STOPPED → PROVISIONING/REPAIRING/QUEUED` transition in §8.2** — migration is the STOP → in-place re-materialise (state `STOPPED`) → resume composition above.
- **NOT covered / to prove in Phase 4:** live end-to-end migration behaviour (every runtime is `NOT_PROVISIONED` today); retry N + backoff schedule (named, numerically unspecified); forced-trade V6 evidence (not produced by placing a real order); per-runtime heartbeat telemetry (does not exist); reconciliation of the two heartbeat-staleness formulas; whether migration should get its own visible provisioning state (needs a §8.2 extension / Amber ADR); source-account open-position handling remains a **manual user responsibility**, never automated by GuvFX.

### Runbook — Single account-runtime recovery

> **Status: DESIGN DOCUMENTATION ONLY.** This runbook extends the Option A design doc
> (`BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md`, §8/§9/§14/§15). It describes the *target*
> operational procedure for recovering a single account-runtime. **Nothing here is operable
> today.** The provisioning worker and Windows agent that would execute these transitions are
> **unbuilt** (Phase 2+), so in production **every** `AccountRuntime` is `NOT_PROVISIONED` and
> no MT5 terminal, identity, or runtime exists to recover. The durable records this runbook
> reads (`AccountRuntime`, `RuntimeEvent`) *are* shipped (Phase 0), so the diagnostic steps are
> real; the actioning steps (`REPAIR`/`START`/`STOP`) are target-state and are marked **TARGET**
> where they depend on the unbuilt worker/agent. Onboarding stays CLOSED
> (`BETA_ONBOARDING_ENABLED` off); `can_deploy_automation` stays `False` for beta.

#### Purpose / trigger

Recover **one** broker account's MT5 runtime when it has fallen out of `RUNNING` while **its host
is still healthy**. Concretely, this runbook is triggered by either of:

- **`RUNNING → DEGRADED`** — a per-runtime health check failed for a runtime that *was* running:
  the automated (AUTO_DEMO) terminal process is gone, or the terminal is up but no longer
  logged in to the broker. Design intent (§8.1) is that `DEGRADED` renders to the user as
  *"Degraded (auto-repairing)"* and the self-healing reconciler drives repair automatically.
- **`… → FAILED`** — a terminal, non-retryable condition, or a retryable one whose bounded
  retry budget is **exhausted**: bad broker credentials (`AUTHENTICATING → FAILED`),
  provisioning materialisation exhausted (`PROVISIONING → FAILED`), or repair exhausted
  (`REPAIRING → FAILED`). `FAILED` renders as *"Failed (reason) — Retry"* and needs an
  operator/user `Retry` before anything moves.

A secondary trigger is **`… → BLOCKED`** encountered *during a re-seat* of an already-owned
runtime (e.g. the reconciler tries to re-materialise onto a pool host and finds no slot). That
is in scope only as the single-runtime capacity branch below; broad capacity exhaustion is not.

The **canonical state** is always the durable `AccountRuntime.state` — never a transient probe.
Per `runtime_state.user_facing_state()`, the panel label is derived **only** from the durable
record. Do not diagnose or act off a live process check alone.

#### Scope & preconditions

**In scope**

- Exactly **one** `AccountRuntime` (1:1 FK-owned by one `TradingAccount`, §0).
- The RDSH **host that runtime lives on is HEALTHY** (reachable over WinRM/agent, not
  resource-starved, other runtimes on it are fine).
- Recovery back to `RUNNING`, or a safe park at `STOPPED`.

**Out of scope (each has its own runbook / section)**

- **Host-wide failure** — an RDSH is down or unreachable, or multiple runtimes on it are
  affected. That is the *Host failure & account recovery* path (§14 / the host-failure runbook):
  re-materialise the account's runtime on another pool host and re-point routing. **Do not** use
  this runbook to respond to a host outage.
- **Capacity exhaustion beyond a single `BLOCKED`** — pool-wide "no slots anywhere" is a
  capacity/scaling event (§3/§17: add an RDSH), not a single-runtime repair. This runbook only
  covers freeing/finding **one** slot for **one** re-seat.
- **Credential change** — rotating or re-entering broker credentials (the injection/Fernet
  boundary, §9) is its own procedure (the password/credential runbook). This runbook may *route
  to* it (bad-creds → user re-enters creds), but does not perform it.
- **Deprovision / offboard** (`DEPROVISIONING → REMOVED`) — lifecycle teardown, not recovery.

**Preconditions before starting**

1. You can read the durable records (`AccountRuntime`, `RuntimeEvent`) for the affected
   account — via the Django admin surface or an authorised read path.
2. You have confirmed the **host is healthy** (if not, stop — use the host-failure runbook / §14).
3. You have confirmed the symptom is a **single** runtime (if many, suspect host — use §14).

#### Roles & authorisation (Green / Amber / Red governance)

Recovery actions map onto the GuvFX governance overlay. **Fail-closed at every tier**: if the
tier of an action is unclear, treat it as the more restrictive tier.

| Tier | Who / what | Actions in this runbook |
|---|---|---|
| **Green** — automatic, behaviour-preserving, single-runtime scope | The **self-healing reconciler** (extends `execution_health`, §8.2/§14) | Detect `RUNNING → DEGRADED`; enqueue a `REPAIR` `ProvisioningJob`; drive `DEGRADED → REPAIRING → RUNNING` via **idempotent** re-materialise/restart under **bounded exponential backoff** (`attempt`/`next_retry_at`); on success return to `RUNNING`; on exhaustion move to `FAILED`/`DEGRADED` with a truthful reason. No operator action required or expected. |
| **Amber** — touches shared structure/capacity or a manual state change; needs an operator with a documented decision + handoff flag | **L2 ops operator** | Manual **`Retry`** on a `FAILED` runtime (`FAILED → QUEUED`); **safe stop** (`→ STOPPING → STOPPED`) when repair loops; **freeing one slot** on a shared RDSH or requesting one host be added (§3) to clear a single `BLOCKED`. These affect shared host capacity or force a state change, so they require an explicit, recorded decision and a note in the handoff. |
| **Red** — live/production authority, credentials, irreversible or out-of-scope | **Nuno only** | Anything touching **production trading authority**, **raw broker/Windows credential values**, **Nuno's untouched production box**, flipping **`BETA_ONBOARDING_ENABLED`** on, flipping **`can_deploy_automation`** to `True`, **procurement** of hosts/CALs, or **placing/closing any trade**. None of these are ever performed to "recover" a runtime. Escalate; do not proceed. |

The reconciler's authority is deliberately narrow: it may re-run idempotent materialisation and
restart of **one** runtime it already owns. It may **not** cross into another tenant, place or
close a trade, decrypt or handle raw secrets, or open onboarding. Any step outside Green is a
human decision.

#### Isolation & safety invariants

These hold for every action, automatic or manual. Violating any one is a stop-and-escalate event.

1. **Single-runtime blast radius.** Only the affected account's `AccountRuntime` and its own
   per-account runtime directory (`D:\GuvFX\users\<uid>\accounts\<account_id>\mt5\`, NTFS-ACL'd
   to `guvfx_u_<uid>`, §7) are touched. No other tenant's identity, runtime, or session is read
   or modified.
2. **Nuno's production box is never in scope.** It is not in the pool (§2/§18) and no recovery
   action may reach it.
3. **Fail-closed.** If the owned runtime cannot be resolved or safely repaired, the account
   resolves to **no runtime** (`_get_user_mt5_instance` returns `None`, §11) with a clear
   message — never a shared or other-user box, never a silent success.
4. **Never place or close a trade.** Recovery restarts a *terminal*; it never sends an order,
   closes a position, or modifies a stop. Positions are broker-side and are not touched (§14).
5. **Secrets stay Fernet-encrypted.** Broker credentials and the Windows identity password are
   decrypted **only at injection time**, inside the isolated runtime, over the authenticated
   agent/WinRM (TLS) channel (§9). The **operator and the agent never handle raw secret values**;
   an operator's "re-inject cred" is an *op the worker performs*, not a value the operator sees.
6. **Sanitised vs raw error separation is enforced in code.** `record_transition()` writes only
   the **sanitised `reason_code`** into `AccountRuntime.last_error` (user-safe, capped 500 chars),
   and never lets raw `detail` reach that field. The raw diagnostic lives on the immutable
   `RuntimeEvent.detail` (admin-only, capped 2000 chars). Operators read raw detail; users never
   do.
7. **Persist-then-act, never swallow.** Every step writes the next durable state + a
   `RuntimeEvent` **before** the side-effect, reconciles the result after, and on error records
   it and applies the retry/`DEGRADED`/`FAILED` policy. No `pass`, no false success (§8.3).

#### Decision tree

Map the observed **durable state + latest `RuntimeEvent.reason_code`** to the correct path.
"AUTO" = Green reconciler path; "MANUAL" = Amber operator action.

```
Observed AccountRuntime.state
│
├─ DEGRADED  (was RUNNING; health check failed: terminal gone / not logged-in)
│    └─ AUTO: reconciler enqueues REPAIR → DEGRADED → REPAIRING
│             REPAIRING = idempotent re-materialise + restart, bounded exponential backoff
│             ├─ ok       → RUNNING            (recovered — verify)
│             └─ exhausted → FAILED            (fall through to FAILED branch)
│       MANUAL only if AUTO is not progressing (see step-by-step) — do NOT hand-drive a
│       repair the reconciler is already retrying; you will double-drive the state.
│
├─ FAILED  (terminal / retries spent) — read reason_code to choose:
│    ├─ "invalid broker credentials"  (AUTHENTICATING → FAILED)
│    │     → NOT an ops repair. User must re-enter broker creds (credential-change runbook).
│    │       After creds updated: MANUAL Retry → FAILED → QUEUED → (worker) PROVISIONING…→ RUNNING
│    ├─ "host at capacity"  (materialise/re-seat had no slot, exhausted)
│    │     → capacity branch below (free a slot / add a host), then MANUAL Retry → QUEUED
│    └─ other (provisioning/repair exhausted, transient that ran out of budget)
│          → MANUAL Retry → FAILED → QUEUED  (re-drives from durable state; idempotent)
│            If Retry re-FAILS with the same reason_code → escalate (do not loop Retry).
│
├─ BLOCKED  (prerequisite missing during a re-seat: no host slot / entitlement / gate)
│    ├─ reason "host at capacity" for THIS single re-seat:
│    │     MANUAL (Amber): free one slot on a healthy pool host, OR request +1 RDSH (§3/§17).
│    │     When cleared → BLOCKED → QUEUED  → (worker) PROVISIONING…
│    └─ reason = entitlement / gate closed:
│          EXPECTED during beta. can_deploy_automation is False and onboarding is CLOSED by
│          design. This is fail-closed working correctly — do NOT "fix" by flipping a gate
│          (that is Red / Nuno). Leave BLOCKED; record and stop.
│
└─ broker unreachable / transient fault (surfaced mid-AUTHENTICATING or a health blip)
     → AUTO: retry under backoff.
        ├─ AUTHENTICATING transient → retry → exhausted → DEGRADED (then DEGRADED branch)
        └─ STARTING error           → DEGRADED (then DEGRADED branch)
       Distinguish from "invalid broker credentials": transient = broker/network reachability,
       retryable; bad creds = terminal, needs the user, NOT retryable by ops.
```

**AUTO vs MANUAL, stated plainly:**

- The **reconciler (AUTO/Green)** owns the `DEGRADED → REPAIRING → RUNNING` loop and all
  bounded-backoff retries. In steady state an operator does **nothing** for a `DEGRADED`
  runtime — they *watch* it recover.
- An **operator (MANUAL/Amber)** acts only when the automatic loop has terminated at `FAILED`
  (press `Retry` after the blocking condition is cleared), or when a runtime is stuck `BLOCKED`
  on capacity for a single re-seat (free/add a slot), or when repair is looping and the safe
  move is to **stop** (`→ STOPPED`).

#### Step-by-step procedure

Every actioning step is a **single durable state transition** via
`terminal_provisioning.runtime_state.record_transition(runtime, to_state, …)` — the worker
persists the next state + an immutable `RuntimeEvent` **before** attempting the side-effect and
reconciles after. Operators **enqueue ops / press Retry**; they do **not** call side-effects by
hand. Steps that depend on the unbuilt worker/agent are marked **TARGET**.

1. **Confirm scope.** Verify the host is healthy and only this one runtime is affected. If the
   host is down or several runtimes are affected → **stop, go to the host-failure runbook (§14)**.

2. **Read the durable state.** Load the `AccountRuntime` for the affected `TradingAccount` and
   read `state`, `attempt`, `last_error` (sanitised), and `next_retry_at`. This is the single
   source of truth; do not substitute a live process probe.

3. **Read the latest evidence.** Pull the most recent `RuntimeEvent` rows for this runtime
   (append-only, `ordering = ["id"]`). For each: `event_type` (`TRANSITION`/`RETRY`/`FAILURE`),
   `from_state → to_state`, the **sanitised `reason_code`**, and the **admin-only raw `detail`**.
   Use `reason_code` to classify (creds vs capacity vs transient vs entitlement); use `detail`
   for root-cause. **Never surface raw `detail` to a user.**

4. **Classify** against the decision tree using `state` + latest `reason_code`.

5. **If `DEGRADED` and the reconciler is progressing** (recent `REPAIRING` events, `attempt`
   incrementing, `next_retry_at` in the near future): **do nothing but observe.** The Green path
   owns this. Re-check after the next backoff window. Only intervene if it terminates at
   `FAILED` (→ step 7) or is not progressing (→ step 6).

6. **If `DEGRADED` and NOT progressing** (no reconciler events advancing, `next_retry_at` stale
   or unset, worker not claiming): this is a stuck automatic path, not a normal repair. Do **not**
   hand-drive `REPAIRING` in parallel with the reconciler. Instead: confirm the provisioning
   worker is alive and leasing; if the worker is down that is an infrastructure fault → escalate
   to L3. If the worker is healthy but this runtime is wedged, the safe Amber action is a
   controlled **stop** (step 9), then a fresh `Retry`.

7. **If `FAILED`, act on `reason_code`:**
   - `"invalid broker credentials"` → **not an ops repair.** Route the user to re-enter broker
     credentials (credential-change runbook). Only after creds are updated do you (or the user)
     press **`Retry`** → `record_transition(rt, QUEUED, …)` → the worker re-drives
     `PROVISIONING → STARTING → AUTHENTICATING → RUNNING` **(TARGET)**.
   - `"host at capacity"` → go to the **capacity branch** (step 8) first, then `Retry`.
   - any other exhausted/terminal reason → **`Retry`** (`FAILED → QUEUED`). Because every step is
     idempotent (create-if-absent identity/dir, overwrite config, re-inject cred, ensure-task),
     re-entry converges to the target **(TARGET)**. If it re-`FAILED`s with the **same**
     `reason_code`, **stop retrying** and escalate (see Escalation) — a looping Retry is not a fix.

8. **Capacity branch (single `BLOCKED`/`"host at capacity"` on a re-seat).** Confirm this is a
   *single*-slot problem, not pool-wide (pool-wide = out of scope, §3/§17). Then, as an Amber
   decision with a recorded rationale: free one slot on a healthy pool host **or** request one
   RDSH be added. When the prerequisite clears, the runtime moves `BLOCKED → QUEUED`
   (`record_transition(rt, QUEUED, reason_code="capacity cleared")`) and the worker resumes
   **(TARGET)**. If the blocker is **entitlement / gate closed** (`can_deploy_automation` False,
   onboarding CLOSED), this is fail-closed **working as designed** — record it and stop; do
   **not** flip any gate (Red / Nuno).

9. **Safe stop (if repair loops).** If REPAIR/Retry cannot converge, park the runtime rather
   than loop: `record_transition(rt, STOPPING, …)` then `→ STOPPED`. The runtime record still
   exists; the terminal is not running; the account fails closed (no shared box). This halts
   the backoff churn and hands off to a human at a known-safe state **(TARGET for the actual
   terminal stop; the state transition itself is real)**.

10. **Record and hand off.** Regardless of outcome, the `RuntimeEvent` chain already captures
    the trail. Add the operator decision (what was done, why, which tier) to the handoff.

#### Verification (post-conditions)

Recovery is confirmed **only** when the durable state and health agree:

1. **`AccountRuntime.state == RUNNING`** (durable), rendering as user-facing `RUNNING`.
2. **Automated terminal up + logged in** — the account's AUTO_DEMO terminal process is running
   under `guvfx_u_<uid>` and authenticated to the broker.
3. **Recent heartbeat** — the runtime reported a health heartbeat within the expected window.
4. A closing `RuntimeEvent` records the `→ RUNNING` transition (and `attempt` reset per policy).
5. `last_error` is cleared — `record_transition()` sets `last_error = ""` on any transition to a
   non-attention state (only `FAILED`/`DEGRADED`/`BLOCKED` carry a reason).

> **Verification is PARTIAL today (honest limitation).** Per §15, **per-runtime heartbeat
> telemetry is a Phase-2 build and is NOT yet configured.** Until it exists, conditions (2) and
> (3) above **cannot be positively measured** — an operator can confirm the *durable state* and
> the *event chain* but **cannot yet prove "logged-in + heartbeat" from telemetry**. Do not
> record a fully-verified recovery on state alone; mark verification `PARTIAL` and name the
> missing heartbeat check. This is a **stated coverage gap**, not a green result.

#### Rollback / abort

- **Safe park, not force.** The abort is a controlled **stop** to `STOPPED` (procedure step 9),
  never a hard kill that leaves the durable state lying. `STOPPED` is a truthful, fail-closed
  resting state: runtime record present, terminal not running, account resolves to no live box.
- **When to stop retrying:** (a) a `Retry` re-`FAILED`s with the **same** `reason_code`; (b) the
  reconciler has spent its bounded backoff budget and re-entered `FAILED`; (c) the same
  `DEGRADED → REPAIRING → FAILED` cycle repeats without a changing root cause in
  `RuntimeEvent.detail`. In all three, **stop and escalate** — repeated identical retries are
  churn, not recovery, and they violate persist-then-act discipline if hand-driven in parallel
  with the reconciler.
- **Never** abort by touching another runtime, another host, the credential store, a trade, or
  a governance gate.

#### Escalation

| To | When |
|---|---|
| **L2 ops** | Green reconciler terminated at `FAILED`; a single `BLOCKED`/capacity re-seat needs a slot freed or a host added; a controlled stop is warranted. (Amber decisions live here.) |
| **L3 engineering** | Provisioning worker not claiming/leasing; agent/WinRM channel faults; the same `reason_code` recurs across Retries; suspected host-level or multi-runtime symptom (hand off toward §14); any `RuntimeEvent.detail` indicating a code/agent defect. |
| **Nuno (Red)** | Anything requiring production trading authority, raw credentials, the untouched production box, flipping `BETA_ONBOARDING_ENABLED` or `can_deploy_automation`, or procurement. Also: suspected cross-tenant exposure or a possible secret leak — **stop, do not commit, report with redacted detail** (security rule) so it can be rotated. |

#### Audit / evidence

- The **`RuntimeEvent` chain is the primary evidence**: append-only and content-immutable
  (refused at the app layer in `save()`/`delete()` and by a DB BEFORE-UPDATE trigger, migration
  0005). Rows leave only by CASCADE when the owning account/runtime is deleted — a lifecycle op,
  not a rewrite. It captures, in chronological order, every `from_state → to_state`, the
  sanitised `reason_code`, and the admin-only raw `detail`.
- **Redaction in evidence:** when recording this recovery in a handoff, cite the **sanitised
  `reason_code`** and the category of the fault, not raw agent strings and never a secret value
  (security + evidence rules). Reference the `RuntimeEvent` ids rather than pasting raw `detail`.
- **Operator actions** (Amber `Retry`, stop, slot-free/host-add decision) are recorded in the
  ops audit log alongside the event ids, with the tier and the rationale.
- **Evidence honesty:** mark the recovery `PASS` only if the RUNNING + logged-in + heartbeat
  post-conditions were actually met. Because heartbeat is not yet built, a real recovery today
  is at best `PARTIAL` — say so; do not assert a green result that was not produced.

#### SLO linkage

- **Target:** single-runtime repair (`DEGRADED → REPAIRING → RUNNING`) completes within the
  SLO-2a recovery-time objective, to be fixed in Phase 2 alongside the backoff schedule.
- **This is a TARGET, not a MEASURED value.** No single-runtime recovery time has been measured,
  because the worker/agent are unbuilt and there is no heartbeat to time against. The SLO cannot
  be validated until **Phase 4** load + isolation proving produces real recovery timings. Until
  then, treat any recovery-time figure as an **estimate pending measurement** and never present
  it as an observed SLO.

#### Residual risks / assumptions

- **State machine, provisioning worker, and Windows agent are DESIGN-ONLY (unbuilt).** In
  production **every `AccountRuntime` is `NOT_PROVISIONED`** — there is nothing to recover yet.
  The actioning steps in this runbook are target-state; only the durable-record diagnostics are
  live today.
- **Retry count `N` and the exponential-backoff schedule are UNSPECIFIED** in both code and the
  design doc (`attempt`/`next_retry_at` exist as fields, but no concrete `N`, base, cap, or
  jitter is defined). These **must be sourced and pinned in Phase 2** before this runbook's
  retry/exhaustion logic can be operated.
- **Per-runtime heartbeat is NOT_CONFIGURED** (Phase 2). Health verification is therefore
  **partial**: durable state and the event chain are confirmable; "logged-in + live heartbeat"
  is not yet measurable.
- **Sanitised-reason mappings are a fixed, small set** (*invalid broker credentials*, *wrong
  server*, *host at capacity*, *broker unreachable*, §8.2). A fault outside that set will surface
  a generic sanitised code to the user with root cause only in admin-only `detail` — classify
  from `detail`, not from the user-facing label.
- **Nothing in this runbook is authorised to run against production.** It implies **no**
  procurement, **no** flip of `BETA_ONBOARDING_ENABLED` (onboarding stays CLOSED), and **no**
  flip of `can_deploy_automation` (stays `False` for beta). All three remain Red / Nuno-gated and
  are explicitly out of any recovery action.
- **Assumes host health is correctly determined up front.** Misclassifying a host-wide failure
  as a single-runtime fault would route recovery through the wrong runbook; step 1 is the guard,
  and any multi-runtime signal sends the operator to §14.

### Runbook — Windows host (RDSH) failure & pool recovery

> **Status: DESIGN — NOT YET OPERATIONAL.** This runbook describes the *designed* recovery behaviour of the Option A beta pool (§8 state machine, §14 recovery). None of it is live today: every `AccountRuntime` in production is `NOT_PROVISIONED`, the provisioning worker / `ProvisioningJob` queue / Windows-agent provisioning endpoints are Phase 2+ and gated on Nuno's approval of §19/§20, and onboarding is closed (`BETA_ONBOARDING_ENABLED=0`, default). Nuno's existing production MT5 box, runtimes, routing and AUTO_DEMO operation are **not part of this pool** and are untouched by any procedure here. **Do not execute these steps against production**; this is the procedure that becomes operational *after* Phase 4 isolation gates pass.
>
> **MEASURED vs ESTIMATE.** The durable state models (`AccountRuntime`, `RuntimeEvent`, the 14-value `RuntimeState` enum, `record_transition`, migration `0005` immutability trigger), the read-only status/estate surfaces (`AdminBetaEstateView`, Account Status panel), `ExecutionJob` lease semantics (`EXECUTION_LEASE_TTL_SECONDS=300`, RX-2E), and the heartbeat grace multiplier (`HEARTBEAT_GRACE_MULTIPLIER=2.5`) are **MEASURED** (shipped code). Everything about *recovery execution* — a per-RDSH host-heartbeat source, per-host density (~2 interactive users/RDSH), the RAM budget, retry counts/backoff, snapshot cadence, and all recovery-time / SLO numbers — is **ESTIMATE / DESIGN**, an explicit Phase-4 proof and marked inline. No estimate below is a measurement.

---

#### Severity ladder

| Sev | Trigger | Blast radius | Data loss | New logins during outage |
|---|---|---|---|---|
| **SEV-2** | One **RDSH session host** dies | Only that host's account runtimes — ~2 interactive users, **≈ 10 automated terminals at the heavy profile (5/user) and up to ≈ 20 at the max profile (10/user)** per capacity §3.4/§3.5; ESTIMATE, not a proven ceiling — the actual displaced count is measured live in §5 | None (positions broker-side; runtime rebuildable from durable state + Fernet creds) | Unaffected users unaffected; displaced runtimes re-materialise on healthy hosts (capacity permitting) |
| **SEV-1** | **Infra VM** dies (DC + RDCB + RDGW + Licensing) | Entire pool's *control plane* — **single point of failure at beta** | None on the infra VM (positions broker-side; the authoritative GuvFX DB lives on the **Ubuntu VPS, not the infra VM** — see backup-gap caveat) | **All** new sessions/logins blocked; already-running automated terminals keep trading, with the DC caveat below |

The severity gap is deliberate: an RDSH is one member of a redundant pool; the infra VM is, at beta, collapsed onto **one Windows Server VM** (§2) with no standby. HA-splitting it is post-beta (§17), unscheduled and unauthorised.

---

### SEV-2 — An RDSH session host fails

**Ownership invariant preserved throughout:** `User → Broker Account → MT5 Runtime (1:1, owned by the account) → Strategies → Positions`. Recovery re-materialises a *runtime* onto a new host; it never re-parents ownership, never merges tenants, never touches another user's runtime. The per-user Windows identity `guvfx_u_<uid>` is per-**user** and may exist on more than one RDSH; the runtime is per-**account**.

#### 1. Detect

| Signal | Source | MEASURED / DESIGN | Meaning |
|---|---|---|---|
| Host heartbeat gap | **New** per-RDSH agent heartbeat, evaluated by the reconciler as `age > expected_interval_s × HEARTBEAT_GRACE_MULTIPLIER` | Multiplier `2.5` MEASURED; the RDSH host-heartbeat **source and its `expected_interval_s` do not exist yet** (DESIGN — Phase 2) | RDSH agent silent → host suspect |
| Per-account runtime health fail | `ComponentHealth` + a runtime health probe (terminal running / logged-in / last heartbeat) | `ComponentHealth` model MEASURED; the runtime health probe is DESIGN (Phase 2) | Terminals on that host stopped reporting |
| `ExecutionJob` orphans | RUNNING job with `lease_expires_at` elapsed vs `EXECUTION_LEASE_TTL_SECONDS=300` | MEASURED (RX-2E) | In-flight work on the dead host is stuck — **reconcile against broker Trade records, never replay** (§4) |

The reconciler (design: extends `execution_health`) correlates a heartbeat gap for host *H* with every `AccountRuntime` whose runtime is placed on *H*.

#### 2. Contain

1. **Fence *H* first (single-writer guarantee).** Before any relocation, mark *H* **draining** at the Connection Broker (no new RemoteApp sessions routed to it) **and** confirm *H*'s per-account automated terminals are unreachable / disabled. This prevents split-brain: if *H* was only network-partitioned and later resurrects, its terminals must not resume trading the same broker accounts that have been relocated. Relocation proceeds **only after** *H* is fenced; if fencing cannot be confirmed, hold at `DEGRADED` and escalate rather than relocate (fail-closed — a possibly-live duplicate terminal is worse than a delayed recovery).
2. **Transition affected runtimes along their *defined* edges** (do **not** blanket-flip everything to `DEGRADED`):
   - Runtimes that were `RUNNING`, `STARTING`, or `AUTHENTICATING` on *H* → `DEGRADED` (`record_transition(runtime, DEGRADED, event_type="FAILURE", reason_code="host_unreachable")`). These are the defined failure edges (`RUNNING → DEGRADED`; `STARTING → DEGRADED`; `AUTHENTICATING`-transient-exhausted `→ DEGRADED`).
   - Runtimes that were mid-`PROVISIONING` on *H* have **no defined `→ DEGRADED` edge** — leave them in their durable state; the idempotent worker resumes their normal forward path (`PROVISIONING → STARTING → AUTHENTICATING → RUNNING`) on a healthy host. Persistent provisioning failure follows the defined `PROVISIONING → retry×N → FAILED` edge.
   - `QUEUED` / `BLOCKED` / `STOPPED` / `NOT_PROVISIONED` runtimes are not on a host — untouched.
   Each transition is persist-then-act: one immutable `RuntimeEvent` written **before** the state mutates (matches `record_transition` ordering). The panel collapses `DEGRADED`/`REPAIRING` → "Degraded (auto-repairing)". User-facing `last_error` carries only the sanitised `reason_code`; raw agent text never leaves the immutable event trail (admin-only).
3. **Reconcile in-flight orders — never replay.** For every orphaned `ExecutionJob` (RUNNING, lease expired) that was a `PLACE_ORDER`/`MODIFY_POSITION` on *H*: reconcile it against the broker's actual Trade/position records and mark it recovered; **never auto-re-run an order-placing job** (a re-run risks a duplicate live order). This mirrors the existing orphaned-PLACE_ORDER reconciler discipline (reconcile against `Trade`, never re-run, alert if unreconcilable).
4. **Open a durable `AlertEvent`** (`WARN`, escalate to `CRITICAL` if the capacity gate fails — §3, or if any orphan cannot be reconciled). Delivery cascades Telegram → webhook → persist-only `SKIPPED`; delivery never raises.

#### 3. CAPACITY CHECK (gate — do this **before** re-materialising)

Recovery relocates the displaced runtimes of *H*. Confirm the *remaining* pool can seat them. **The check is two-dimensional** — a displaced unit is an **account-runtime** (per-account), while the binding density ceiling is expressed in **interactive users**; the two must not be compared directly. Both dimensions are **ESTIMATE (§3) — raise only with Phase-4 evidence.**

Per-RDSH budget, 16 GB RDSH (all **ESTIMATE**):

```
OS + RDS roles                          ≈ 4.0 GB
Automated MT5 terminal (each)           0.15–0.30 GB
Interactive RemoteApp overhead (each)   0.2–0.4 GB / session

Interactive-user ceiling  U_max ≈ 2 / RDSH        (Phase-4 PROOF, conservative — RAM is not the binding limit)
Automated-terminal RAM headroom, worst case:
    16 − 4.0(OS) − U_max×0.4(interactive) ≈ 11.2 GB
    ⇒ RAM-bound terminal ceiling ≈ 11.2 / 0.30 … 11.2 / 0.15 ≈ 37 … 74 terminals
```

At beta the **interactive-user ceiling (~2/RDSH) binds first**; RAM headroom is ample, so the automated-terminal dimension rarely binds — but check both, in consistent units:

```
Per healthy host h (pool \ H):
  U_max      = interactive-user ceiling per RDSH            (ESTIMATE ≈ 2)
  T_max(h)   = RAM-derived automated-terminal ceiling        (ESTIMATE, per budget above)
  u(h), t(h) = interactive users / automated terminals now seated on h

  user_slots_free     = Σ_h max(0, U_max    − u(h))
  terminal_slots_free = Σ_h max(0, T_max(h) − t(h))

Displaced from H:
  disp_users     = distinct users owning a runtime on H
  disp_terminals = count(AccountRuntime on H,
                         state ∈ {PROVISIONING, STARTING, AUTHENTICATING,
                                  RUNNING, DEGRADED, REPAIRING})

PROCEED to §4  ⟺  (disp_users ≤ user_slots_free) AND (disp_terminals ≤ terminal_slots_free)
ELSE           →  BLOCKED — add an RDSH
```

- **If both dimensions fit:** proceed to §4.
- **If not → `BLOCKED`.** Runtimes that cannot be seated stay `QUEUED` / `BLOCKED ("host at capacity")` — **never** over-pack a host past the proven ceiling (fail-closed beats degrading everyone). Escalate the `AlertEvent` to `CRITICAL` and **add an RDSH** — this is a **Red** action (procurement / infra change → Nuno approval; §17). Do **not** raise `U_max` or `T_max` to avoid adding a host without Phase-4 measurement.

#### 4. Recover — idempotent re-materialisation, per account

For **each** displaced runtime, the provisioning worker (Phase 2, DESIGN) drives the durable state machine from its last persisted `AccountRuntime.state` onto a chosen **healthy** host. The path follows the **defined** transitions:

```
Previously-RUNNING (now DEGRADED):
  DEGRADED ─▶ REPAIRING ─(re-materialise on healthy host, idempotent)─▶ RUNNING
                        └─(retries exhausted / bad creds)─▶ FAILED

Mid-first-provision (was PROVISIONING/STARTING/AUTHENTICATING):
  resume normal forward path  PROVISIONING ─▶ STARTING ─▶ AUTHENTICATING ─▶ RUNNING
  (STARTING/AUTHENTICATING host-error follows its defined edge into DEGRADED → REPAIRING)
```

Re-materialisation is the **internal work of `REPAIRING`** (or of the resumed forward states) — it is **not** a separate set of durable states, and `REPAIRING → PROVISIONING` is **not** a defined transition. Each step is create-if-absent / overwrite / re-inject / ensure-task, so re-entry from any state converges:

- **create-if-absent** AD identity `guvfx_u_<uid>` (per-USER, reused across the user's accounts).
- **create** per-account dir `D:\GuvFX\users\<uid>\accounts\<id>\mt5\`, NTFS-ACL'd to `guvfx_u_<uid>` (+ SYSTEM/admin) only.
- **decrypt broker creds (Fernet) at injection time only**, transported over the authenticated agent/WinRM (TLS) channel, written **only** into that ACL'd dir. No shared handoff directory (this is what structurally eliminates the C16 shared-handoff exposure that still exists on the legacy box). The worker handles ciphertext→plaintext transiently inside the isolated runtime; **no operator ever handles a raw credential** — see Escalation for suspected exposure.
- **ensure** the per-account logon/scheduled task (headless AUTO_DEMO terminal).

Then:

- **Nothing is reconstructed from the dead host.** Source of truth is durable `AccountRuntime` state + Fernet-encrypted creds in the GuvFX DB. Positions are broker-side and untouched.
- **Bounded retries** per fail-able transition (`attempt`, `last_error`, `next_retry_at`; exponential backoff — **N and the schedule are OPEN / ESTIMATE**, not numerically fixed in the design; source from the Phase-2 worker or fix by ADR). Exhaustion → `FAILED (sanitised reason)` with a Retry affordance (`FAILED → QUEUED`); transient auth failure → `DEGRADED`; **bad creds → `FAILED` immediately** (no retry storm).
- **Re-point routing atomically, after fencing.** Update each `TradingAccount`'s runtime endpoint (`Mt5Instance`) to the new host as a single-writer switch (old endpoint retired, new endpoint live). `_get_user_mt5_instance` (Phase 0, fail-closed, MEASURED) then resolves the user to *their own* relocated runtime or `None` — never a shared or other-tenant box. Ingest worker / bridge follow the new endpoint only. Because all order flow targets the single re-pointed endpoint (and *H* is fenced), a resurrected *H* cannot receive new order jobs.

#### 5. Verify (per account, evidence-first)

| Check | MEASURED source | PASS condition |
|---|---|---|
| Runtime up | `AccountRuntime.state` | `== RUNNING` **exactly** (not the collapsed panel state) |
| Terminal hosted | Account Status panel `hosted_terminal` | `"RUNNING"` **only** when `runtime.state == RUNNING` exactly |
| Logged in | runtime health probe (DESIGN) | terminal logged-in, heartbeat fresh (`age < interval × 2.5`) |
| Routing correct | `_get_user_mt5_instance(user, account)` (MEASURED, fail-closed) | resolves to the **new host**; not `None`, not shared |
| No cross-tenant leak | user-scoped querysets + NTFS ACL (Phase 0) | user sees only their own runtimes |
| Orphans reconciled | `ExecutionJob.recovered` / broker Trade records | every orphaned order job reconciled (recovered), none replayed |
| Interactive reconnect | Connection Broker | user's RemoteApp session reconnects to their own collection only |

Mark the recovery **PASS only when these actually ran and met** (evidence rule). If any could not run, record `PARTIAL`/`FAIL` with the reason in the `RuntimeEvent` trail and the `AlertEvent`, and state what was **not** covered (e.g. "interactive reconnect not exercised — no user online at recovery time"; "logged-in check is a DESIGN probe, not yet deployed").

#### 6. Interactive users

Users reconnect through the **Connection Broker**, which reconnects existing/relocated RemoteApp sessions and routes each user only to their own collection (defence layer 2). No user action beyond re-opening their session. AUTO_DEMO terminals were rebuilt headless and keep trading independent of any interactive session.

---

### SEV-1 — The infra VM (DC + RDCB + RDGW + Licensing) fails

At beta these roles are **collapsed onto one Windows Server VM (§2)** — a **single point of failure**, a known and accepted beta risk, not a defect; the HA remedy is post-beta (§17). The authoritative GuvFX DB is **not** on this VM (it is on the Ubuntu VPS), so DB-held runtime state and Fernet creds survive an infra-VM loss.

#### What breaks vs. what survives

| During infra-VM outage | Status |
|---|---|
| New RemoteApp sessions / logins | **BLOCKED** — no DC (identity), no RDCB (routing), no RDGW (TLS tunnel), no Licensing (CAL issuance) |
| New provisioning / onboarding | **BLOCKED** (also structurally closed — onboarding gate off) |
| **Already-running automated terminals (AUTO_DEMO)** | **CONTINUE — with caveat.** Processes already running under `guvfx_u_<uid>` keep trading without a live login session. **But** any terminal that crashes or must relaunch mid-outage may fail to restart, because a fresh logon of a domain identity needs the DC. So continuity is guaranteed only for terminals already up; it is **not** guaranteed across a mid-outage restart. |
| Broker positions | **Untouched** (broker-side) |
| Authoritative GuvFX DB (runtime state + Fernet creds) | **Unaffected** (Ubuntu VPS, separate host) |

So an infra-VM outage suspends *access and change*, not *automated execution already in flight* — subject to the DC-relaunch caveat.

#### Recovery

1. **Detect** — infra-host heartbeat gap + RDCB/RDGW unreachable + login failures across *all* users simultaneously (the "all users at once" signature distinguishes SEV-1 from SEV-2). Open an `AlertEvent` at `CRITICAL` and page Nuno (control-plane events are Red).
2. **Restore the infra VM from its most recent snapshot** (DC + RDCB + RDGW + Licensing state). **This assumes a scheduled, current infra-VM snapshot exists** — that snapshot regime is **not yet in place** (the infra VM does not yet exist) and is a **Phase-2 prerequisite** (§ backup-gap). Snapshot restore is the *only* designed beta path for the control plane — there is no standby. Any snapshot restore is a **Red** action requiring Nuno's approval.
3. **Reconcile** — on infra restore, the reconciler re-verifies every `AccountRuntime` against reality on the RDSH pool (reading the DB, which was never lost): runtimes that kept running settle back to `RUNNING`; any that drifted follow the SEV-2 §4 procedure per account (`DEGRADED → REPAIRING → RUNNING`, or resume forward path). Orphaned order jobs are reconciled against broker Trades, **never replayed**.
4. **Verify** — DC resolves identities; RDCB routes; RDGW tunnels 443; Licensing issues Per-User CALs; then re-run the SEV-2 §5 per-account verification for a sample across tenants.

#### Backup-gap caveat (estate RED gap — MEASURED)

The estate's standing RED gap is **no automated DB backup** (newest DB backup was ~4.5 months stale at last audit; KNOWN_ISSUES / operations estate). The GuvFX DB holds the authoritative `AccountRuntime` state **and** the Fernet-encrypted broker creds that make runtimes rebuildable. Two *distinct* prerequisites, both **OPEN**, must be closed before the pool carries real beta users:

- **(a) A current infra-VM snapshot** — required for SEV-1 control-plane restore. Not yet in place.
- **(b) A recoverable GuvFX DB** — a general estate prerequisite (BACKUP-RECOVERY-BASELINE). It is not consumed by an infra-VM-only outage (the DB is on a different host), but a DB-host loss would make runtime state and creds unreconstructable pool-wide.

Both are **prerequisites, not steps**. Flag as OPEN / not-yet-covered.

#### Post-beta HA path (§17 — design, not scheduled, unauthorised)

- **Redundant Connection Broker** (RDCB HA pair, shared SQL config DB) — removes routing SPOF.
- **Secondary Domain Controller** — removes identity SPOF (and the DC-relaunch caveat above).
- **RD Gateway farm** behind the existing Traefik/TLS edge — removes external-access SPOF.
- **Licensing** on a surviving infra node.

App-layer fan-out is already O(N accounts) and host-agnostic, so the ceiling is RDS-infra HA + host capacity, not GuvFX code. None of this is procured or authorised — it requires Nuno's approval of §17/§20.

---

### Escalation

| Condition | Action | Gate |
|---|---|---|
| SEV-2, both capacity dimensions PASS | On-call recovers via §2–§5; INFO/WARN alert | Green — no human gate |
| SEV-2, fencing of *H* cannot be confirmed | Hold displaced runtimes at `DEGRADED`; do **not** relocate; escalate | Amber — avoid split-brain double execution |
| SEV-2, capacity **BLOCKED** | Escalate `CRITICAL` → **add RDSH** (procurement/infra change) | **Red — Nuno approval** |
| Orphaned order job cannot be reconciled against broker | Escalate `CRITICAL`; human triage; **never replay** the order | Amber — trade-safety |
| Repeated per-account `FAILED` after retry exhaustion | Human triage of sanitised `RuntimeEvent` FAILURE trail; no auto-remediation beyond the designed state machine | Amber — advisory only |
| SEV-1 (infra VM) | **Immediate `CRITICAL` + Nuno**; snapshot restore | **Red — control-plane SPOF, infra change** |
| Suspected credential exposure during recovery | **Stop, do not commit, report redacted** (security rule); rotate the Fernet-injected creds | Red — security |

Remediation stays **advisory** (`RecoveryRecommendation`) with manual/audited execution (`RecoveryAction`); Phase-1 discipline is **detect → recommend → human act → audit**, never auto-execute, and never auto-place/close a trade.

### Audit

- Every state change is an immutable, append-only `RuntimeEvent` (app-layer refusal + DB BEFORE-UPDATE trigger, migration `0005_runtimeevent_immutable_trigger` — MEASURED) with `from_state / to_state / reason_code / sanitised detail / timestamp` → the forensic timeline of the recovery.
- Admin visibility via `AdminBetaEstateView` (`/api/admin/beta-estate/`, `IsSuperOrOpsAdmin`, read-only, capped, MEASURED): per-account `runtime_state`, sanitised `runtime_last_error`, last-5 `FAILURE` events, `account_number` only — **never** a decrypted credential.
- User-facing truth via the Account Status panel (Phase 0): reads durable `AccountRuntime.state` only, `hosted_terminal == "RUNNING"` only when `state == RUNNING` exactly, `terminal_provisioning_available: False` always — the UI can never infer a live terminal from a green overall.

### SLO linkage

**No SLO model exists yet (OPEN — SLOs would be net-new).** These are *proposed* targets to be **measured and ratified in Phase 4**, reusing the existing threshold vocabulary (age vs `expected_interval × grace_multiplier`; `lease_ttl=300`) rather than inventing a new one. Do not assert them as proven.

| SLI | Derived from | Proposed target (**ESTIMATE — prove in Phase 4**) |
|---|---|---|
| Host-failure detection latency | new RDSH host-heartbeat gap = `interval × 2.5` (multiplier MEASURED; **interval not yet defined**) | ≤ detection bound once the host-heartbeat interval is fixed (e.g. ≤ ~150 s if interval = 60 s — the 60 s is an assumption, not the measured worker interval) |
| Per-account recovery time (SEV-2) | Δ between the `DEGRADED` and the next `RUNNING` `RuntimeEvent` | e.g. ≤ 10 min/account, capacity permitting |
| Automated-execution continuity during SEV-1 | `ExecutionJob` success on the (unaffected) Ubuntu backend during a Windows-infra-VM outage | best-effort, bounded by outage duration for any terminal needing a DC-dependent relaunch (**no crisp % is defensible** — see caveat) |
| Control-plane restore time (SEV-1) | infra-VM snapshot restore duration | bounded by the (not-yet-established) snapshot RPO/RTO |
| Silent-loss guard | `all_accounted = (pending_stuck == 0)` per source, settle window 300 s (MEASURED pattern) | 0 unaccounted account-runtimes post-recovery |

**Availability caveat.** With the infra VM as a beta SPOF, the DC-relaunch gap, and the DB-backup gap all open, a headline pool-availability SLO is **not yet defensible**. Publishing one requires (a) the HA path (§17) or an accepted single-VM RTO, and (b) the estate backup baseline closed. The earlier "≥99% execution continuity during SEV-1" is withdrawn — it both overstated measurability and contradicted the "unaffected" framing given the DC-relaunch caveat.

### Not covered / must be proven in Phase 4

- **Density ceiling (~2 users/RDSH), the RAM budget, and the RAM-derived terminal ceiling** are ESTIMATES — load-test before relying on the two-dimensional capacity check.
- **The per-RDSH host-heartbeat source and its interval do not exist yet** — the detection SLI cannot be quantified until they are built (only the `2.5` multiplier is measured).
- **Retry count N and backoff schedule** are unspecified in the design — source from the Phase-2 worker or fix by ADR.
- **Fencing / split-brain protection** for a resurrected RDSH, and **orphan-order reconcile-never-replay**, are designed here but not exercised against a live pool — both are trade-safety critical.
- **SEV-1 recovery assumes a current infra-VM snapshot** (not yet in place) **and** a recoverable GuvFX DB (BACKUP-RECOVERY-BASELINE open) — blocking prerequisites, not steps.
- **Interactive-reconnect and the runtime health probe** are designed, not exercised/deployed.
- **Two heartbeat-staleness formulas coexist in code** (`heartbeat.py` `×2.5` binary vs `operations_summary.py` `×2.0`/`×4.0`) — this runbook uses the `×2.5` detection form; the divergence must be reconciled by the packet owner before an SLO is ratified.

### Runbook — Customer support triage

> **Status of this runbook — read first.** This is a **design/documentation deliverable**. It describes how support *will* triage tickets once the Beta Onboarding V1 pool (Option A) is live. **It is not yet an operational procedure and most of it cannot be executed today.**
>
> - Provisioning is at **Phase 0**: `AccountRuntime` / `RuntimeEvent` / `RuntimeState` **record state only; they do not drive provisioning.** No provisioning worker, `ProvisioningJob`, Windows agent, AD identity, RemoteApp, or Guacamole per-account mapping is deployed.
> - **Therefore the only `RuntimeState` that appears in production today is `NOT_PROVISIONED`.** Every beta account reads `NOT_PROVISIONED`, `can_deploy_automation=False` on the `beta` plan, and the onboarding gate (`BETA_ONBOARDING_ENABLED`) is **closed by default**. Customer onboarding stays **CLOSED** until Phase-4 isolation gates pass. No email-verify bypass.
> - **Every state row, `Retry`, `Resume`, capacity-clear, identity re-enable, and host-recovery action below is `DESIGN — not live`.** Do **not** attempt them today — the controls do not exist. They are documented so the procedure is ready to review, not to run.
> - Nuno's existing production box, MT5 runtimes, broker accounts, and AUTO_DEMO operation are **untouched** and are **not** part of the beta pool. "AUTO_DEMO" below refers to a *beta-pool* per-account automated terminal (design), never to Nuno's live operation.

---

#### 0. What support can and cannot see (grounded in shipped Phase 0 code)

| Support **can** see | Support **cannot** see |
|---|---|
| Broker **account numbers** (non-secret identifiers) — via `AdminBetaEstateView` (`/api/admin/beta-estate/`) | **Decrypted broker credentials** — Fernet-encrypted at rest (`GUVFX_FERNET_KEY`), never returned by any admin surface |
| Per-account **`runtime_state`** (`AccountRuntime.state`; `NOT_PROVISIONED` if no row — the only value in production today) | **Windows identity passwords** (`guvfx_u_<uid>`) — provisioner-generated, Fernet-stored, never surfaced |
| **Sanitised** `runtime_last_error` and the **last 5** `FAILURE` `RuntimeEvent`s (`reason_code` + timestamp only) | **Raw agent/bridge exception strings** — sanitised at the boundary before they reach the DB or API |
| Per-runtime Account Status panel (§1), `is_beta`, `account_count`, `max_accounts` (`min(10, max_trading_accounts)`) | **TOTP secrets** (`TwoFactorSecret.secret_enc`) — same crypto boundary as broker creds |

**Canonical rule.** `AdminBetaEstateView` is the model for every support-facing view — **read-only, capped (`[:200]` users), sanitised, non-secret fields only, `IsSuperOrOpsAdmin`-gated**, documented in code as **"NEVER exposes decrypted broker credentials."** If any support tool ever appears to show a secret (broker password, Windows password, TOTP seed, token), **stop, do not copy it, treat it as a P1 secret-exposure incident, and escalate to L3 immediately** so it can be rotated (security rule: stop-and-report on suspected leak).

#### 0.1 Escalation tiers

| Tier | Owns | Typical actions | Hand up when |
|---|---|---|---|
| **L1 — Triage** | First response, symptom capture, reading Account Status + beta-estate view | Identify `RuntimeState`, confirm gate behaviour, answer "is this expected?", route all credential resets to the password runbook | Anything requiring a state change, a `Retry`/`Resume`, or a runtime touch |
| **L2 — Ops / Runtime** *(runtime-touching actions are `DESIGN — not live`)* | Runtime lifecycle, provisioning failures, host capacity, entitlements | Read `RuntimeEvent` trail, read ops rollups, **ack** `AlertEvent`s (`alerts/<pk>/acknowledge/`). **When Phase 2+ is live:** drive one-shot `Retry` on `FAILED`, clear `BLOCKED` capacity, `Resume` a `STOPPED` runtime | Suspected code defect, cross-tenant leak, non-retryable/host-level failure, or a `Retry` that re-fails with the same `reason_code` |
| **L3 — Engineering / On-call** | Code, migrations, infra, isolation | Root-cause defects, host recovery, **any P1 isolation or secret-exposure incident** | — (top tier) |

**Evidence capture (every ticket, all tiers).** Record: `user_id`, `account_id`, `account_number` (non-secret), observed `runtime_state`, sanitised `last_error`, the relevant `RuntimeEvent` `reason_code`s + timestamps, any `ExecutionJob` id/status, and the exact UTC time window. **Never** paste a raw secret, raw agent string, or another tenant's data into a ticket. Redact by file/path/category per the security rule.

**Audit of support actions.** Every runtime state change is written as an **immutable, append-only `RuntimeEvent`** (app-layer `save()`/`delete()` refuse mutation; DB BEFORE-UPDATE trigger, migration 0005). When live, an L2 `Retry`/`Resume` produces a durable `FAILED→QUEUED` / `STOPPED→STARTING` event — support must also record **who** actioned it and the **ticket id** in the ticket. No support action is fire-and-forget.

---

#### 1. "My terminal isn't running" → read the Account Status `RuntimeState`

**Check (grounded surface).** Open the account's **Account Status panel** (`build_account_status(account)` → 9 ordered stages) and the **`beta-estate`** row. The authoritative field is `AccountRuntime.state`. The user-facing label comes from the `USER_FACING` collapse; the `mt5_runtime` stage shows the exact state; the `hosted_terminal` stage reads **"RUNNING" only if `state == RUNNING` exactly** (not derived from the collapse).

> **Today (current build):** every account reads `NOT_PROVISIONED` — provisioning is not deployed. A "terminal isn't running" ticket right now is **expected**; the correct L1 answer is "provisioning is not yet enabled for beta." The mapping below is the **DESIGN behaviour for when Phase 2+ is live** — do not describe it to a customer as a live capability today.

| `RuntimeState` | Likely cause | L1 action | Escalate to |
|---|---|---|---|
| `NOT_PROVISIONED` | No runtime (Phase 0 baseline — the current production value for every account) | Confirm this is expected in the current build; only if the onboarding gate is open *and* the account should have provisioned, check whether `QUEUED`/`BLOCKED` fired | L2 if it should have provisioned |
| `QUEUED` | Provisioning requested; awaiting worker + host slot | Reassure; normal transient. If stuck, check for `BLOCKED` | L2 if not moving |
| `BLOCKED` | Prerequisite missing: **no host capacity**, **entitlement absent**, or **onboarding gate closed** | Read sanitised reason. Gate-closed / no-entitlement = **expected, not a fault**. Capacity → L2 | L2 (capacity/entitlement) |
| `PROVISIONING` / `STARTING` / `AUTHENTICATING` | Materialising identity/dir/MT5, launching, or logging into broker — in progress | Reassure; show stage from panel | L2 if wedged past bounded retries |
| `RUNNING` | Automated terminal up + logged in — **operational** | If user still reports "not running," suspect interactive-session confusion (§2) or a stale view; a beta-pool AUTO_DEMO terminal stays up across RemoteApp disconnects | — |
| `DEGRADED` / `REPAIRING` | Was running; health check failed → auto-repairing | Reassure — self-healing in progress. Capture `RuntimeEvent`s | L2 if it does not return to `RUNNING` |
| `STOPPING` / `STOPPED` | **Deliberately** stopped (pause/deactivate); runtime exists, terminal not running | Confirm whether user/admin paused it. Valid resume path is `STOPPED —(resume)→ STARTING` | L2 to `Resume` if intended |
| `FAILED` | Terminal, non-retryable failure (e.g. **invalid broker credentials**, **wrong server**) or bounded retries exhausted | Read sanitised reason. **Bad creds → route to §5** (support never handles the secret). The valid recovery is `FAILED —(Retry)→ QUEUED` | L2 to drive Retry / L3 if defect |
| `DEPROVISIONING` / `REMOVED` | Account offboarded; runtime torn down | Expected after offboarding | — |

**Verification & anti-retry-storm rule (when live).** After an L2 `Retry`, **verify** the state advances (`QUEUED → PROVISIONING → …`). The state machine already applies **bounded exponential backoff** per fail-able transition (`attempt` / `next_retry_at` on `AccountRuntime`; exact N and schedule are **not specified in source — an open item, do not quote a number**). **Do not hand-loop `Retry`** — a manual `Retry` is **one-shot**; if it re-fails with the same `reason_code`, escalate to L3 instead of retrying again (retry storms have previously self-inflicted broker/agent throttling).

**Sanitised reasons.** `last_error` / `reason_code` are user-safe strings (*invalid broker credentials*, *wrong server*, *host at capacity*, *broker unreachable*). The **raw** agent text is admin-only, lives in the immutable `RuntimeEvent.detail`, and is **never** surfaced to the customer.

---

#### 2. "I can't log in" → disambiguate the three distinct logins

There are **three separate authentication surfaces**. Identify which one before acting — they share nothing, and **support never types, reads, or resets a raw password/credential for any of them.**

| Which login | Symptom / surface | Check | Action / Escalation |
|---|---|---|---|
| **GuvFX app auth** (live) | Can't sign into guvfx.com / dashboard; 401s | Cookie JWT (`CookieJWTAuthentication`); CSRF `/api/auth/cookie/csrf/`, refresh `/api/auth/cookie/refresh/`. Confirm an email-verified account exists | Standard app password reset → **§5**. Note: email-verify send is currently broken **and onboarding is closed by design** — do **not** bypass the gate to "unblock" |
| **Windows identity** `guvfx_u_<uid>` **(DESIGN — not live)** | Can't launch RemoteApp / MT5 desktop | Per-user AD identity, `GuvFX-BetaUsers`, RemoteApp-only. Password is provisioner-generated + Fernet-stored — **support never sees or resets it** | If disabled/suspended: **when live**, L2 re-enables via the provisioning control-plane (fail-closed: disabled = no launch). Never disclose the password |
| **Broker login** (MT5 auto-login) **(DESIGN — not live)** | Terminal reaches `AUTHENTICATING` then `FAILED` with *invalid broker credentials* / *wrong server* | Runtime state + sanitised reason in Account Status | User re-enters broker creds via the app (encrypted at rest); **support never handles the raw secret** → **§5**. Then one-shot `Retry` (→ `QUEUED`) |

**Rule.** All credential resets route to the password runbook (§5). Support confirms the *state*, never the *value*.

---

#### 3. "My trade didn't execute" → walk the pipeline gates in order

**Most non-executions are correct gate behaviour, not defects.** Forensics repeatedly show "N of M signals executed" turning out to be a gate (drawdown, entitlement, exposure) acting *correctly*. Confirm expected behaviour **before** suspecting a fault. All checks below are **read-only — never mutate.**

| # | Gate | Where to check | Blocking = expected when… |
|---|---|---|---|
| 1 | **Onboarding gate** | `beta_onboarding_open()` (`BETA_ONBOARDING_ENABLED`, default **closed**) — blocks `account_connected` / `strategy_assigned` for non-staff | Gate closed (current default). No automation for non-staff. **Expected.** |
| 2 | **Entitlement — `can_deploy_automation`** | `resolve_entitlements(sub)`; **`False` for the `beta` plan** — a fail-closed EXECUTION-authorization block, **independent** of provisioning | Beta user has automation off (current design). Gates `create_open_trade_job` / `create_place_order_job`. **Expected — do NOT propose flipping this to `True` until Phase 4.** |
| 3 | **Composite readiness** (read-only report) | `check_onboarding_permits_execution(user)`: `onboarding_completed` + `has_active_account` + `has_live_assignment` (stage `LIVE`) + `entitlement_valid` + `terminal_node_valid` | Any check false → not permitted. The report names which check failed. |
| 4 | **Daily-drawdown** | Per-account drawdown gate (durably marks plans `PROMOTION_REJECTED` when tripped) | Account hit its daily loss limit → subsequent signals rejected. **Expected.** Confirm the limit and the trip event. |
| 5 | **Per-account routing** | `_get_user_mt5_instance` (Phase 0, fail-closed) — resolves to the user's **own** per-account runtime, or **None** with a clear message; **never** a shared/other-user box | Today, with no `RUNNING` runtime, this **always** resolves to `None` → no order. Ties back to §1. |
| 6 | **Bridge** | Per-account bridge / Windows agent `/mt5/order_check` (order **check** — **no order is placed**); `ExecutionJob` status + `EXECUTION_LEASE_TTL_SECONDS=300` orphan detection | Bridge unreachable or job stuck/orphaned (RUNNING with expired lease). L2/L3 territory. |

**Support never places or closes a trade, and no step here does so.** Gate 6 is an order *check* only; the pipeline's only order-placing path is server-side and gated by (1)/(2), which are closed for beta.

**Existing read-only forensics to reference:**
- **`OperationsSummaryView`** (`/api/reliability/operations-summary/`) → **`_signal_execution_block()`** classifies every `SignalExecutionPlan` created *today* per source into **executed / rejected (reason breakdown) / pending**, with a 300s `settle` window and `all_accounted = (pending_stuck == 0)` as the "no silent signal loss" assertion. **`ti_signals` and `wayond` are never combined.** First place to look for "which gate rejected it, and why." Fail-safe: the endpoint returns a degraded shape rather than 500-ing.
- **Account Status `last_execution` stage** — HEALTHY iff the most recent `ExecutionJob.status == "SUCCESS"`; a past **FAILED / PENDING / RUNNING / other** is surfaced as **WARNING** (deliberately **not** escalated to FAILED, so a stale past failure doesn't over-escalate the overall).
- **`ExecutionJob`** — `status`, `error_message`, `created_at`/`started_at`/`finished_at`, `lease_expires_at`, `recovered`/`recovery_reason` for claim latency, duration, and stuck-job detection.

**Triage stance.** If the signal appears under `rejected` with a known reason (drawdown, entitlement, exposure, gate-closed), it is **correct gate behaviour** — explain it, capture the reason as evidence, close. Only a signal that is `pending_stuck` (breaks `all_accounted`) or a bridge/job orphan is a genuine execution fault → L2/L3.

---

#### 4. "I see wrong / someone else's data" → **P1 ISOLATION incident**

**Treat any suspected cross-tenant data exposure as P1 and escalate immediately.** This overrides normal triage flow and is fail-closed: when in doubt, escalate.

1. **Do not reproduce or forward the leaked data.** Never paste another tenant's account numbers, positions, or identifiers into the ticket, chat, or logs.
2. **Capture minimal, redacted evidence:** reporting `user_id`, the surface/URL, timestamp, and a **category-only** description (data *type*, not values). Note whether it was a user-facing view or an admin view.
3. **Escalate to L3 on-call immediately; flag L2 ops in parallel.** This is Red-tier — it touches the isolation guarantee that gates the entire beta.
4. **Verify tenant scoping (L2/L3).** Ownership chain: `User → Broker Account → MT5 Runtime → Strategies → Positions → Notifications`, with `guvfx_u_<uid>` per user. Defence-in-depth layers, **with current status**:
   - **Live today (Phase 0 ✓):** app-layer fail-closed `_get_user_mt5_instance`; user-scoped querysets; tenant-scoped alerts/health/ops; `AdminBetaEstateView` scoping and non-secret projection.
   - **DESIGN — not live (Phase 2+):** non-admin Windows identities + NTFS ACLs; RDS Connection Broker per-collection routing; per-user Guacamole grants; RD Gateway TLS-only, hosts not public.
5. **Never expose one tenant to another to "prove" or "compare."** No compiling data across tenants to investigate (security/privacy rule).

> Because onboarding is single-tenant / **CLOSED** today, a genuine cross-tenant production leak should be structurally impossible — which makes any such report **more** urgent to investigate as either a real defect or a misread. **Do not dismiss it.**

---

#### 5. Credential reset requests → route to the password runbook

**Support NEVER handles raw secrets** — not broker passwords, not the Windows identity password, not TOTP secrets, not app passwords.

- **All resets route to the dedicated password runbook / password-manager flow.** The customer supplies/updates the secret through the encrypted app surface (`trading/crypto.py` `encrypt_password`, `GUVFX_FERNET_KEY`) — never through support.
- **Broker creds:** user re-enters in-app → stored Fernet-encrypted → then one-shot `Retry` the runtime (§1, **DESIGN — not live**). Support confirms the *state* (`FAILED — invalid broker credentials`), not the value.
- **Windows identity `guvfx_u_<uid>` (DESIGN — not live):** provisioner-owned, Fernet-stored. Reset = **when live**, L2 re-runs the provisioning control-plane action; the RemoteApp connection is rebuilt from it; the password is disclosed to no one.
- **App / 2FA:** standard app reset flow; 2FA is optional and never blocks. Email-verify send is currently broken **and onboarding is closed** — **do not bypass** the gate.

If a customer pastes a secret into a ticket: **redact it, do not act on it, note the exposure, and report per the security rule** (stop-and-report so it can be rotated).

---

#### 6. At-a-glance: `RuntimeState` → likely cause → action

> **All rows except `NOT_PROVISIONED` are `DESIGN — not live`.** `NOT_PROVISIONED` is the only state in production today; the "First action" for every other row assumes Phase 2+ is deployed.

| `RuntimeState` | User-facing label | Likely cause | First action | Tier |
|---|---|---|---|---|
| `NOT_PROVISIONED` | Not provisioned | No runtime (Phase 0 default — **current production value**) | Confirm expected in current build | L1 |
| `QUEUED` | Queued | Awaiting worker/host slot | Reassure; watch for `BLOCKED` | L1 → L2 |
| `BLOCKED` | Blocked (reason) | No capacity / no entitlement / gate closed | Read reason; gate/entitlement = expected | L1 → L2 (capacity) |
| `PROVISIONING` | Provisioning… | Materialising runtime | Show stage | L1 → L2 if wedged |
| `STARTING` | Provisioning… | Launching terminal | Reassure | L1 → L2 if wedged |
| `AUTHENTICATING` | Provisioning… | MT5 logging into broker | Reassure; watch for creds fail | L1 → L2 |
| `RUNNING` | Running | Operational | Suspect session confusion / stale view | L1 |
| `DEGRADED` | Degraded (auto-repairing) | Health check failed | Reassure — self-healing | L1 → L2 if stuck |
| `REPAIRING` | Degraded (auto-repairing) | Re-materialise/restart in progress | Capture `RuntimeEvent`s | L1 → L2 |
| `STOPPING` | Stopped | Deliberate stop in progress | Confirm who paused | L1 → L2 |
| `STOPPED` | Stopped | Paused/deactivated; runtime intact | `Resume` if intended (→ `STARTING`) | L1 → L2 |
| `FAILED` | Failed (reason) — Retry | Non-retryable / retries exhausted (bad creds, wrong server) | Read reason; bad creds → §5; one-shot **Retry** (→ `QUEUED`) | L2 / L3 if defect |
| `DEPROVISIONING` | Removing… | Offboarding in progress | Expected | L1 |
| `REMOVED` | Removed | Account offboarded | Expected | L1 |

---

**Assumptions & limitations (evidence rule).**

- **MEASURED / grounded in shipped Phase 0 code:** the 14 `RuntimeState` values and the `USER_FACING` collapse; `AdminBetaEstateView` fields + `[:200]` cap + `IsSuperOrOpsAdmin` gate + "never decrypted credentials" guarantee; `build_account_status` 9-stage panel with the `hosted_terminal == "RUNNING"`-exact and `last_execution` WARNING-not-FAILED rules; `RuntimeEvent` immutability (app + DB trigger, migration 0005); `beta_onboarding_open()` default-closed gate at `mark_account_connected`/`mark_strategy_assigned`; `can_deploy_automation=False` on the `beta` plan; `_get_user_mt5_instance` fail-closed resolution; `check_onboarding_permits_execution` composite; `_signal_execution_block` per-source classification with a 300s settle window and `all_accounted = (pending_stuck == 0)`; `EXECUTION_LEASE_TTL_SECONDS=300`; alert ack endpoint `alerts/<pk>/acknowledge/`.
- **DESIGN — not live (Phase 2+, gated on Nuno's approval of §19/§20):** the async provisioning workflow (`ProvisioningJob`, provisioning worker, WinRM/agent drive), the AD identity / RemoteApp / Guacamole per-account mapping, host-failure re-materialisation, `Retry`/`Resume`/capacity-clear controls, and **any `RuntimeState` other than `NOT_PROVISIONED` appearing in production.** Retry count `N` and backoff schedule (`retry×N(backoff)`) are **named but not numerically specified** in source — an **open item**, not a value support may quote.
- **NOT covered / to be proven in Phase 4:** live density and SLO/SLI numbers (density is a Phase-4 proof, not an assumption; **no SLO/SLI model exists yet** — none is asserted here, and support must not quote target numbers). The two coexisting heartbeat-staleness formulas — `HEARTBEAT_GRACE_MULTIPLIER=2.5` (binary OK/FAILED in `heartbeat.py`) vs `_STALE_FACTOR=2.0` / `_CRITICAL_FACTOR=4.0` (two-tier in `operations_summary.py`) — are **not reconciled**; support metrics must not assume one canonical threshold, and reconciliation is owned by the packet owner, not this runbook. End-to-end cross-tenant isolation under 5-user load is unproven. No `/operations` frontend page exists — support works from the backend surfaces named above. This section contains **no capacity/RAM/CPU arithmetic**; all density and cost math is deferred to the capacity/SLO section and must not be inferred from here.

---

## C. Service-level objectives (targets, pending Phase-4 measurement)

> **Status — DESIGN TARGETS, not observed SLAs.** Every number is an *estimate*, not a *measurement*. Per §14.4 the Phase-0 `AccountRuntime` records state only; every runtime is `NOT_PROVISIONED` today, so there is **zero** historical provisioning telemetry to fit these targets against. Each SLO names the exact durable signal it will be computed from once Phase 2+ ships, and each target must be **validated or corrected by Phase-4 measurement** before onboarding opens. Nothing here authorises procurement, deployment, or flipping `BETA_ONBOARDING_ENABLED` / `can_deploy_automation`.
>
> These SLOs **extend** §3's density rule (*"Density is a Phase-4 proof, not an assumption — raise only with evidence"*); they do not restate capacity as proven.
>
> **All SLI computation is read-only and fail-safe.** Every SLI is reconstructed after the fact from the immutable durable trail by a read path that must never mutate state, never place/modify/close an order, never touch Nuno's untouched production box, and never 500 (following the `operations_summary.py` fail-safe discipline). No active runtime probing and **no automated remediation** — remediation stays *detect → recommend → human/manual act → audit* (advisory-only `RecoveryRecommendation`/`RecoveryAction`). "Freeze/hold on budget exhaustion" is **design-doc policy only**.
>
> **Vocabulary reuse.** Reuses `reliability/constants.py`: `EXECUTION_LEASE_TTL_SECONDS = 300`, `HEARTBEAT_GRACE_MULTIPLIER = 2.5`, `HEARTBEAT_EXPECTED_INTERVAL = 60 s`, `SNAPSHOT_STALE_SECONDS = 300`. Alerting reuses the `AlertEvent` cascade (Telegram → webhook → persist-only `SKIPPED`); no PagerDuty/Opsgenie is used or proposed. **"Page"** = `CRITICAL` `AlertEvent`; **"ticket"** = `WARN` `AlertEvent` — same delivery path, differing only in severity.
>
> **Open dependencies (flag, do not assume).** `[OPEN — retry policy unspecified]`: §8.2 names `retry×N(backoff)` but does not fix N or the schedule, and no worker module exists to source them; SLO-1's tail and SLO-2's repair ceiling depend on it. `[OPEN — reason_code taxonomy not frozen]`: the user-vs-system split that scopes every error budget needs a curated `reason_code → {user, system, capacity, broker}` map, defined alongside the Phase-2 worker. `[OPEN — ProvisioningJob lease TTL]`: `ProvisioningJob` is unshipped; its lease TTL is assumed to mirror `EXECUTION_LEASE_TTL_SECONDS=300` but is not yet defined. `[OPEN — per-runtime heartbeat]`: no per-runtime `Heartbeat` source exists (the named sources are `scheduler_*/ingest_worker/validate_worker`); Account-Status stage 8 is `NOT_CONFIGURED` (§14.7). `[OPEN — host-death detection signal]`: SLO-2b's `t_host_failure` anchor (the reconciler's first host-dead `RuntimeEvent` / `ComponentHealth → DOWN`) has **no telemetry source today** — no per-RDSH host-heartbeat exists (the named heartbeat sources `scheduler_*/ingest_worker/validate_worker` contain no RDSH host component); the recovery-time SLI cannot be computed until a host-death detection signal is built in Phase 2. `[OPEN — launch-outcome event]` and `[OPEN — cred-propagation re-auth path]` per SLO-5.
>
> **Two coexisting staleness formulas (flag, do not silently reconcile).** `heartbeat.py`: `age > interval × 2.5` → binary OK/FAILED. `operations_summary.py`: two-tier `age > interval × 2.0` (WARNING) / `× 4.0` (CRITICAL). SLO-3 **commits to the binary `heartbeat.py` formula** for the "is this runtime up" determination and says so; the divergence is left for the packet owner to resolve.

### Common definitions

- **Substrate = `RuntimeEvent`** (§14.5): immutable, append-only, ordered by `id`, one row per transition with `from_state`, `to_state`, `event_type ∈ {TRANSITION, RETRY, FAILURE}`, `reason_code`, `created_at`. Latency A→B = `created_at(to_state=B) − created_at(to_state=A)` over that runtime's stream. Auditable after the fact; no separate metrics store required or proposed.
- **`ProvisioningJob` lifecycle** (planned, §8.3 — *not yet shipped*): mirrors `ExecutionJob` (`created_at`/`started_at`/`finished_at`/`lease_expires_at`). Orphan/stuck detection reuses RX-2E (`lease_expires_at` past + `status=RUNNING`). Because the worker drives **one durable step per iteration** (§8.3), the lease is renewed per step; an "orphan" is a *single step* exceeding its lease, not a long-but-healthy multi-step provision.
- **"SHOULD be RUNNING"** iff owning `TradingAccount.is_active`, an active `AUTO_DEMO StrategyAssignment` exists, and the runtime is not in a deliberate `STOPPING/STOPPED/DEPROVISIONING/REMOVED` state nor `FAILED` for a user-caused reason. Keys off the §1 ownership chain; this is the availability denominator.
- **User- vs system-caused** is read from `RuntimeEvent.reason_code`. User-caused terminal failures (`invalid broker credentials`, `wrong server`) are **excluded** from every error budget — the platform did not fail. System-caused (`host at capacity`, `broker unreachable`, host death, agent/WinRM error) count. Enforced by the `[OPEN]` taxonomy above.

---

### SLO-1 — Provisioning latency (enqueue → RUNNING)

**Definition.** Wall-clock from a request becoming actionable to the automated terminal being live and broker-authenticated. Start = `NOT_PROVISIONED → QUEUED` (= `ProvisioningJob.created_at`, op `PROVISION`). End = `AUTHENTICATING → RUNNING`. Spans the §8.2 happy path `QUEUED → PROVISIONING → STARTING → AUTHENTICATING → RUNNING`.

**SLI.** Per success, `latency = created_at(RUNNING) − created_at(QUEUED)` from that runtime's `RuntimeEvent` stream. Report p50/p95/p99 over the window. Cross-check `ProvisioningJob.finished_at − created_at`; a systematic gap beyond one poll interval is itself a signal.

**Targets (ESTIMATE — Phase-4 to validate).**

| Percentile | Beta target | Steady-state target |
|---|---|---|
| p50 | ≤ 90 s | ≤ 60 s |
| p95 | ≤ 4 min | ≤ 2 min |
| p99 | ≤ 8 min | ≤ 4 min |

**Window & sample.** Rolling 7-day. Minimum **20 successful provisions** before p50/p95 is treated as meaningful (else report "insufficient sample"). **p99 requires ≥ 100 samples** to be stable — below that it is reported as *indicative only*, never as a validated verdict.

**Error budget.** The p95 target already embeds a 5% tail; SLO compliance = the windowed p95 statistic ≤ target (equivalently, ≤ 5% of qualifying provisions exceed the p95 target value). **Excluded from the denominator:** (a) runtimes that entered `BLOCKED` on `host at capacity`/entitlement/closed gate — a §17 scaling decision, not a latency defect; (b) runtimes that reached `FAILED` on a user-caused `reason_code`. Time parked in `BLOCKED` awaiting a host slot is **subtracted** from `latency` via the `QUEUED↔BLOCKED` timestamps, so a capacity wait never counts as slow provisioning.

**Alerting.** Ticket (WARN): rolling p95 breaches target for 2 consecutive windows, or any single provision exceeds 2× the p99 target with `RUNNING` never reached. Page (CRITICAL): a `ProvisioningJob` step is stuck `RUNNING` past its lease (RX-2E orphan, TTL 300 s) **and** the runtime is still not `RUNNING` — a hung worker/agent, not mere slowness. *(Note: the per-step lease, not the whole-provision clock, gates this page, so a legitimate multi-step provision up to the p99 target of 8 min does not false-page — depends on `[OPEN — ProvisioningJob lease TTL]`.)*

**Caveats.** No number measured yet. Depends on `[OPEN — retry policy]` (a `PROVISIONING → retry → PROVISIONING` loop inflates the tail; whether retries charge here or SLO-4 must be decided). Cold-host first-provision may include one-time staging cost — Phase-4 reports cold vs warm separately.

---

### SLO-2 — Recovery time (DEGRADED / host-failure → RUNNING again)

**Definition.** Two sub-SLOs, never blended:
- **2a — Single-runtime repair.** `RUNNING → DEGRADED → REPAIRING → RUNNING` on the same host (§8.2 self-repair).
- **2b — Full-host re-materialisation.** An RDSH dies; the reconciler (extends `execution_health`, §11/§14) re-drives each affected `AccountRuntime` onto another pool host and re-points routing.

**SLI.** 2a: `created_at(RUNNING) − created_at(DEGRADED)` for the episode (intervening `REPAIRING` confirms repair, not fresh provision). 2b, per account: `created_at(RUNNING) − t_host_failure`, where `t_host_failure` = the reconciler's first host-dead `RuntimeEvent` (`to_state=DEGRADED, reason_code=host_unreachable`) or the `ComponentHealth → DOWN` transition; host metric = `max` over the N accounts that were `RUNNING` on the dead host.

**Targets (ESTIMATE — Phase-4 to validate).**

| Metric | Beta target | Steady-state target |
|---|---|---|
| 2a single-runtime repair, p95 | ≤ 3 min | ≤ 90 s |
| 2b per-account re-materialise, p95 | ≤ 8 min | ≤ 4 min |
| 2b full-host (last of N back), p95 | ≤ 15 min (**heavy profile, N ≈ 10**) | ≤ 10 min |

*N depends on which capacity profile the dead host was carrying: the **heavy** profile is 2 users × up to ~5 active automated terminals ≈ **10**; the **max** profile (capacity §3.4/§3.5) is 2 users × up to 10 active accounts ≈ **20**. The ≤ 15 min beta target is scoped to the **heavy (~10)** case; a **max-loaded (~20)** host re-materialisation is expected to take proportionally longer and its target is **explicitly deferred to Phase-4 measurement** — do not treat ≤ 15 min as covering a 20-terminal host. This N, the `rb-host-failure` SEV-2 blast-radius figure, and capacity §3.4 use the same ~10 (heavy) / ~20 (max) pair. All ride the §3 unproven density budget and must be re-derived from Phase-4 density evidence, not assumed.*

**Window.** Rolling 30-day (recovery is rare). With < 5 episodes, report raw episodes, not a percentile.

**Error budget.** Stated as a miss count: **≤ 1 episode / 30-day window** may exceed the 2b full-host target. 2a downtime is charged to SLO-3 availability, not double-counted here. **Excluded:** recoveries blocked purely on `host at capacity` (no spare host) — a capacity/headroom finding (feeds §17), surfaced as a capacity alert, not charged to recovery budget.

**Alerting.** Ticket (WARN): a 2a repair exceeds 2× its p95, or a runtime flaps `RUNNING↔DEGRADED` ≥ 3 times in 1 h. Page (CRITICAL): an RDSH is detected down and any affected account is still not `RUNNING` past the 2b target; or the reconciler cannot place an account on any host (no capacity).

**Caveats.** `REPAIRING` is Phase-2+ and undeployed. **2b is not computable today** — its `t_host_failure` anchor has no telemetry source (`[OPEN — host-death detection signal]`: no per-RDSH host-heartbeat exists; a host-death detector must be built in Phase 2 before recovery time can be measured). Broker-side outages that block `AUTHENTICATING` are **excluded** (`reason_code=broker_unreachable`). Depends on `[OPEN — retry policy]` for the `REPAIRING → FAILED` ceiling. Positions are broker-side and survive host loss (§14) — this SLO covers *runtime* restoration only, **not** any trade/position guarantee.

---

### SLO-3 — Runtime availability (per active AUTO_DEMO account)

**Definition.** Fraction of time an account that **should be RUNNING** actually *is* `RUNNING`, per active AUTO_DEMO account over 30 days, reported as a fleet distribution (worst-account and median). Per **account-runtime** (1:1 with broker account, §1), never per host or per user.

**SLI.** Two computations, both required:
1. **State-integral (authoritative).** `availability = Σ(time in RUNNING) / (window − Σ(excluded time))`, boundaries from consecutive `RuntimeEvent.created_at`. Excluded time = `STOPPING/STOPPED/DEPROVISIONING/REMOVED`, `FAILED` on a user-caused reason, `broker_unreachable` intervals, **capacity `BLOCKED` (host at capacity — a §17 scaling decision, consistent with SLO-1/2/4, not an availability defect), and the SEV-1 control-plane (infra-VM/DC) outage window** (the accepted beta SPOF per the host-failure runbook: a terminal that cannot relaunch during a DC outage is charged to the control-plane HA gap, not to per-runtime availability — `rb-host-failure` states this window carries no crisp availability number until the secondary-DC HA path exists).
2. **Heartbeat corroboration.** A `RUNNING` claim must be liveness-confirmed once a per-runtime `Heartbeat` exists (`[OPEN — per-runtime heartbeat]`; stage 8 is `NOT_CONFIGURED` today). Using the **binary `heartbeat.py` formula** (`age > 60 s × 2.5 = 150 s` ⇒ FAILED — assuming the runtime heartbeat inherits the 60 s expected interval, itself to be confirmed), any `RUNNING` interval with a stale heartbeat is **reclassified as not-available** even absent a `DEGRADED` transition. *(The `operations_summary.py` two-tier formula is deliberately not used here; divergence flagged for the packet owner.)*

**Targets (ESTIMATE — Phase-4 to validate).**

| Metric | Beta target | Steady-state target |
|---|---|---|
| Per-account availability (worst active account) | ≥ 99.0% | ≥ 99.5% |
| Fleet median | ≥ 99.5% | ≥ 99.9% |

**Window.** Rolling 30-day, per account. < 7 days of eligible history → "warming up", not a verdict.

**Error budget (computed on a 30-day = 720 h window).** 99.0% ⇒ 1% ≈ **7 h 12 min** / account / 30 d. 99.5% ⇒ 0.5% ≈ **3 h 36 min**. (Fleet-median steady 99.9% ⇒ 0.1% ≈ 43 min.) Consumed only by *system-caused* unavailability (host death, agent failure, non-user `FAILED`, stale-heartbeat-while-`RUNNING`). **Not charged:** planned stops, user-caused `FAILED`, broker-side outages, capacity `BLOCKED` (no host slot — a scaling finding), and the SEV-1 control-plane (infra-VM/DC) outage window. > 50% consumed → WARN; on exhaustion, design-doc policy freezes non-essential change for that runtime (no automated action).

**Alerting.** Ticket (WARN): 30-day availability projects below target, or rolling budget crosses 50% consumed. Page (CRITICAL): a should-be-RUNNING account is observed not-`RUNNING` (state ≠ RUNNING **or** heartbeat stale > 150 s) continuously beyond the SLO-2a repair target with no `REPAIRING` in flight — down and not self-healing.

**Caveats.** Computation (2) is **not yet possible** (per-runtime heartbeat undeployed) and is the single biggest Phase-4 prerequisite for this SLO. Makes **no** claim about signal-execution correctness or fill quality (covered by the existing per-source rollup); stays source-agnostic (never combines `ti_signals`/`wayond`).

---

### SLO-4 — Provisioning success rate (reached RUNNING without manual intervention)

**Definition.** Fraction of `PROVISION` `ProvisioningJob`s whose runtime reached `RUNNING` **hands-off** — no operator `Retry`, no manual state edit, no admin host action.

**SLI.** `success_rate = (# runtimes reaching RUNNING with zero manual RuntimeEvents on the path) / (total eligible PROVISION attempts)`. A `FAILED → QUEUED` transition marked operator-triggered (the §8.2 `FAILED ─(user/admin Retry)─▶ QUEUED` edge) disqualifies the numerator. Automatic `RETRY` events (bounded backoff, §8.2) do **not** disqualify — self-healed retries still count as hands-off success.

**Targets (ESTIMATE — Phase-4 to validate).**

| Metric | Beta target | Steady-state target |
|---|---|---|
| Hands-off provisioning success | ≥ 90% | ≥ 98% |

**Window & sample.** Rolling 30-day. Minimum **20 attempts** before the rate is treated as directional; a **≥ 98% steady-state verdict requires ≥ 100 attempts** (20 attempts cannot distinguish 90% from 98%).

**Error budget.** Beta = 10% of attempts may need manual intervention or fail system-side; steady = 2%. **Excluded from denominator:** user-caused `FAILED` (the pipeline correctly refused; the durable `RuntimeEvent(FAILURE)` is the evidence — matching the Increment-2 `provision_terminal_error` "no swallowed failure" fix) and capacity `BLOCKED` (not a failure; waiting).

**Alerting.** Ticket (WARN): rolling rate below target for 2 windows, or ≥ 3 system-caused `FAILURE` events (non-user reason) in 24 h — the same signal `AdminBetaEstateView` already surfaces (last 5 `FAILURE` per account). Page (CRITICAL): rate < 50% over 24 h with ≥ 5 attempts (systemic breakage — WinRM/agent/licensing).

**Caveats.** Sensitive to `[OPEN — reason_code taxonomy]`: the user-vs-system split *is* the numerator/denominator boundary. The WinRM-vs-broker-vs-capacity breakdown is a Phase-4 reporting requirement, not part of the headline rate.

---

### SLO-5 — Interactive RemoteApp launch and credential-change propagation

Concerns the **interactive** plane (§3, §10), which is *ephemeral and never drives runtime state* (§1) — so none of 5a–5c affects SLO-3 availability.

**5a — RemoteApp launch success rate.** Fraction of user-initiated launches (per (user, account-runtime) Guacamole connection, §10) reaching an interactive session.
- **SLI:** success / total attempts. Derived from Guacamole connection/session records tied to the per-(user,account) connection; **fail-closed rejections (e.g. disabled `GuvFX-BetaUsers` membership, §5) are counted as intended refusals, not failures.** Requires a new lightweight launch-outcome event following the `core/observability.log_stage` pattern (dedicated logger, JSON line, never raises) — no new metrics backend. `[OPEN — launch-outcome event not yet defined]`.
- **Target (ESTIMATE):** beta ≥ 97%, steady ≥ 99.5% of *eligible* (entitled, enabled) launches.

**5b — RemoteApp launch latency.** `t(session established) − t(launch requested)` from those events. **Target:** p95 ≤ 10 s (beta), ≤ 6 s (steady). Cold session-host spin-up excluded from the warm p95, reported separately.

**5c — Credential-change propagation.** Time from a broker-credential (or per-user Windows password) change to the running terminal using the new credential.
- **SLI:** the change triggers a re-injection + re-auth cycle (§9) as a `ProvisioningJob` op (`REPAIR`); `propagation = created_at(RUNNING after change) − t(change committed)`, anchored on the REPAIR job's `finished_at` and the resulting `RUNNING` `RuntimeEvent`. **The exact §8.2 re-auth transition path for a live cred change is not drawn in the diagram** (`REPAIRING ─ok─▶ RUNNING` does not pass through `AUTHENTICATING`) — `[OPEN — cred-propagation re-auth path]`; the anchor above avoids depending on an `AUTHENTICATING → RUNNING` edge that may not fire.
- **Target (ESTIMATE):** p95 ≤ 3 min (beta), ≤ 90 s (steady).

**Window / budget / alerting.** Rolling 7-day for 5a/5b; rolling 30-day for 5c. 5a beta budget = 3% of eligible launches may fail system-side (auth/gateway/agent); user-cancelled and fail-closed-denied excluded. Ticket (WARN): 5a below target for 2 windows, or 5b/5c p95 breached. Page (CRITICAL): launch success < 50% over 1 h with ≥ 5 attempts (RDGW/Connection-Broker/Guacamole outage). Shared-infra outages are **attributed by component and page immediately**, not charged to per-user launch budget.

**Caveats.** All events undeployed and `[OPEN]`. 5c must **never surface or log the credential** — only timing and a sanitised outcome, per the §9 encryption boundary and the `last_error`/`RuntimeEvent.detail` "sanitised only" rule. Interactive-plane SLOs exclude the automated AUTO_DEMO terminal (SLO-3).

---

### SLO summary table

*(All targets are ESTIMATES for Phase-4 validation. Page = `CRITICAL`, Ticket = `WARN` — same cascade, different severity.)*

| # | SLI (source telemetry) | Beta target | Window / min sample | Error budget (system-caused only) | Alert |
|---|---|---|---|---|---|
| 1 | Provisioning latency `QUEUED→RUNNING` (`RuntimeEvent`; `ProvisioningJob` cross-check) | p50 ≤ 90 s / p95 ≤ 4 min / p99 ≤ 8 min | Rolling 7 d; ≥ 20 (p99 indicative until ≥ 100) | p95 statistic ≤ target; BLOCKED-capacity & user-cred FAILED excluded, BLOCKED time subtracted | Page: job step orphaned past 300 s lease, not RUNNING. Ticket: p95 breach ×2 |
| 2a | Single-runtime repair `DEGRADED→REPAIRING→RUNNING` | p95 ≤ 3 min | Rolling 30 d | charged to SLO-3 availability | Page: down past target, no REPAIRING. Ticket: flap ≥ 3/h |
| 2b | Host re-materialise, per-acct `t_fail→RUNNING`; host = last of N | per-acct ≤ 8 min; host ≤ 15 min (heavy N≈10; **max N≈20 deferred to Phase-4**) | Rolling 30 d | ≤ 1 miss / 30 d; no-capacity excluded (capacity alert) | Page: RDSH down & acct not RUNNING past target, or no host to place on. *`[OPEN — host-death detection signal]`* |
| 3 | Availability = Σ RUNNING / eligible time (`RuntimeEvent` integral + binary 150 s heartbeat corroboration), per active AUTO_DEMO acct | ≥ 99.0% worst / ≥ 99.5% median | Rolling 30 d; ≥ 7 d history | ≈ 7 h 12 min / acct / 30 d; planned stops, user-FAILED, broker outages, **capacity-BLOCKED, SEV-1 DC-outage** excluded | Page: should-be-RUNNING down past 2a target, not self-healing. Ticket: budget > 50% |
| 4 | Hands-off `→RUNNING` / eligible PROVISION attempts (`ProvisioningJob` + manual-retry detection) | ≥ 90% | Rolling 30 d; ≥ 20 (≥ 98% verdict needs ≥ 100) | ≤ 10% manual/system; user-cred FAILED & capacity BLOCKED excluded | Page: < 50%/24 h (≥ 5). Ticket: < target ×2, or ≥ 3 system FAILURE/24 h |
| 5a | RemoteApp launch success (per-(user,acct) Guac connection; fail-closed denials = intended) | ≥ 97% eligible | Rolling 7 d | ≤ 3% system-side; user-cancel/denied excluded | Page: < 50%/1 h (≥ 5). Ticket: < target ×2 |
| 5b | Launch latency, request→interactive | p95 ≤ 10 s (warm) | Rolling 7 d | latency SLO — no budget | Ticket: p95 breach |
| 5c | Cred-change propagation → re-auth (`ProvisioningJob` REPAIR + `RuntimeEvent`) | p95 ≤ 3 min | Rolling 30 d | latency SLO — no budget | Ticket: p95 breach |

**Phase-4 prerequisites before any figure can be *measured* (not just defined):** (i) the Phase-2 `ProvisioningJob` worker + Windows agent emitting real `RuntimeEvent` transitions **and** a defined `ProvisioningJob` lease TTL; (ii) per-runtime `Heartbeat` emission wiring Account-Status stage 8 (today `NOT_CONFIGURED`); (iii) a frozen `reason_code → {user, system, capacity, broker}` taxonomy; (iv) the §8.2 retry-count/backoff schedule and the cred-change re-auth transition path pinned; (v) a launch-outcome event following `core/observability.log_stage`. Until all exist, every figure here remains an unvalidated **target**.

---

## D. Final procurement package (for approval — NO procurement yet)

> **Status — DECISION-READY DESIGN, NOT A PURCHASE ORDER.** This section consolidates §§2, 3 (hardened capacity), 16–20 and the SLO targets into a single package for Nuno's approval. It authorises **nothing**: no procurement, no VM creation, no licence purchase, no architecture-dependent (Phase 2+) work. Every cost is an **ESTIMATE** (SPLA/cloud list-price bands, not quotes); every capacity figure is an **ESTIMATE derived from one measured MT5 terminal/bridge sample** (§3.1) except where marked **MEASURED**. Nuno's existing production Windows box, MT5 runtimes, broker accounts, Guacamole access, strategies, routing, lot sizes and AUTO_DEMO operation are **out of scope, untouched, and excluded from all math below.** Onboarding stays **CLOSED** (`BETA_ONBOARDING_ENABLED` off, default) and `can_deploy_automation` stays **False** for the `beta` plan until the Phase-4 gates (§19 item 8) pass. Nothing here places, sizes, or closes a trade.

---

### 1. Refined Bill of Materials (capacity-justified)

The §16/§20 BoM is unchanged in shape (**1 infra host + 2–3 RDSH + 5 RDS Per-User CALs/SALs + TLS/backup**); the refinement here is to **fix the initial per-host spec from the §3 capacity math** rather than carry the §2 range unqualified.

| # | Host / item | Qty (beta) | **Recommended initial spec** | Capacity justification (from §3, hardened) |
|---|---|---|---|---|
| 1 | **RDSH session host** | **2** (add 3rd once density proven — §5) | **4 vCPU / 16 GB RAM / 120 GB SSD**, Windows Server 2022/2025 | At the beta density (**≤2 users/host**), a 16 GB host sits at `4 GB base + 2×1.3 GB users + 2.4 GB storm reserve ≈ 9.0 GB` typical, and `≈ 11.8 GB` with **both** users at the ~2.7 GB max (§3.5) — leaving **4–7 GB** headroom for the concurrent tick-burst + repair-storm case (§3.7) that is the whole reason density stays at 2. 4 vCPU covers the three coincident CPU peaks by inspection at ≤2 users (§3.7). **Choose 120 GB (upper of the §2 80–120 range):** a host may carry up to ~20 portable MT5 dirs (2 users × 10 provisioned accts) ≈ 6 GB of program copies + history/log growth toward the 0.5–1.5 GB/account envelope (§3.2) — fits 120 GB **only with the mandatory log/history pruning policy** in force. |
| 2 | **Infra host** (AD DS + RDCB + RDGW + RDWeb + RDS Licensing, collapsed) | 1 | **4 vCPU / 16 GB RAM / 80 GB SSD**, Windows Server 2022/2025 | Control-plane, **not** runtime: its load is per-login / session-broker / licensing, **not** per-terminal, so it scales far past 5 users on one VM (§3.9). Pick **16 GB** (top of the §2 8–16 range) because five roles are collapsed onto one VM; it becomes the ceiling only via **availability** (SPOF — see §6/§SEV-1), never capacity. |
| 3 | **RDS Per-User CAL / SAL** | 5 (= `active_users`) | RDS **Per-User** SAL via SPLA | One licence per beta user (`CALs/SALs = active_users`, §3.9). Per-User (not Per-Device) because a user reaches their RemoteApp from any browser via RDGW. |
| 4 | **SSD storage** | all hosts | **SSD mandatory (not a preference)** | §3.2: spinning disk would serialise concurrent tick-burst flushes and multiply portable-dir re-materialisation time during host-failure recovery (§3.7). Design constraint. |
| 5 | TLS cert / RD Gateway | 1 | Let's Encrypt via existing Traefik edge (§18) | Single external path is RDGW/443 (§3.8, §18). |
| 6 | Backups / snapshots | — | Daily infra-VM snapshot **+** GuvFX DB backup | Two **distinct** prerequisites (SEV-1 backup-gap): (a) current infra-VM snapshot for control-plane restore; (b) recoverable GuvFX DB (holds durable `AccountRuntime` state + Fernet creds). Both currently **OPEN** (estate RED gap) and are hard preconditions (P6) before any real provision. |

> **Do not oversubscribe RAM on RDSH** (§3.6): MT5 tick handling is latency-sensitive; paging a terminal during a tick burst risks late/missed execution — fail-closed. CPU may be **modestly** oversubscribed (≤~2:1) **only after** §3.7 tick-burst + repair-storm peaks are measured in Phase 4, never inferred from the near-idle steady state.

---

### 2. Licensing model

| Model | Up-front | Monthly | Best when |
|---|---|---|---|
| **SPLA / cloud-rented — RECOMMENDED for beta** | **~$0** | **~$350–700/mo** (3–4 Windows VMs + RDS SAL ×5) | Elastic; add/remove hosts and per-user SALs as `active_users` moves; **prove density first** before committing capital. |
| Owned licences | **~$3.5–5.5k one-off** (Windows Server Std ×3–4 + RDS Per-User CALs ×5) + VM hosting | VM hosting only | Steady-state **after** beta, once §3 density + user retention are proven. |

**Recommendation: start on SPLA/monthly.** Rationale: (i) no capital outlay against an **unproven** density assumption (§3.6 — "density is a Phase-4 proof, raise only with evidence"); (ii) the scaling model is "add an RDSH per ~2 users" (§5), which SPLA prices elastically per-VM and per-SAL; (iii) if Phase-4 measurement **corrects the 210 MB/600 MB planning values upward** and density must stay at 2 (or drop), SPLA lets us add hosts without stranded owned CALs. **Revisit owned CALs only after** beta proves both density and retention. No licence is purchased until Nuno approves this package.

---

### 3. Deployment topology (concise — see §18 for the full diagram)

- **Existing Ubuntu VPS is unchanged:** Frontend + Backend + workers + Guacamole + Traefik stay put. Backend/workers drive provisioning via **`ProvisioningJob` → WinRM/PowerShell agent** to the infra host, and **per-account bridge (orders)** to the RDSH pool.
- **New Windows footprint = 1 infra host + 2–3 RDSH.** Infra host runs AD DS · RDCB · RDGW · RDWeb · RDS Licensing (collapsed). RDSH run per-user non-admin `guvfx_u_<uid>` identities, each hosting per-account portable-MT5 runtimes (automated terminal + bridge), NTFS-isolated per account.
- **Single external path:** browsers reach RDSH **only** via RDGW/443 (fronted by the existing Traefik/TLS edge). RDSH are **not** otherwise public (§13 layer 5).
- **Nuno's production box:** outside the pool, no runtime/identity/Guacamole connection/routing target may resolve to it (isolation invariant 2).

---

### 4. Deployment / implementation sequence (per §19 — each step gated + reviewed; onboarding CLOSED until Phase 4)

| Phase | Step (§19) | Gate |
|---|---|---|
| **1 — Procure** | Stand up 1 infra host + 2 RDSH (SPLA); daily infra snapshot **and** GuvFX DB backup baseline (closes P6). | **Red — Nuno approval of this package** before any VM/licence is created. |
| **2 — Control-plane** | (1) Data model `AccountRuntime`/`RuntimeEvent`/**`ProvisioningJob`** (additive migrations); (2) idempotent WinRM/PowerShell Windows agent (read-only proof first, then materialise) + health probe; (3) provisioning worker (one durable step/iteration, persist-then-act, retries, no swallowed errors). | Each increment reviewed; **retry count N + backoff schedule must be fixed here** (OPEN in §8.2). |
| **3 — Multi-tenant execution** | (4) per-account routing on Phase-0 fail-closed `_get_user_mt5_instance`; (5) auto-router fan-out + per-account sizing override; (6) RemoteApp + per-user AD identity + per-account Guacamole connection; (7) wire Account Status panel to live state. | Reviewed; per-account/per-user isolation asserted at every step. |
| **4 — Isolation + load hardening (GATE)** | (8) per-user/per-account isolation red-team; **≥5 concurrent automated terminals across 2–3 hosts at ≤2 users/host** (do **not** co-locate 5 on one host — §3.6); RAM/CPU/disk p50/p95/max via Phase-4 perfmon capture; repair-storm cost; no cross-tenant data; production unaffected. | **Only on PASS** may Nuno (Red) open `BETA_ONBOARDING_ENABLED` and, separately, flip `can_deploy_automation`. |

**Onboarding stays CLOSED for the entire sequence.** A `RUNNING` runtime still cannot place orders (`can_deploy_automation=False` is an independent server-side block). No email-verify bypass, no signal replay, no forced trade at any step.

---

### 5. Operating costs (monthly OPEX)

All figures **ESTIMATE** (SPLA/cloud list bands, not quotes). The existing Ubuntu/OVH VPS is **already-incurred spend shown for total-cost context**, not new beta cost.

| Line | Model | Monthly (ESTIMATE) | Notes |
|---|---|---|---|
| Existing Ubuntu VPS (backend, workers, Guacamole, Traefik, DB) | already running | **~$40–90** | **Not new beta spend** — sunk estate cost, shown for all-in context (§18: Ubuntu VPS unchanged). |
| Windows **infra host** (SPLA, incl. Windows) | new | **$80–150** | §16. |
| Windows **RDSH ×2** (SPLA) | new | **$180–320** | 2 × $90–160 (§16). |
| Windows **RDSH 3rd** (added once density proven — §5) | new, conditional | **+$90–160** | Not in the initial spend; adds failure-tolerance headroom (§2). |
| **RDS Per-User SAL ×5** | new | **$20–35** | 5 × $4–7/user/mo (§16). |
| TLS / Gateway | new | **$0–15** | Let's Encrypt via existing Traefik, else nominal. |
| Backups / snapshots (infra VM + DB) | new | **$10–30** | Closes the SEV-1 backup-gap prerequisite. |
| **New Windows infra subtotal** | | **≈ $290–550/mo** (2 RDSH) → **$380–710/mo** (3 RDSH) | Matches the §16/§20 **$350–700/mo** SPLA envelope. |
| Ops / support labour (**ESTIMATE** — assumes ~2–4 h/wk: onboarding, `Retry`/`Resume`, capacity/alert triage per the support runbook) | new | **~$200–800** (rate-dependent) | Priced by Nuno's chosen blended rate; the hours assumption is the load-bearing input, not the dollar figure. |

- **Beta all-in range (incl. existing VPS + labour)** = new-infra subtotal **+** VPS ($40–90) **+** labour ($200–800), computed consistently on the **itemized** subtotals (low and high ends use the same infra figure): **≈ $530–1,440/mo** (2 RDSH), rising to **≈ $620–1,600/mo** with the 3rd RDSH. The **new-cloud-infra** portion alone is **≈ $290–550/mo (2 RDSH) / $380–710/mo (3 RDSH)** — consistent with, and bracketed by, the §16/§20 **~$350–700/mo** SPLA planning envelope (which is a rounded 3-RDSH planning band, not the 2-RDSH itemized cost).
- **Per-user marginal cost (steady beta):** adding a user consumes **½ an RDSH** (density 2/host, §5) **+ 1 SAL** ⇒ `≈ ($90–160 ÷ 2) + $4–7 ≈ **$49–87 / user / mo**` in infra/licensing (excludes the near-fixed infra host, backups, and labour, which amortise across the cohort). This marginal figure is **valid only at the proven density**; a Phase-4 correction to density or the 210 MB/600 MB planning values re-prices it.

---

### 6. Scaling model

**Host count formula (§3.9):**

```
active_users = users with ≥1 active automated account
density      = validated interactive users per RDSH   (beta = 2, UNPROVEN — §3.6)

RDSH_count   = ceil(active_users / density)
infra        = 1 collapsed infra VM (serves well beyond beta — control-plane, not per-terminal)
CALs/SALs    = active_users            (1 RDS Per-User SAL each)
```

Worked (beta): `ceil(5 / 2) = 3` RDSH; **start with 2, add the 3rd once density is proven** (headroom + one-host-failure tolerance in the interim).

**The density-proof gate (§3.6 — governed, fail-closed, reversible).** Density is raised **only** by a documented decision (ADR/Notion record), **never** an in-passing config change, and **only** when the Phase-4 capture shows, at the proposed density, **p95 host utilisation below ~70% RAM and ~60% CPU with the §3.7 storm reserve remaining as unallocated headroom on top of measured utilisation, AND the cross-tenant isolation gate passes** (gate 6, independent of resource numbers). Verify over ≥1 further full session at the new density; **roll back immediately** and record the breach if utilisation or the storm reserve is exceeded. Until that capture exists, the thresholds are **load-test acceptance criteria, not measurable operational SLOs** (no host-resource telemetry exists today — §3.6).

**Role-split + HA path beyond beta (§17, design only — not scheduled, not authorised):**

| Trigger | Action |
|---|---|
| Infra-host SPOF blocks availability (SEV-1) | Split DC / RDCB / RDGW / Licensing onto separate VMs; add a **2nd DC**, **redundant RDCB (HA broker)**, **2+ RDGW behind a load balancer**. |
| `active_users` outgrows one RDGW/443 path (§3.8) | Add RDGW instances + LB. |
| Density proven higher in Phase-4 | Recompute `RDSH_count` with the new `density`; RDCB auto-load-balances the pool. |

App-layer fan-out is already O(N accounts) and host-agnostic — **not** a scaling ceiling; the ceiling is **host capacity + RDS-infra HA**, not GuvFX code.

---

### 7. Approval ask

**Nuno's decision is requested on this single package:**

1. The **refined BoM** (§1) — 2 RDSH at **4 vCPU / 16 GB / 120 GB SSD**, 1 infra host at **4 vCPU / 16 GB / 80 GB SSD**, 5 RDS Per-User SALs, SSD mandatory, daily infra snapshot + DB backup.
2. The **SPLA/monthly licensing model** (§2) for beta.
3. The **topology** (§3/§18) and **gated Phase-2→4 sequence** (§4/§19), onboarding CLOSED until Phase-4 gates pass.
4. The **OPEX envelope** (§5): **~$290–710/mo** new cloud infra (2–3 RDSH; §16/§20 planning envelope ~$350–700/mo); **~$530–1,600/mo** all-in (2–3 RDSH) incl. existing VPS + labour; **~$49–87/user/mo** marginal at proven density.

**Reaffirmation.** **No procurement, no VM creation, no licence purchase, and no architecture-dependent (Phase 2+) implementation begins until Nuno explicitly approves this BoM + topology + licensing + cost + §19 sequence.** All capacity numbers remain ESTIMATES pending Phase-4 measurement; density (2/host) is an unproven planning assumption, not an SLO. `BETA_ONBOARDING_ENABLED` stays off and `can_deploy_automation` stays False (Red — Nuno only, Phase 4). Nuno's production estate stays untouched and excluded. **What is NOT covered / must be proven in Phase 4:** the single-sample RAM footprint (no p95/variance), repair-storm cost, RDP bitrate/external-path capacity, disk pruning policy, retry N + backoff schedule, and end-to-end cross-tenant isolation under 5-user load — each a load-test acceptance criterion, none a measurement today.

---

## E. Consolidated open items (Phase-2 prerequisites)

Every SLO target above is a **design target**, and several runbook steps are **TARGET**, because the
control-plane is unbuilt and the platform has **zero** provisioning telemetry today (every
`AccountRuntime` is `NOT_PROVISIONED`). The following dependencies — surfaced consistently across the
capacity, runbook, and SLO sections — **must be closed in Phase 2** before any figure here can be
*measured* (not merely defined), before density can be raised, and before onboarding can open. None is
authorised by this document.

| # | Open item | Blocks | Owner / how it closes |
|---|---|---|---|
| O1 | **Retry count `N` + exponential-backoff schedule** are unspecified in code and design (`attempt`/`next_retry_at` fields exist; no `N`/base/cap/jitter). | SLO-1 tail, SLO-2a/2b repair ceiling, every runbook retry/exhaustion branch. | Phase-2 provisioning worker fixes them by ADR. |
| O2 | **`reason_code` → {user, system, capacity, broker} taxonomy not frozen.** | The user-vs-system boundary that scopes **every** error budget (SLO-1/3/4). | Curated map defined alongside the Phase-2 worker. |
| O3 | **Per-runtime heartbeat NOT_CONFIGURED** (Account-Status stage 8). | SLO-3 liveness corroboration; "logged-in + heartbeat" verification in every runbook is only PARTIAL. | Phase-2 heartbeat emission + wiring. |
| O4 | **No per-RDSH host-death detection signal** (named heartbeats cover workers, not hosts). | SLO-2b `t_host_failure` anchor; host-failure runbook detection latency. | Phase-2 host-heartbeat / `ComponentHealth` for RDSH. |
| O5 | **`ProvisioningJob` unshipped; lease TTL undefined** (assumed to mirror `EXECUTION_LEASE_TTL_SECONDS=300`). | SLO-1 orphan-page threshold; onboarding/recovery step timing. | Phase-2 `ProvisioningJob` model + lease policy. |
| O6 | **Two coexisting heartbeat-staleness formulas** (`heartbeat.py` binary x2.5 vs `operations_summary.py` tiered x2.0/x4.0). | Any health-based verification/alerting threshold (flagged, not silently reconciled). | Packet owner picks the canonical formula. |
| O7 | **Cred-change re-auth transition path** not drawn in §8.2 (`REPAIRING-ok->RUNNING` skips `AUTHENTICATING`); **launch-outcome event** undefined. | SLO-5c propagation SLI; SLO-5a/5b interactive-plane SLIs; password/broker-migration verification. | Phase-2 defines the transition + a `log_stage`-style launch event. |
| O8 | **GuvFX/provisioning DB backup gap** (estate RED gap; newest DB backup ~4.5 months stale at last audit) + **no current infra-VM snapshot**. | The "rebuild-from-durable-state + Fernet creds" guarantee underpinning onboarding recovery, runtime recovery, and SEV-1/SEV-2 host recovery. | BACKUP-RECOVERY-BASELINE (a **hard precondition P6** before any real provision). |
| O9 | **Density (~2 users/RDSH) is UNPROVEN** — a planning assumption, not a measurement; the 70% RAM / 60% CPU raise thresholds are load-test acceptance criteria with no host-resource telemetry today. | Every per-host/pool capacity figure, the recovery-N (~10 heavy / ~20 max), the OPEX per-user marginal cost. | Phase-4 load test fixes `U_max`/`T_max` with real p50/p95/max capture. |
| O10 | **Windows launch-task credential model undecided** (Scheduled Task with stored password vs logon task vs **gMSA**). | The "automated terminals keep running across restart / host-failover" claim in the password + host-failure runbooks. | Phase-2 identity design decision (prefer gMSA/managed identity). |
| O11 | **§8.2 lacks a clean `STOPPED` -> re-provision edge** used by broker migration; the "state stays STOPPED, REPAIR-op side-effect" framing is a design judgement. | Broker-migration runbook state accuracy. | Amber ADR if a visible migration state is wanted. |

**Bottom line for approval.** The §D procurement package can be approved on its own terms (BoM,
licensing, topology, sequence, OPEX, scaling) — it is decision-ready. But **O1–O11 are the work of
Phase 2/4**, and until they close, the SLOs are targets and the runbooks are documentation. Nothing in
this document changes production, opens onboarding, or authorises spend.
