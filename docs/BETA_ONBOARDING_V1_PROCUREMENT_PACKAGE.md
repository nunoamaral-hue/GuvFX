# GFX Beta Onboarding V1 — Phase 2 Procurement & Planning Package (for approval)

> **Status: FOR APPROVAL — this is PLANNING, not procurement authority.** Nuno accepted Option A
> (Windows-native RDS/RemoteApp) as the Phase-2 target architecture on 2026-07-20 and authorised Phase-2
> **non-procurement software planning/implementation**. This document is the supplier-backed procurement
> comparison + refined capacity model + final topology + concurrency/account limits + the Phase-2
> implementation plan + the exact procurement action, assembled for Nuno's approval. It authorises
> **NO purchase, NO paid server, NO licence buy, NO external onboarding, and NO architecture-dependent
> spend.** No procurement executes until Nuno explicitly approves the recommended action in §5.
>
> **Governance invariants (throughout):** onboarding stays CLOSED (`BETA_ONBOARDING_ENABLED` off);
> `can_deploy_automation` stays False for `beta`; Nuno's existing Windows host, MT5 runtimes, broker
> accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO are out of scope and untouched;
> TI Signals and Wayond execution unchanged; no shared Windows desktop; no fallback to any unowned
> runtime (fail-closed to None); no per-account sizing wired to production execution.
>
> **Companion docs:** [`…ARCHITECTURE_OPTION_A.md`](BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md) (architecture,
> state machine §8, topology §18) · [`…OPERATIONS_CAPACITY_SLO.md`](BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md)
> (§A capacity, §B runbooks, §C SLOs, §D procurement envelope, §E open items) ·
> [`…PROGRAMME.md`](BETA_ONBOARDING_V1_PROGRAMME.md) (programme + Phase-0 log).
>
> **Provenance & price integrity.** Supplier prices were researched live on the web (multi-provider
> fan-out with a per-provider adversarial price-verification pass) and passed a completeness critic
> (verdict *REVISE BEFORE SENDING*; all its findings resolved here). The one **decision-driving** figure —
> the per-user RDS access licence — was **personally re-verified** by the author against the AWS Marketplace
> listing (EC2 RDS SAL **$10.00/user/mo**, 2026-07-20), correcting an earlier draft that had conflated it
> with the Amazon WorkSpaces SAL ($4.19). **Every figure is tagged MEASURED / ESTIMATE / quote-required;
> no quote-required figure is presented as exact, and nothing here is a purchase authority.**

---

## Table of contents

- **§1 Refined capacity & concurrency model** — the five distinguished dimensions + the initial concurrently-hosted broker-account cap and how it expands.
- **§2 Final deployment topology** — component placement, single external path, RDCB decision point, infra-host SPOF.
- **§3 Supplier-backed procurement comparison** — OVH · Hetzner · Azure · AWS · SPLA-inclusive VPS · licensing routes & compliance (real sourced prices).
- **§4 Control-plane analysis (procurement items 8–11)** — role coexistence · Connection-Broker necessity · failure-domain SPOF · backup & restore.
- **§5 Comparison, cost breakdown, recommendation & exact procurement action** (items 6 & 12).
- **§6 Phase 2 implementation plan (non-procurement software)** — the authorised increments + DO-NOTs.
- **§7 What Nuno is asked to approve.**

---

## 1. Refined capacity & concurrency model

> **Status — DESIGN / PLANNING ONLY.** This refinement extends [`§A Capacity planning`](BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md) and the [`§D procurement package`](BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md); it authorises **no** procurement, purchase, paid server, licence buy, or architecture-dependent implementation code. Onboarding stays **CLOSED** (`BETA_ONBOARDING_ENABLED` off, default); `can_deploy_automation` stays **False** for the `beta` plan until the Phase-4 gates pass. Nuno's existing production Windows box, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO operation are **out of scope, untouched, and excluded from all math below.** Every capacity figure is an **ESTIMATE derived from one measured terminal/bridge sample** (§A/§3.1) except where marked **MEASURED**; no estimate or price is asserted as fact.

#### Why five dimensions, not one

"How many users / accounts can the beta hold?" has **five different answers** that are routinely conflated. Conflating them is what produces the wrong claim *"5 users × 10 accounts = 50, so the pool must run 50 terminals."* It must not. Only **one** of the five dimensions actually loads the host pool — the count of **continuously-running automated terminals** — and that dimension is bounded by an **unproven** density assumption, so it must carry its own conservative, server-enforced cap that is **distinct** from the product's 10-accounts-per-user configuration cap.

The five dimensions, defined and distinguished:

1. **REGISTERED users** — anyone who has signed up and been auto-granted the `beta` entitlement. Effectively **unbounded-ish**, because registration grants entitlement, not capacity. It is gated at the point that matters: a registered user with no open onboarding gate and `can_deploy_automation=False` consumes **zero** pool resource. Registration is a control-plane fact, not a load fact.
   - **Beta value: unbounded-ish (gated).**

2. **ACTIVE users** — registered users who actually use the platform: **≥1 active automated account** or a live interactive session. This is the cohort the host-count formula (§3.9) is sized around (`RDSH_count = ceil(active_users / density)`).
   - **Beta value: up to 5.**

3. **SIMULTANEOUS RemoteApp SESSIONS** — interactive viewers connected *at one moment* through RDGW/443. **Typically 1 per user** (a person watches one terminal), ephemeral, and — critically — a session **never drives runtime state** (opening/closing a viewer never provisions or tears down an automated terminal, §3.3). Budget a **small concurrent peak**, not one-per-registered-user.
   - **Beta value: plan ~1/active user; the 2-host pool seats ~4 concurrent interactive sessions** (≤2 interactive users/host × 2 hosts); a 5th *simultaneous* viewer holds briefly at the Connection Broker until a slot frees. Because sessions are ephemeral (a viewer is open for seconds-to-minutes, not continuously), a 5th concurrent viewer is a rare, transient queue — **not** a blocker on that user's automated trading, which runs headless regardless (dim 5). ESTIMATE, ~600 MB each per §3.3.

4. **BROKER ACCOUNTS (configured)** — accounts a user has *created* in the product. Product cap = **10/user** ⇒ up to **50 across 5 users**. These are **CONFIGURED, not running**: most are expected to sit idle (`NOT_PROVISIONED`/`STOPPED`) and cost only a portable-MT5 directory on disk, **not** a live terminal + CPU. The "50" is a **configuration ceiling, never a running-load figure.**
   - **Beta value: ≤ 10/user, ≤ 50 pool (configured — most NOT running).**

5. **CONTINUOUSLY-RUNNING automated MT5 TERMINALS** — one headless `terminal64.exe` (+ its ~33 MB bridge) per **active** account, always-on, surviving RemoteApp disconnect. **This is the load-bearing pool-capacity dimension** — the only one that consumes always-on RAM/CPU across the host pool. Everything the density math, the storm reserve, and the host count exist to protect is measured *here*.
   - **Beta value: capped at ~16 pool (see below) — NOT 50.**

#### The load-bearing dimension does not scale to 50

**CRITICAL:** the 2-host pool must **not** be assumed to run 50 continuous terminals.

- **MEASURED (one sample, one terminal, one moment — §3.1):** `terminal64.exe` ~165 MB working set / ~98 MB private / 16 threads / near-idle steady CPU; bridge ~33 MB. This is **one observation, not a distribution** — no variance, no p95, no multi-terminal-interference figure.
- **ESTIMATE (planning value derived from that sample):** ~**210 MB** per automated runtime (terminal + bridge), worst ~**260 MB**.
- At the beta density of **≤2 users/host** (UNPROVEN — §3.6), the **heavy** profile is ~5 active automated accounts/user ⇒ **~10 running terminals/host ⇒ ~20 across the 2-host pool** (ESTIMATE). A hypothetical **all-10-automated** load would be **~20 terminals/host**, and **~20/host is UNPROVEN** — it rests on estimates the single-sample footprint cannot validate and on density the platform has never measured (no host-resource telemetry exists today, §3.6).
- Therefore the pool's *safe* running-terminal ceiling for beta is **well below** the 50 configuration ceiling, and even below the ~20 heavy-envelope estimate — because the whole point of the ≤2/host density is to leave 4–7 GB/host headroom for the concurrent tick-burst + repair-storm case (§3.5/§3.7), not to pack terminals to the estimate's edge.

#### Initial beta entitlement limit: a hosted (running) concurrency cap

Establish an **INITIAL BETA ENTITLEMENT LIMIT for concurrently-HOSTED (running) broker accounts**, enforced **server-side**, **distinct** from the 10-accounts-per-user configuration cap:

> **Proposed initial cap: ~16 concurrently-running automated terminals across the 2-host pool** — an average of **~3 active accounts per beta user** (5 users × 3 ≈ 15–16), **NOT all 50 configured accounts**.

Rationale for ~16 (conservative, ESTIMATE-based):
- It sits **below** the ~20-terminal heavy envelope estimate for the 2-host pool, preserving the storm/tick-burst reserve rather than consuming it.
- ~3 active/user is a realistic beta usage shape (the §3.4 *typical* user is 1–3 active automated), not the *max* outlier.
- It is **fail-closed against measurement error**: because the 210 MB planning value may be understated under real concurrent load (single-sample caveat, §3.1), the cap deliberately leaves margin the arithmetic alone would spend.

#### Config cap vs hosted/running cap — the distinction

| Cap | What it limits | Scope | Where enforced (today / target) |
|---|---|---|---|
| **Configuration cap (existing)** | How many broker accounts a user may **create** | Per **user**, ceiling **10** | **Shipped** — `resolve_entitlements(...).max_trading_accounts`, hard-ceilinged `min(10, …)`, enforced **atomically** on account create (`select_for_update` on the user row) at `backend/trading/views.py` (Increment 5, PR #153). A created account costs disk, not a terminal. |
| **Hosted / running concurrency cap (proposed, new)** | How many automated terminals may be **RUNNING at once** | **Pool-wide** (and optionally per-user sub-limit) | **Design target — Phase 2.** A user requesting activation/provisioning of an account is admitted only if the pool's running count is below the cap; otherwise the runtime holds at `QUEUED`/`BLOCKED (host at capacity)` per the §8.1/§8.2 state machine — it never silently exceeds the ceiling. |

The two caps answer different questions: **config cap** = "may this account exist?" (a create-time entitlement check); **hosted cap** = "may this account *run right now*?" (a provisioning-admission check on the load-bearing dimension). A user can legitimately have 10 accounts configured while only ~3 are hosted/running — the config cap and the hosted cap are both satisfied.

**Enforcement point (target, Phase 2 — not built today).** The hosted cap belongs at the **provisioning-admission boundary**, precisely the `QUEUED ─(no capacity)─▶ BLOCKED` edge already drawn in §8.2. When a broker-account activation is requested, the provisioning worker checks the durable pool running-count against the entitlement cap **before** materialising a runtime; over-cap requests hold in `BLOCKED (host at capacity)` — a truthful, user-visible state — until a slot frees or the ceiling is raised. This reuses the platform's existing capacity-bookkeeping shape (`HostingNode.max_accounts` / `computed_active_accounts`, `execution/models.py`) rather than adding new machinery. It is **independent of** `can_deploy_automation`: even an admitted, `RUNNING` runtime still cannot place an order while the automation gate is False.

#### How the running cap EXPANDS (measured, governed, reversible)

The hosted cap is **not** a fixed constant — it is the **proven** running-terminal ceiling, and it rises **only with measured density evidence**:

- **Add an RDSH → +N proven terminals.** Adding the 3rd RDSH (already in the §D BoM, "add once density proven") raises the pool ceiling by the number of terminals that host is **measured** to carry — not the estimate.
- **N comes from Phase-4 load evidence, not arithmetic.** N is set from the Phase-4 capture (p50/p95/max RAM per terminal at the proposed density, with the §3.7 storm reserve remaining as unallocated headroom, **and** the cross-tenant isolation gate passing — §3.6). Until that capture exists, N stays at the conservative planning value.
- **The ceiling rises only with measured density.** Any increase to the hosted cap is a **documented decision (ADR/Notion record), never an in-passing config change**, and is **reversible** — if measured p95 utilisation breaches ~70% RAM / ~60% CPU or the storm reserve is consumed, revert the cap to the prior value and record the breach (§3.6 density-raise rule).

#### Reconciling the two host ceilings (and why 2 hosts, not 3)

Two **distinct** per-host ceilings apply at once; they measure different resources and must not be conflated:

| Ceiling | What it limits | Resource | 2-host pool value |
|---|---|---|---|
| **Interactive-session density** | concurrent RemoteApp **viewers** | RDP session + user profile + frame-encode (~600 MB + CPU/session) | ≤2 interactive users/host → **~4 concurrent interactive sessions** |
| **Automated-terminal capacity** | continuously-**running** headless terminals | terminal + bridge RAM (~210 MB each), always-on | ~8/host initial cap (heavy est ~10) → **~16 pool** |

These do **not** compete 1:1 — a headless terminal has no RDP-session overhead, and a viewer adds no second terminal (it re-attaches the existing one, §3.3). So one host can simultaneously run ~8 automated terminals *and* host ~2 interactive viewers.

**Why the approved 2-host start is coherent (not under-provisioned).** The §3.9 formula `RDSH_count = ceil(active_users / density) = ceil(5/2) = 3` sizes for **guaranteeing every one of 5 active users a dedicated *interactive* slot at the same instant**. Beta does **not** need that guarantee: active-user count is bounded by the **automated-terminal** cap (dim 5), **not** by interactive density — all 5 users' automated terminals run within the ~16 cap on 2 hosts, independent of who is viewing. The only thing 2 hosts cannot guarantee is a **5th simultaneous interactive viewer**, which holds briefly at the Connection Broker (an ephemeral, seconds-to-minutes queue). Nuno's approved **2 RDSH** start is therefore correct; the **3rd RDSH is the first expansion**, triggered when **either** (a) a 5th *concurrent* interactive session becomes routine, **or** (b) the measured running-terminal count approaches the hosted cap and more headroom is needed — added once Phase-4 density is proven. If a hard 5-concurrent-interactive guarantee is wanted from day one, the alternative is to launch with 3 RDSH per `ceil(5/2)`; this is called out as an explicit option for Nuno, not assumed.

#### Five-dimension mapping (beta values + enforcement point)

| # | Dimension | What it counts | Beta value | Loads the pool? | Enforcement point |
|---|---|---|---|---|---|
| 1 | **Registered users** | Signed-up accounts with `beta` entitlement | **Unbounded-ish (gated)** | No | Registration grants entitlement, not capacity; onboarding gate (`BETA_ONBOARDING_ENABLED`) + `can_deploy_automation=False` neutralise idle registrants |
| 2 | **Active users** | Users with ≥1 active automated account / live session | **Up to 5** | Indirectly (drives dims 3+5) | Cohort size for `RDSH_count = ceil(active_users / density)` (§3.9); an operational admission target, not a hard code gate |
| 3 | **Simultaneous RemoteApp sessions** | Interactive viewers connected at one moment | **~1/active user; peak ≤ ~5** | Yes (ephemeral, ~600 MB/session — ESTIMATE) | RDCB session brokering + RDGW/443; viewer never provisions/tears down a runtime (§3.3) |
| 4 | **Broker accounts (configured)** | Accounts a user has created | **≤ 10/user, ≤ 50 pool** | No (disk only until run) | **Shipped** config cap: atomic `min(10, max_trading_accounts)` on create — `backend/trading/views.py` (Increment 5) |
| 5 | **Continuously-running automated terminals** | Always-on headless `terminal64.exe` + bridge per **active** account | **Cap ~16 pool (~3/user avg) — NOT 50** | **Yes — the load-bearing dimension** | **Proposed hosted/running concurrency cap**, Phase-2 provisioning-admission check at the `QUEUED→BLOCKED (host at capacity)` edge (§8.2), pool-wide, distinct from dim 4; raised only by measured Phase-4 density evidence via ADR |

#### Separation of MEASURED and ESTIMATE (for this deliverable)

- **MEASURED:** one `terminal64.exe` ~165 MB working set / ~98 MB private / 16 threads / near-idle CPU; bridge ~33 MB (single sample, §3.1). The **existing config cap** enforcement (atomic `min(10,…)` at account create) is **shipped code**, verifiable in `backend/trading/views.py`.
- **ESTIMATE:** the ~210 MB/260 MB per-runtime planning value; the ~10-terminals/host heavy / ~20-pool envelope; the ~20/host maximum (UNPROVEN); the ~16-terminal / ~3-per-user proposed cap; density ≤2/host (UNPROVEN). All rest on one sample plus vendor typicals and **must be replaced by the Phase-4 measured distribution** before the hosted cap is raised.

**Governance reaffirmation.** This is planning for approval. It authorises no procurement, no purchase, no paid server, no licence buy, and no architecture-dependent implementation. The proposed hosted-account concurrency cap is a **Phase-2 design target**, not shipped behaviour; the shipped configuration cap is unchanged. Onboarding stays CLOSED; `can_deploy_automation` stays False for `beta`; Nuno's production estate is untouched and excluded. Nothing here places, sizes, or closes a trade, or opens onboarding.

---

## 2. Final deployment topology

> **Status: PLANNING for approval.** This diagram authorises no procurement, no paid server, no licence purchase, and no architecture-dependent implementation code. Onboarding stays **CLOSED**. Nuno's existing Windows host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes, and AUTO_DEMO are **out of scope and untouched**. All host counts, roles, and the include/defer decision below are **design intent (ESTIMATE)**, not provisioned fact. The only **MEASURED** elements are the existing Ubuntu VPS stack and Nuno's production box, which already exist and are unchanged here.

#### Topology (ASCII)

```
                                 INTERNET
                                    │
                                    │  (1) SINGLE EXTERNAL PATH
                                    │      HTTPS/TLS 443  →  RD Gateway
                                    ▼
        ┌───────────────────────────────────────────────────────────────┐
        │  BROWSER ACCESS LAYER  (existing Ubuntu VPS — UNCHANGED)        │
        │  ┌──────────┐   ┌───────────────┐   ┌───────────┐  ┌────────┐  │
        │  │ Traefik  │──▶│ Next.js FE /  │   │ Guacamole │  │Postgres│  │
        │  │  (TLS)   │   │ DRF backend + │   │ (guacd +  │  │  16    │  │
        │  │          │   │ workers/queue │   │  webapp)  │  │        │  │
        │  └──────────┘   └──────┬────────┘   └─────┬─────┘  └────────┘  │
        └───────────────────────┼──────────────────┼───────────────────┘
                                 │                  │
              (2) WinRM 5986     │                  │  RDP over
                  PROVISIONING   │                  │  RD Gateway (443)
                  (internal only)│                  │  browser → MT5 desktop
                                 ▼                  ▼
        ┌───────────────────────────────────────────────────────────────┐
        │  WINDOWS RDS POOL  (NEW — design only, NOT procured)           │
        │                                                                 │
        │   ┌─────────────────────────────────────────────────────────┐  │
        │   │  INFRA HOST  ★ SINGLE POINT OF FAILURE (item 10)         │  │
        │   │  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐   │  │
        │   │  │ RD Gateway  │  │  RD Web       │  │  RDCB          │   │  │
        │   │  │ (443 ingress│  │  Access       │  │ (RD Connection │   │  │
        │   │  │  terminator)│  │               │  │  Broker)       │   │  │
        │   │  └─────────────┘  └──────────────┘  └───────┬────────┘   │  │
        │   │                        DECISION POINT ──────┘            │  │
        │   │              item 9: INCLUDE vs DEFER RDCB               │  │
        │   └─────────────────────────┬───────────────────────────────┘  │
        │                             │ session brokering / direct RDP    │
        │              ┌──────────────┴───────────────┐                   │
        │              ▼                              ▼                    │
        │   ┌────────────────────┐        ┌────────────────────┐          │
        │   │  RDSH #1           │        │  RDSH #2           │          │
        │   │  guvfx_u_<uid>     │        │  guvfx_u_<uid>     │          │
        │   │  per-user identity │        │  per-user identity │          │
        │   │  ┌──────────────┐  │        │  ┌──────────────┐  │          │
        │   │  │ MT5 runtime  │  │        │  │ MT5 runtime  │  │          │
        │   │  │ per account  │  │        │  │ per account  │  │          │
        │   │  │ (portable,   │  │        │  │ (portable,   │  │          │
        │   │  │  D:\GuvFX\   │  │        │  │  D:\GuvFX\   │  │          │
        │   │  │ users\<uid>\)│  │        │  │ users\<uid>\)│  │          │
        │   │  └──────────────┘  │        │  └──────────────┘  │          │
        │   └────────────────────┘        └────────────────────┘          │
        └───────────────────────────────────────────────────────────────┘

        ┌───────────────────────────────────────────────────────────────┐
        │  NUNO'S PRODUCTION BOX  ★ OUTSIDE THE POOL — OUT OF SCOPE       │
        │  Existing Windows host: Wayond + ti_signals MT5 runtimes,      │
        │  signal bridge, AUTO_DEMO. NOT brokered, NOT provisioned,      │
        │  NOT reachable by pool WinRM/RDCB. Untouched by this design.   │
        └───────────────────────────────────────────────────────────────┘
```

#### Labelled paths

| # | Path | Protocol / port | Direction | Notes |
|---|------|-----------------|-----------|-------|
| 1 | **External browser access** | HTTPS/TLS **443** → RD Gateway | Internet → pool | The **only** externally reachable path into the Windows pool. Browser RDP is tunnelled through Guacamole (VPS) and terminated at RD Gateway on the infra host. No RDP/3389 is ever internet-exposed. |
| 2 | **WinRM provisioning** | **WinRM 5986 (HTTPS)** | VPS backend → RDSH (internal) | Automated per-account provisioning of `guvfx_u_<uid>` identity + portable MT5 runtime. Internal management path only; never internet-exposed (satisfies "no public admin exposure"). |

#### Component placement

| Component | Placement | State |
|-----------|-----------|-------|
| Traefik (TLS), Next.js frontend, DRF backend, workers/queue, Postgres 16 | Existing **Ubuntu VPS** | **MEASURED** — unchanged |
| Guacamole (guacd + webapp) | Existing **Ubuntu VPS** — the browser access layer; brokers browser→RDP sessions via RD Gateway | **MEASURED** — unchanged |
| RD Gateway (443 ingress terminator), RD Web Access | **Infra host** (new) | ESTIMATE — design only |
| **RDCB (RD Connection Broker)** | **Infra host** — **decision point (item 9)** | ESTIMATE — INCLUDE vs DEFER unresolved |
| RDSH #1 / RDSH #2, each with per-user `guvfx_u_<uid>` non-admin identities and per-account portable MT5 runtimes | **Windows RDS pool** (new) | ESTIMATE — design only, NOT procured |
| Wayond + ti_signals runtimes, signal bridge, AUTO_DEMO | **Nuno's production box** | **MEASURED** — explicitly OUTSIDE the pool, out of scope |

#### Open decision points (for Nuno)

- **Item 9 — RDCB include/defer.** With only 2 RDSH, session brokering can be deferred: RD Gateway can route directly to a fixed host per user, or a lightweight per-user host affinity map can live in the backend. **INCLUDE RDCB** buys proper load-balancing, session reconnect, and drain-for-maintenance semantics at the cost of another Windows role on the SPOF host and additional licensing. **DEFER RDCB** keeps the footprint minimal for a 2-host beta but hard-codes host affinity and loses graceful session reconnect. This diagram places RDCB on the infra host *if included*; the routing arrow degrades to direct-to-RDSH *if deferred*. **No default is assumed — Nuno decides.**
- **Item 10 — infra-host SPOF.** RD Gateway + RD Web (+ RDCB if included) co-reside on one infra host. Loss of that host severs all external browser access to the pool. This is an accepted, flagged risk for beta scale; mitigation (second gateway / HA broker) is **out of scope for this planning packet** and would require its own approved decision. The SPOF is documented here, not resolved here.

#### Consistency note vs §18

**Reconciled against the authoritative §18 (Architecture doc) on 2026-07-20 — consistent.** §18 places Traefik + Frontend + Backend + workers + Guacamole + Postgres on the unchanged Ubuntu VPS; the infra host running **AD DS · RDCB · RDGW · RDWeb · RDS Licensing**; a 2–3-host RDSH pool with `guvfx_u_<uid>` non-admin identities and per-account NTFS-isolated portable MT5 runtimes; and Nuno's production box explicitly outside the pool — all as drawn above. This version **refines** §18 for the Phase-2 decisions: (a) the single external path is explicitly labelled RDGW/443 fronted by Guacamole on the VPS; (b) the WinRM provisioning path (backend → RDSH) is called out as internal-only; (c) RDCB is presented as an unresolved include/defer decision (item 9) rather than assumed present; (d) the infra-host SPOF is named (item 10); (e) the per-account portable path is `D:\GuvFX\users\<uid>\accounts\<id>\mt5\` per §7. Note the infra-host box above abbreviates to the externally-relevant roles; **AD DS and RDS Licensing also reside on the infra host** (see the coexistence analysis, item 8).

---

## 3. Supplier-backed procurement comparison

> Six routes researched live (2026-07-20). Each brief tags figures **MEASURED** (confirmed on a
> public/authoritative page), **ESTIMATE** (derived), or **quote-required** (no confirmable public price).
> **Key finding:** only **AWS** publishes a confirmable per-user RDS access price for the approved EC2
> RemoteApp architecture ($10/user/mo, verified); the cheaper routes (Hetzner, SPLA-VPS) publish **no**
> RDS SAL/CAL price — the single most load-bearing licensing line — so they are **blocking** without a
> written quote. Azure AVD needs no RDS CAL but is a **different architecture** (not RDSH/RemoteApp).

### OVH (incumbent)

**What it is:** Host A — the existing app VPS (`guvfx-prod`). Ubuntu 25.04, OVH Milan region, ~193 GB disk (49% used), running all 11 containers + the Guacamole stack. This is a **Linux control-plane host** already in production; it is **sunk/already-incurred spend shown for total-cost context, not new beta cost** (source: `OPERATIONS_DASHBOARD.md` §1; capacity doc §5, §18).

| Item | Figure | Basis / status |
|---|---|---|
| Monthly cost (incumbent VPS) | **quote-required (indicative) — ~$40–90/mo** | **ESTIMATE / DOWNGRADED.** Not confirmable on any public OVH page; the true figure is on Nuno's OVH invoice. See notes. |
| Microsoft / RDS licensing on this line | **$0 — none** | MEASURED-by-design: Ubuntu Linux host. **No Windows Server, no RDS SAL/CAL.** No SAL/CAL conflation present on this line. |
| Currency | USD ($) as written in source | **Unstated FX basis** — OVH bills EU/Milan in **EUR**; the `$` band carries an undocumented conversion. |
| Tax basis (ex-VAT vs incl-VAT) | **NOT STATED in source → treat as ex-VAT, unconfirmed** | OVH EU business invoices are ex-VAT with VAT added at checkout; the `$40–90` band does not declare which it is. Must be stated. |
| Backups / snapshots | Not separately itemised on this line | Capacity doc lists new backups ($10–30) under *new* infra, not the incumbent line. |
| Materiality to beta decision | **Low** | Sunk cost; not part of the new-Windows-infra procurement being approved. |

**Verdict:** **Retain as-is; do not treat the dollar figure as a fact.** The OVH incumbent line is a pre-existing Linux control-plane cost, correctly excluded from new beta spend. Its one load-bearing number (~$40–90/mo) is an internal ESTIMATE that **cannot be confirmed against any public OVH page** and is therefore downgraded to **quote-required (indicative)**; replace it with the actual OVH invoice line (with region, plan/generation, EUR amount, and ex-VAT basis) before it appears in any approval total. The line is **clean of Windows/RDS SAL/CAL conflation** — that risk lives entirely on the *new* Windows RDSH lines, not here. **Governance: this is planning only — authorises no procurement, no purchase, no licence buy. Onboarding stays CLOSED; Nuno's existing host/MT5/broker/Guacamole/strategies/routing/lot-sizes/AUTO_DEMO are untouched and out of scope.**

**Verification notes:**

- **Confirmed (public pages):** OVH's current worldwide VPS list tiers are VPS-1 $4.54 / VPS-2 $8.50 / VPS-3 $12.32 / VPS-4 $23.37 per month (2/4/6/8 vCore; 4/8/12/24 GB; 40/75/100/200 GB NVMe). A price adjustment took effect **1 April 2026** (legacy Comfort/Elite/Value/Starter/Essentials families were excluded from the hike). The worldwide USD pages **do not state ex-VAT vs incl-VAT**; only the UK page shows both (e.g. £6.29 ex-VAT / £7.55 incl-VAT).
- **Downgraded:** the incumbent **~$40–90/mo** figure → **quote-required (indicative)**. Reasons: (1) the source labels it ESTIMATE, not a quote; (2) the specific plan/generation is not identified, and the **193 GB disk matches no current standard tier** (40/75/100/200 GB), so it is likely a legacy or optioned config whose price is not on today's list pages; (3) $40–90 is *above* a standard VPS-4 list price ($23.37), implying added options (backups/snapshots/extra IP/older-gen pricing) that only the invoice reveals; (4) OVH's April-2026 repricing means any recalled figure may be stale.
- **No fabrication / no conflation found:** the line does **not** conflate Windows Server licensing with RDS SAL/CAL — it carries **zero** Microsoft licensing (Linux host). That is correct and should stay explicit so the new-infra Windows/SAL lines are audited separately.
- **ex-VAT vs VAT:** **not stated in the source** and must be added. Recommend expressing the incumbent as the actual **EUR ex-VAT** invoice amount (VAT shown separately), not a USD band.
- **Residual price risk:** LOW materiality (sunk cost, excluded from new beta spend) but the displayed band is **unverifiable and directionally soft** — could be lower (if a plain VPS-4) or driven by options. Action: substitute Nuno's real OVH invoice line before any approval total is struck.

Sources: [OVH VPS (worldwide)](https://www.ovhcloud.com/en/vps/), [OVH VPS US](https://us.ovhcloud.com/vps/), [OVHcloud/Hetzner 2026 price increases](https://blog.cdnsun.com/ovhcloud-and-hetzner-2026-hosting-price-increases-explained/), [OVH UK Cloud VPS (ex/incl-VAT)](https://www.ovhcloud.com/en-gb/vps/vps-cloud/).

### Hetzner

**Vendor pricing brief — GuvFX Beta Option A RDS host pool (4 vCPU / 16 GB / SSD Windows Server hosts + per-user RDS licensing).**
**Status: PLANNING / DESIGN ONLY — authorises no procurement, no purchase, no paid server, no licence buy. All figures are list-price ESTIMATES for approval context, not quotes. Onboarding stays CLOSED; Nuno's production estate is out of scope.**
**All prices below are ex-VAT (Hetzner states "prices above do not include VAT"); German VAT (19%) or reverse-charge applies at invoice depending on Nuno's billing entity. Currency EUR primary, Hetzner-published USD shown where given.**

| # | Line item (maps to §D BoM) | Hetzner product | List price (ex-VAT) | Confidence | Notes / flag |
|---|---|---|---|---|---|
| 1 | RDSH host ×1 (4 vCPU / 16 GB / SSD) — **compute only** | Cloud **CCX23** (4 dedicated vCPU, 16 GB, 160 GB NVMe) | **≈ €24.49 / mo** | **quote-required (indicative)** | Confirmed only on third-party trackers, **not** on Hetzner's own live pricing page (figures did not render); an April 2026 increase is referenced. Confirm on hetzner.com/cloud before use. |
| 2 | Infra host ×1 (4 vCPU / 16 GB / SSD) | Cloud **CCX23** (as above) | **≈ €24.49 / mo** | **quote-required (indicative)** | Same caveat as line 1. |
| 3 | **Windows Server licence — on Hetzner Cloud** | *(none available)* | **N/A** | **CONFIRMED (blocking)** | Hetzner Cloud provides **no** Windows licence and **no** SPLA: *"The installation must be done manually"* and the customer must **bring their own eligible licence (BYOL)**. Lines 1–2 as priced are **Linux/BYOL only** — a Cloud CCX "with Windows SPLA" line is fabricated; that product does not exist. |
| 4 | Windows Server 2025 **Standard** licence — on **Robot dedicated** only | Windows Server 2025 Standard add-on (per **core**) | **8-core €27.90 / $31; 16-core €55.90 / $63 per mo** | **CONFIRMED** | Verified on Hetzner docs (new prices from 1 Jan 2025, ex-VAT). **Per-core, dedicated-server only** — not attachable to a 4-vCPU Cloud VM. Requires renting a **Robot dedicated server** (separate hardware cost, line 5). |
| 5 | Dedicated server hardware (to host line 4) | Robot dedicated (e.g. AX/EX line) | **quote-required (indicative)** | **quote-required (indicative)** | Not spot-checked to a specific model this session; dedicated hardware is per-month per-box and starts well above the 4-vCPU cloud spec (min core counts are 6–8+). Materially changes the §D per-host math. |
| 6 | **RDS Per-User SAL / RDP CAL ×5** (the §D core licensing line) | Hetzner "Windows Server Remote Desktop Services" | **no public price** | **quote-required (blocking)** | Hetzner docs: *"You can order additional RDP licenses (on a per user basis) by writing a support request."* **No public per-user price exists.** Cannot be confirmed on any public page → downgraded. **The Windows Server Standard licence (line 4) is NOT an RDS SAL/CAL** — Standard grants only 2 administrative RDP sessions; multi-user RemoteApp needs these separate per-user RDP licences. |
| 7 | TLS / RD Gateway | Let's Encrypt via existing Traefik edge (§18) | **€0** | CONFIRMED (design) | Unchanged from §D; no Hetzner cost. |
| 8 | Backups / snapshots | Hetzner Cloud Backups (20% of server price) / snapshots | **quote-required (indicative)** | **quote-required (indicative)** | Hetzner backup add-on is percentage-of-server; not spot-verified this session. |

**Verdict.** **DO NOT treat Hetzner as a drop-in for the §D "SPLA / cloud-rented, RDS Per-User SAL ×5" model.** Two independent blocking findings:

1. **Hetzner Cloud carries no Windows licensing at all (BYOL/manual only).** The cheap CCX line-items (€24.49/mo) are Linux/bring-your-own-licence — pricing them "with Windows" is a conflation, not a real Hetzner product.
2. **Hetzner offers Windows Server licensing only on Robot *dedicated* servers, per-core, and publishes *no* RDS/RDP per-user (SAL/CAL) price** — that must be requested from support. So the single most load-bearing §D line (5 × RDS Per-User SAL, the elastic-per-user premise) **cannot be validated against any Hetzner public page.**

Hetzner is viable **only** as a **Robot-dedicated + per-core Windows Standard** host where GuvFX **supplies its own RDS Per-User CALs** (owned-licence path), which contradicts the §D "SPLA / no capital / elastic per-user" recommendation. For an SPLA per-user RDS SAL model, a Microsoft SPLA-licensed provider (Azure, or an SPLA reseller/CSP) fits; Hetzner does not. **Recommend: keep §D on the provider-agnostic SPLA band, and if Hetzner is pursued, obtain a written quote for (a) a specific Robot dedicated model, (b) per-core Windows Standard, and (c) per-user RDP CAL count — none of which is a published, elastic price.**

---

**Verification notes:**

- **Confirmed on a Hetzner public page (CONFIRMED):**
  - Windows Server 2025 **Standard** per-core monthly, **ex-VAT**: 8-core **€27.90 / $31.00**, 16-core **€55.90 / $63.00** (also 10/12/14/18/24/32/48-core tiers). Source: `docs.hetzner.com/robot/general/pricing/windows-2025-pricing/` — page explicitly states *"The prices above do not include VAT."*
  - Hetzner **Cloud provides no Windows licence** — BYOL, manual install, no SPLA. Source: `docs.hetzner.com/cloud/servers/windows-on-cloud/`.
  - **RDS/RDP is per-user, by support request, no published price.** Source: `docs.hetzner.com/robot/dedicated-server/windows-server/windows-server-2025/`.
  - Windows Server licensing is **dedicated-server (Robot) only**, not a Cloud add-on. Sources: Hetzner docs (Robot Windows Server 2025; Windows-on-Cloud).

- **Downgraded to quote-required (indicative) — could NOT confirm on Hetzner's own live page:**
  - **CCX23 ≈ €24.49/mo** and CCX33 ≈ €48.49/mo: appeared only on third-party trackers (costgoat, comparedge) and a Hetzner press release; Hetzner's own `/cloud/` pricing table did not render numeric figures this session, and an **April 2026 price increase** is referenced but unverified line-by-line. Also a conflicting USD figure ($31.99 vs €24.49) appeared. Treat all CCX monthly numbers as indicative pending confirmation on hetzner.com/cloud.
  - **Robot dedicated hardware price** (line 5) and **backup add-on** (line 8): not spot-checked to a model this session.
  - **RDS Per-User RDP licence price** (line 6): no public figure exists at all — must be quoted by Hetzner support.

- **Fabrication / staleness / conflation flags raised:**
  - **Conflation (primary):** any brief line that treats "Windows Server Standard licence" as covering multi-user RDS is wrong — Standard = 2 admin RDP sessions; **RDS SAL/CAL is a distinct, separately-requested, unpriced item** at Hetzner.
  - **Fabrication risk:** a "Hetzner Cloud CCX + Windows SPLA per-user SAL" bundle **does not exist as a purchasable product** and should not appear as a priced line.
  - **Staleness:** Windows per-core prices are dated "new from 1 Jan 2025" (still current as read July 2026); CCX cloud prices are mid-2026 and subject to the noted April 2026 change — reconfirm before any approval.

- **Residual price risk:**
  - The €24.49/mo CCX figure is the anchor for the whole compute cost and is **third-party-sourced only** — a real increase would move the §D infra subtotal.
  - The **RDS per-user cost is entirely unknown** (support-quote only); §D's "$4–7/user/mo SAL" band **cannot be attributed to Hetzner** and may not be achievable there at all.
  - The Cloud-vs-Dedicated split means a Hetzner Windows/RDS deployment likely costs **materially more** than the Linux CCX list price, because it forces the dedicated-server + owned-CAL path rather than the assumed elastic SPLA path.

**Governance reaffirmation.** This is planning input for Nuno's §D approval only. It authorises no procurement, no VM/licence purchase, and no Phase-2+ implementation. Onboarding stays CLOSED; `can_deploy_automation` stays False; Nuno's Windows host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO are untouched and excluded.

Sources: [Hetzner Windows 2025 pricing](https://docs.hetzner.com/robot/general/pricing/windows-2025-pricing/) · [Windows on Cloud (BYOL)](https://docs.hetzner.com/cloud/servers/windows-on-cloud/) · [Robot Windows Server 2025 / RDS-by-request](https://docs.hetzner.com/robot/dedicated-server/windows-server/windows-server-2025/) · [Hetzner Cloud](https://www.hetzner.com/cloud) · CCX figures (indicative): [comparedge](https://comparedge.com/tools/hetzner/pricing), [costgoat](https://costgoat.com/pricing/hetzner)

### Microsoft Azure

*Scope: Windows session-host hosting for a small RDS/RemoteApp-style host pool (Option A). Planning only — authorises no procurement. All prices **ex-VAT**; UK VAT (20%) is additional. Azure list prices are pay-as-you-go (PAYG), region-specific, and change without notice.*

| Item | Figure | Basis | Confidence |
|---|---|---|---|
| Session-host VM — D4s v5 (4 vCPU / 16 GB), **Linux** on-demand, US region | $0.192 / hr (USD, ex-VAT) | Third-party mirror of Azure list (Vantage) | MEASURED (mirror, not Azure's own page) |
| Same VM with **Windows** OS (adds Windows licensing uplift ~$0.046/vCPU/hr) | ≈ $0.37–0.38 / hr (USD, ex-VAT) | Uplift arithmetic, not confirmed on a public page this pass | quote-required (indicative) |
| Windows D4s v5, monthly compute (~730 hr, PAYG, US region) | ≈ $270–280 / mo (USD, ex-VAT) | Derived from the indicative hourly | quote-required (indicative) |
| AVD control plane / broker / gateway (management) | $0 (no charge) | Microsoft AVD pricing model | MEASURED |
| AVD per-user **external** access licence | ≈ $5 / user / mo (USD, ex-VAT) | Could not confirm on page (fetch timed out) | quote-required (indicative) |
| AVD per-user access for M365 E3/E5, Business Premium, Windows E3/E5 users | Included, $0 incremental | Microsoft Learn / MS Q&A | MEASURED |
| **RDS CAL / RDS SAL for AVD Windows multi-session** | **Not required** | Microsoft Learn | MEASURED |
| RDS SAL user-access right for **Windows Server**-based AVD (SPLA) | Retired **30 Sep 2025** | Microsoft Q&A / Learn | MEASURED |
| Managed OS disk, egress, backup, storage | Not quoted | — | quote-required |

**Verdict:** Azure is viable for the Option A host pool, but **no line in the original brief could be validated against its own citation because no brief text or source URLs were provided.** The only figures I can stand behind as measured are the *licensing-model facts*, not the *prices*. Treat all rate/monthly numbers as indicative pending a live Azure Pricing Calculator quote for the exact region (UK South vs US), VM size, disk, egress, and reservation term. Do not present any dollar figure here as a committed cost.

**Verification notes:**

- **Confirmed (MEASURED):**
  - AVD running Windows 10/11 **multi-session does not require RDS CALs** — it does not use the Windows Server RD Session Host role (Microsoft Learn / MS Q&A).
  - AVD **per-user access is included at no extra charge** for M365 E3/E5, A3/A5, F3, Business Premium, and Windows 10/11 Enterprise E3/E5.
  - The **RDS SAL** user-access right for *Windows Server*-based AVD was available to SPLA partners only and **ended 30 Sep 2025** — after which Windows Server RDS in Azure needs Windows Server + RDS CALs with Software Assurance.
  - AVD's **management/control plane is free**; you pay only for session-host VMs, storage, networking.
  - D4s v5 **Linux** on-demand ≈ **$0.192/hr (USD, ex-VAT, US region)** on a third-party Azure-list mirror.

- **Downgraded to quote-required (indicative):**
  - Windows D4s v5 hourly and monthly compute — could not confirm the Windows uplift on a public Azure page in this pass; the ~$0.37–0.38/hr and ~$270–280/mo figures are arithmetic, not a quoted rate.
  - AVD external per-user access price (~$5/user/mo) — the Azure AVD pricing page timed out on fetch; unconfirmed.
  - Region is unresolved: the measured Linux anchor is a **US** price in **USD**; a UK deployment (UK South) prices in **GBP** and differs. Do not mix them.

- **Conflation flags:**
  - Any brief line that attaches **RDS SAL/CAL cost to an AVD Windows multi-session** design is **wrong** — that model needs no RDS CAL. RDS SAL/CAL applies only to a **Windows Server RD Session Host** design, which is a different (and, for SAL, now-retired) path. Windows Server *base* licensing (bundled in the VM's Windows rate) must not be conflated with **RDS** SAL/CAL user rights — they are separate charges.

- **Residual price risk:**
  - All prices ex-VAT; UK VAT adds 20%.
  - PAYG rates shown; 1-yr/3-yr Reserved Instances or savings plans materially lower compute cost and are not modelled here.
  - Region/currency mismatch (US/USD anchor vs likely UK/GBP deployment).
  - Storage, egress, backup, and per-user M365/Windows E3 subscription costs are **not** in these figures and can dominate total cost.
  - Third-party mirror ≠ Azure's own committed quote; only the live Azure Pricing Calculator is authoritative.

Sources: [Azure Virtual Desktop pricing](https://azure.microsoft.com/en-us/pricing/details/virtual-desktop/), [RDS CAL licensing (Microsoft Learn)](https://learn.microsoft.com/en-us/windows-server/remote/remote-desktop-services/rds-client-access-license), [RDS SAL for Windows Server AVD (MS Q&A)](https://learn.microsoft.com/en-us/answers/questions/2139376/rds-sal-user-access-right-for-azure-virtual-deskto), [D4s v5 pricing (Vantage)](https://instances.vantage.sh/azure/vm/d4s-v5)

### AWS

**Scope:** planning-only realization of the Option A RDS host pool on AWS EC2 (1 Windows infra host + 2 RDSH session hosts + 5 RDS per-user licences). Region reference: **us-east-1**. All prices **ex-VAT, USD, on-demand** unless noted. **No procurement authorised.**

| Item | Qty (beta) | AWS realization | Unit price (ex-VAT) | Status |
|---|---|---|---|---|
| Infra host (DC+RDCB+RDGW+RDWeb+Licensing), 4 vCPU / 16 GB | 1 | EC2 `m5.xlarge`, Windows License-Included | **Linux $0.192/hr = $140.16/mo (MEASURED)**; **Windows ~$0.376/hr ≈ ~$275/mo (indicative — Windows adder not confirmed on a public page)** | quote-required (indicative) for the Windows figure |
| RDSH session host, 4 vCPU / 16 GB | 2 | EC2 `m5.xlarge`, Windows License-Included | ~$275/mo on-demand each (indicative); **~$100–150/mo each only with 1–3 yr Reserved/Savings Plan** | quote-required (indicative) |
| RDS per-user licence (Remote Desktop Services) | 5 | **AWS-provided EC2 RDS SAL** (per-user, per-month, via License Manager user-based subscriptions) — **not SPLA, not bundled in EC2** | **$10.00/user/mo → $50/mo for 5 (MEASURED — AWS Marketplace listing, verified 2026-07-20; cancel-anytime, elastic)**. *(NB: the $4.19 figure is the Amazon **WorkSpaces** bundle SAL — a different service — not the standalone EC2 License-Manager rate.)* | confirmed |
| RDS User CAL — *owned alternative* | 5 | Not an AWS SKU. Must be **RDS User CAL with Software Assurance**, brought via **License Mobility** (plain RDS CALs are **not** Mobility-eligible) | ~$140–160 one-off (Microsoft/reseller VL list, ex-VAT) | quote-required (indicative) — Microsoft price, not AWS |
| Windows Server Std — *owned alternative* | per host | Not an AWS SKU (EC2 is License-Included or BYOL on Dedicated Hosts) | ~$900 one-off (Microsoft/reseller VL list, ex-VAT) | quote-required (indicative) — Microsoft price, not AWS |
| EBS storage (80–120 GB gp3) | per host | gp3 | ~$8–12/mo per host (indicative) | quote-required (indicative) |
| Data egress / backup snapshots | — | EC2 egress + EBS snapshot | ~$10–40/mo (indicative, usage-dependent) | quote-required (indicative) |

**Indicative monthly OPEX (beta, 3 Windows hosts + 5 RDS SAL, ex-VAT):**
- **On-demand:** ~$825/mo compute (3 × ~$275) + **$50/mo SAL (5 × $10)** + ~$30–75/mo storage/egress/backup ≈ **~$915–955/mo**.
- **With 1–3 yr Reserved/Savings Plan on the hosts:** ~$300–450/mo compute + $50 SAL + storage ≈ **~$450–630/mo**.

**Verdict:** **NOT ready to purchase — planning estimate only.** The RDS-SAL line is confirmed and cheap; the load-bearing cost is the three Windows hosts, and the previously-cited "$80–160/mo per Windows host" is **not achievable on AWS on-demand** (real ~$275/mo) — only under a multi-year commitment. Two prior conflations are corrected: (1) EC2 "License-Included" covers the **Windows Server OS + 2 admin RDP sessions only** — it does **not** include RDS SAL/CAL, which is a separate per-user charge; (2) "RDS User CAL / Windows Server Std one-off" are **Microsoft VL prices, not AWS SKUs**. A binding AWS quote (region, instance family, commitment term, single- vs multi-session SAL) is required before any figure here is treated as fact. **Recommend AWS-provided RDS SAL (monthly, elastic) over owned CALs for beta.**

**Verification notes:**
- **Confirmed (MEASURED against public pages):**
  - `m5.xlarge` = 4 vCPU / 16 GB, Linux on-demand **$0.192/hr = $140.16/mo**, us-east-1 (Vantage `instances.vantage.sh`, Economize).
  - **AWS-provided EC2 RDS SAL is a real per-user/per-month product: $10.00/user/mo** via AWS License Manager user-based subscriptions (AWS Marketplace listing "Win Remote Desktop Services SAL", **verified 2026-07-20**; cancel-anytime, no proration stated). The **$4.19/$6.42** figures belong to the **Amazon WorkSpaces** bundle SAL (a different, managed-desktop service) and were mis-attributed to EC2 in an earlier draft — corrected here.
  - EC2 Windows "License-Included" bundles the **Windows Server OS + only 2 admin RDP connections**; running the **RDS role requires separate SAL/CAL** — the OS licence does **not** cover it (AWS Microsoft-licensing docs, AWS Windows FAQ).
  - **RDS CALs are not License-Mobility-eligible**; only **RDS User CALs with Software Assurance** can be brought to AWS via License Mobility (AWS Prescriptive Guidance / Microsoft licensing on AWS).
- **Downgraded to quote-required (indicative):**
  - **Windows host monthly price** — could not confirm the exact Windows on-demand rendered figure on a public page this session; the ~$0.376/hr (~$275/mo) is the standard Linux-plus-Windows-adder estimate, not a page-confirmed number.
  - Prior **"$80–160/mo per Windows host"** — stale/optimistic for AWS; flagged as achievable only with Reserved/Savings-Plan commitment.
  - **"RDS User CAL ~$140–160" and "Windows Server Std ~$900"** — Microsoft/reseller VL list prices, **not AWS SKUs**; kept as indicative and re-labelled.
  - EBS storage, egress, and backup lines — usage-dependent estimates, not quoted.
- **VAT:** all AWS prices above are **ex-VAT, USD, us-east-1**. AWS UK/EU invoices add **UK VAT (currently 20%)**; Microsoft VL figures are likewise **ex-VAT**. No figure here includes VAT.
- **Residual price risk:** (1) Windows host cost dominates and is only confirmed at the Linux base — the Windows adder must come from a live AWS quote; (2) on-demand vs 1–3 yr commitment swings total OPEX ~2×; (3) the EC2 RDS SAL is a flat **$10/user/mo** (verified) — do **not** substitute the WorkSpaces $4.19/$6.42 bundle rate; (4) region other than us-east-1 shifts all compute/storage figures; (5) the owned-licence path additionally depends on Software Assurance + License Mobility eligibility, not just the CAL sticker price.

**Governance:** planning for approval only — authorises no procurement, no paid server, no licence buy, no architecture-dependent implementation. Onboarding stays CLOSED; Nuno's existing Windows host / MT5 runtimes / broker accounts / Guacamole / strategies / routing / lot sizes / AUTO_DEMO are untouched and out of scope.

### SPLA-inclusive VPS (Kamatera / Vultr / Cloudzy)

*Planning only — authorises no procurement, no purchase, no licence buy. Prices as of July 2026; USD; treat all as **ex-VAT / ex-tax** unless a signed quote states otherwise. "SPLA-inclusive" below is scoped strictly to the **Windows Server OS licence** — see the RDS caveat, which is the load-bearing correction.*

| Provider | Base VPS (entry, indicative) | Windows Server OS licence | RDS SAL / CAL (multi-user RemoteApp) | Verification status |
|---|---|---|---|---|
| **Kamatera** | from ~$4/mo (Linux); Windows tiers ~$8.79–$35.19/mo | **Included — no premium over Linux** (MEASURED, official page) | **NOT included** — "Remote Desktop access requires an additional licence" (MEASURED, official page) | Windows-included: **confirmed**. Tier $ figures: **quote-required (indicative)** — from third-party reviews, not Kamatera's own page |
| **Vultr** | Cloud Compute from ~$5–$10/mo (Linux) | **NOT in base price** — added per compute plan (MEASURED, Vultr docs). Fee ~$16/mo on the $10 plan is **third-party-sourced only** | **Not offered / not mentioned** — Vultr docs make no reference to RDS/SAL/CAL | License-is-extra: **confirmed** (Vultr docs). $16 figure: **quote-required (indicative)** — Vultr's own pricing pages returned 403, unverifiable on-source |
| **Cloudzy** | Windows tiers $3.48 / $7.48 / $14.48 / $26.48/mo (**50%-off promotional**; non-promo ≈ 2×) | **Trial licence or BYOL** — **not a full bundled SPLA licence** (MEASURED, official page) | **NOT included** — markets "full administrator RDP" (2-session admin cap), no RDS/SAL/multi-user (MEASURED) | Prices: **confirmed on-source** but **promotional**. "SPLA-inclusive" label: **contradicted** — it is trial/BYOL |

**Verdict:** For **Option A (a multi-user Windows RDS / RemoteApp host pool serving several beta users)**, none of these three is "SPLA-inclusive" in the sense the heading implies. At most they bundle the **Windows Server OS licence** (Kamatera: yes; Vultr: no, it's an add-on; Cloudzy: trial/BYOL only). **All three omit the RDS Subscriber Access Licence (SAL) / RDS CAL** that a licensed concurrent-multi-user RemoteApp host legally requires — the OS licence only grants **2 administrative RDP sessions**. RDS SAL is a **separate per-user/month line item** (obtainable via a Microsoft SPLA hoster) that **none of these three quote publicly**, so the true per-user cost of Option A on any of them is **unquoted and must be obtained in writing before any BoM is approved**. Recommend: proceed to **written quotes** (Kamatera + a genuine SPLA/RDS-SAL hoster) rather than treating any headline VPS price as the licensed cost.

**Verification notes:**

- **Confirmed on official source:** Vultr docs — Windows licence is **not** in the base price and is an added fee per compute plan; no RDS/SAL/CAL mention. Kamatera — Windows OS licence **included, no premium over Linux**, and **Remote Desktop "requires an additional licence."** Cloudzy — Windows VPS ships with a **trial licence or BYOL** (not a bundled full licence), markets **admin RDP only**, prices are **50%-off promotional** ($3.48/$7.48/$14.48/$26.48).
- **Downgraded to quote-required (indicative):** Vultr's **~$16/mo Windows fee** — sourced only from third-party aggregators; Vultr's own pricing pages returned HTTP 403 and could not be verified on-source. Kamatera's **$8.79–$35.19/mo Windows tier** figures and the "$15–25/mo Windows adds" claim — from third-party reviews, and the latter **conflicts** with Kamatera's official "no premium for Windows" statement. Any RDS SAL per-user price for all three — **not published**.
- **Fabrication / conflation flags:** (1) The heading's premise that these are "**SPLA-inclusive**" conflates **Windows Server OS licensing** with **RDS SAL/CAL**; a Windows-included VPS is **not** a licensed multi-user RemoteApp host. (2) **Cloudzy is mislabelled** — trial/BYOL is not SPLA-inclusive. (3) **Cloudzy prices are promotional** (50% off); a BoM using them understates ongoing cost by ~2×.
- **VAT / tax:** None of the three pages I could read state VAT treatment; US-listed prices are conventionally **ex-tax**. Treat every figure above as **ex-VAT** pending a quote.
- **Residual price risk:** **High.** The single largest cost driver for Option A — **RDS SAL per user/month** — is unpriced by all three vendors and is the item most likely to change the architecture's economics. Entry base-VPS figures also carry provider-page-block risk (Vultr) and promo-expiry risk (Cloudzy). No number here should enter an approved BoM without a signed, VAT-stated quote that itemises the RDS SAL.

Governance: planning only — no procurement, purchase, licence buy, or architecture-dependent code authorised; onboarding remains CLOSED; existing Windows host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO untouched.

### Windows + RDS licensing routes & compliance

> **PLANNING / FOR APPROVAL ONLY — authorises NO procurement, NO purchase, NO paid server, NO licence buy.** Onboarding stays CLOSED; Nuno's existing Windows host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO are untouched and out of scope. All figures are **ESTIMATE** unless marked otherwise. SPLA/SAL pricing is provider-quoted and **not** a Microsoft public list price — treated as **quote-required (indicative)**. **All prices are USD, ex‑VAT** (Microsoft US MSRP basis). A UK/EU deployment adds VAT (UK 20%) on top; SPLA EU quotes are typically stated "+ VAT".

**Two mutually-exclusive licensing routes** (you do not mix them — SPLA is all-rented; owned/volume is all-purchased):

| Route | Windows Server OS | Base Windows Server CAL (per user) | RDS access licence (per user) | Line-item basis | Verified? |
|---|---|---|---|---|---|
| **A — SPLA / cloud-rented** *(recommended for beta)* | Rented monthly, **bundled in the SPLA cloud VM price** (~$80–160/mo/VM, provider-set) | **Bundled in the SPLA VM/host price** (SPLA Windows Server SAL — do not double-count) | **RDS SAL ≈ $4.50–7 / user / mo** | Monthly OPEX, provider-reported; **all** AD-enabled users counted (SPLA = per-authorised-user, **not** concurrent) | RDS SAL band **CONFIRMED** ($4.50 US / €7.90+VAT EU public data points); VM/host price **quote-required (indicative)** |
| **B — Owned / Volume Licensing** | **Windows Server 2022/2025 Standard ≈ $1,069 MSRP / 16-core VM** (was "$900" — corrected up) × 3–4 VMs | **Windows Server User CAL ≈ $40 / user** — *required in addition to the RDS CAL; previously omitted* | **RDS Per-User CAL ≈ $110–160 one-off / user** (wide vendor spread; per-CAL $94 in 5-packs up to $220 single) | One-off capex + VM hosting | Windows Server MSRP **CONFIRMED $1,069** (Microsoft); RDS CAL band **CONFIRMED but wide**; base WS CAL **CONFIRMED ~$40** |

**Corrected all-in beta bands (5 users, 1 infra VM + 2–3 RDSH = 3–4 Windows VMs):**

| Model | One-off | Monthly | Note |
|---|---|---|---|
| **A — SPLA (recommended)** | ~$0 | **~$350–700/mo** (3–4 SPLA VMs @ $80–160 + 5 × RDS SAL @ $4.50–7) | Internally consistent; elastic; prove density first. **Quote-required.** |
| **B — Owned** | **~$4.2–5.5k one-off** (3–4 × WS Std @ ~$1,069 + 5 RDS User CALs @ ~$110–160 + 5 base WS CALs @ ~$40) + VM hosting | VM hosting only | Corrected **up** from "$3.5–5.5k": prior low end used a stale $900 OS and **omitted base Windows Server CALs**. |

**Verdict:** The two-route structure and the **recommendation to start on SPLA/monthly** stand. Numbers are corrected: the Windows Server OS unit price was **understated** ($900 → $1,069 MSRP), the owned-route **omitted the mandatory base Windows Server CAL** (a genuine compliance gap — an RDS CAL alone does **not** grant the underlying Windows Server access right), and the owned one-off floor rises to **~$4.2k**. The RDS SAL and RDS CAL bands survive verification. **No figure here is a quote; all are ESTIMATE / indicative.** Nothing in this brief authorises procurement — the BoM + route choice remain Nuno's explicit gate.

**Verification notes:**

- **CONFIRMED (public page):** Windows Server 2022 Standard MSRP **$1,069 / 16-core** (Microsoft Windows Server pricing). RDS SAL SPLA **~$4.50/user/mo (US)** and **€7.90+VAT/user/mo (EU)** as representative provider prices (SAMexpert / provider listings). RDS User CAL public spread **~$94–$220/CAL** (5-pack $469.99 ≈ $94 → single-user $220), so the brief's per-CAL band is mid-range and defensible. Base Windows Server User CAL **~$40** (standard reseller list).
- **CORRECTED:** Windows Server OS unit **$900 → $1,069** (stale/understated). Owned-route one-off floor **$3.5k → ~$4.2k** after adding the omitted base Windows Server CALs. RDS CAL band widened to **$110–160** to reflect the real vendor spread.
- **DOWNGRADED to "quote-required (indicative)":** all **SPLA VM/host monthly prices** ($80–160/mo) and the **RDS SAL rate** — SPLA is confidential/reseller-set, has no Microsoft public list page, and varies by provider and region; the monthly all-in **$350–700/mo** is a derived estimate, not a quote.
- **Compliance flags raised:** (1) the original single "RDS Per-User CAL / SAL" row **conflated two distinct routes** (perpetual CAL vs SPLA SAL) — now separated. (2) **Windows Server licensing was conflated with RDS access licensing** — the owned route needs **both** a base Windows Server CAL **and** an RDS CAL per user; only the RDS CAL was priced. (3) SPLA counts **every authorised AD user, not concurrent sessions** — 5 provisioned beta users = 5 SALs regardless of how many are online. (4) **ex‑VAT vs VAT** was unstated — now fixed to ex‑VAT USD basis with a UK/EU VAT-on-top caveat.
- **Residual price risk:** SPLA rates move with provider and reporting tier and are unquoted here; the RDS CAL band is wide enough (~2×) to swing the owned one-off by ~$250 across 5 CALs; neither route price includes VAT, Software Assurance, TLS/backup, or the cloud VM compute itself, which dominates monthly OPEX. **These bands must be replaced by real reseller/provider quotes before any approval — none of these numbers is a purchase authority.**

Source note (repo figures audited): `docs/BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md` §16/§20 and `docs/BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md` §D. No literal "### Windows + RDS licensing routes & compliance" section exists in-repo; this brief reconstructs and corrects the licensing figures carried in those two docs' cost tables.

---

## 4. Control-plane analysis (procurement items 8–11)

### Control-plane role coexistence (procurement item 8)

**Question.** Can AD DS + RD Connection Broker (RDCB) + RD Gateway (RDGW) + RD Web Access (RDWeb) + RDS Licensing safely share ONE infra host (§D BoM item 2: **4 vCPU / 16 GB RAM / 80 GB SSD**, Windows Server 2022/2025) during a 5-user beta?

**One-line verdict.** **Yes — on resources comfortably; on the security/supportability axis conditionally, provided the RDSH pool stays off this host (already true by design) and the DC-hardening conditions below are met.** The binding constraint is **availability (SPOF), not capacity, and not co-hosting per se.**

> **Evidence status.** The infra host **does not exist yet** (zero provisioning telemetry — every `AccountRuntime` is `NOT_PROVISIONED`; §E). Therefore **every resource figure below is an ESTIMATE** — vendor-class typicals, consistent with §3.10 (base-OS/RDS ~3–4 GB are explicitly ESTIMATE, "not measured on our pool"). The **only MEASURED** facts are the app-side ones already in §3.10 (single MT5 terminal ~100–350 MB, bridge ~33 MB) — and those are **per-terminal**, i.e. they live on the **RDSH**, not on this infra host. Nothing here is a quote or a proven number.

---

#### 1. Per-role footprint — control-plane, per-login/session, NOT per-terminal

The defining property (per §3.9 "Infra-VM headroom", §D item 2): this host carries **per-login / session-broker / licensing / directory** load, which is a function of `active_users` (5) and login/reconnect rate — **not** `active_terminals`. MT5 terminals (the per-terminal RAM/CPU that drives density) run on the **RDSH pool**, never here. So the footprint barely moves between 5 and, say, 25 users.

| Role | What consumes resources | RAM (ESTIMATE, 5-user beta) | CPU profile |
|---|---|---|---|
| Windows Server base OS | kernel, services | ~1.5–2 GB | idle |
| **AD DS** (DC) | `lsass`/NTDS for a tiny directory (~5 users + a handful of groups) | ~0.3–0.7 GB | near-idle; auth spikes at login |
| **RDCB** | broker service + config store (WID/SQL) | ~0.3–0.8 GB | brief spike per session place/reconnect |
| **RDGW** | IIS + TS-Gateway tunnel state, per active 443 connection | ~0.2–0.5 GB | per-connection encrypt; scales with concurrent sessions, not terminals |
| **RDWeb** | IIS app pool (optional portal) | ~0.2–0.4 GB | trivial (feed refresh) |
| **RDS Licensing** | CAL/SAL issuance + tracking DB | ~0.1–0.2 GB | negligible |
| **Sum (roles + OS)** | | **≈ 3–4.5 GB typical** | near-idle with login-time bursts |

**Resource read on 4 vCPU / 16 GB / 80 GB SSD:** ≈ 3–4.5 GB against 16 GB leaves ~11–13 GB headroom — deliberately generous *because five roles are collapsed* (§D item 2 picks 16 GB, top of the §2 8–16 band, for exactly this reason). 4 vCPU absorbs the coincident login/broker/auth bursts of 5 users by inspection. 80 GB SSD holds OS + IIS + WID + the AD database + Licensing DB + logs with room to spare. **This host scales far past 5 users on RAM/CPU/disk; it becomes the ceiling only via availability, never capacity** (§3.9). SSD (not spinning) is still mandated (§D item 4) so WID/AD/IIS log flushes don't serialise.

---

#### 2. The DC co-hosting caveat (security + supportability)

Putting other roles on a **Domain Controller** is the real question, not raw capacity. A DC should be one of the most hardened, least-exposed machines in an estate; every additional role widens its attack surface and couples its patch/reboot lifecycle. Microsoft's guidance here (well-documented industry stance — **ESTIMATE / best-practice, not a measured claim**):

- **RD Session Host on a DC — the strong "don't".** Microsoft explicitly discourages co-locating **RDSH** with AD DS (security + performance: interactive session users on a DC is a serious exposure). **This design already avoids it entirely** — the RDSH pool is a *separate* set of hosts (§1–2, §18); no beta user ever gets an interactive session on the infra/DC host. **The single most-cited anti-pattern does not apply to us.** This is the most important point in the whole analysis.
- **RDGW on a DC — the genuine concern that remains.** RDGW is the **only internet-facing role** (RDP-over-TLS/443, fronted by the existing Traefik edge; §D item 5, §18). Terminating an externally reachable tunnel *on the DC VM* broadens the DC's exposed surface — best practice is to keep the internet-facing gateway off the DC (ideally in a perimeter segment). Mitigations already in the design: RDGW is the *sole* external surface, RDSH are not otherwise public (§13 layer 5), and it sits behind Traefik/TLS. Residual risk: a RDGW/IIS compromise is a compromise adjacent to `lsass` on the DC. **This is the role to split off first** (see §3).
- **RDCB on a DC — supportability wrinkle, not a security wall.** Co-location is supported for small/single-server deployments (Microsoft's own session-based single-server Quick Start even bundles Broker + Web on one box), but installing RDCB on a DC has known permissioning quirks (local-group / service-account handling differs on a DC) and couples the routing SPOF to the DC reboot cycle. Acceptable at 5 users; a candidate for HA split at scale.
- **RDS Licensing and RDWeb on a DC — low risk, commonly supported.** RD Licensing on a DC is explicitly documented as fine for small deployments; RDWeb is optional (Guacamole is the primary path, §5) and can be omitted to shave surface.

**Supportability, cross-cutting:** one VM means one reboot/patch window takes down identity + routing + gateway + licensing together, and a single snapshot must capture all of it consistently — which is precisely why §D item 6 makes the **daily infra-VM snapshot a hard P6 prerequisite**, and why an infra-VM loss is a **SEV-1 Red event** (§SEV-1): no DC → no new logins, no routing, no CAL issuance; already-running AUTO_DEMO terminals continue but **cannot safely relaunch mid-outage** (a fresh domain logon needs the DC). That DC-relaunch caveat is a direct consequence of collapsing AD DS onto the same SPOF.

---

#### 3. Safe to collapse for beta vs. split first at scale

| Role | Beta (5 users) | Split priority at scale | Why |
|---|---|---|---|
| **RDS Licensing** | ✅ collapse | last | Lightest, DC-co-host explicitly supported. |
| **RDWeb** | ✅ collapse (or omit) | last / drop | Optional; Guacamole is primary (§5). Omitting it *reduces* DC surface. |
| **RDCB** | ✅ collapse | **2nd** (HA broker) | Routing SPOF; HA pair + shared SQL removes it (§17/§6). |
| **AD DS (DC)** | ✅ collapse (it *is* the host) | **redundancy, not split** | Add a **2nd DC** to kill the identity SPOF + the DC-relaunch caveat (§SEV-1 HA path). |
| **RDGW** | ⚠️ collapse *with hardening* | **1st to split** | Only internet-facing role on the DC — highest-value surface to move off first (perimeter RDGW / RDGW farm behind Traefik LB, §6/§17). |

Ordering matches §6/§17's post-beta HA table, re-ranked here by *risk-to-split-first*: **RDGW (security/internet-facing) → RDCB (routing HA) → 2nd DC (identity/availability) → RDGW farm+LB as `active_users` outgrows one 443 path (§3.8).** None of this is scheduled or authorised — it is design-only (§17), triggered by the SPOF/availability limit, **not** by any capacity limit on this host.

---

#### 4. Verdict + conditions

**SAFE to collapse AD DS + RDCB + RDGW + RDWeb + RDS Licensing onto one 4 vCPU / 16 GB / 80 GB SSD infra host for a 5-user beta — conditionally.**

Conditions (all already required elsewhere in the design; none new procurement):

1. **RDSH stays off this host** — no interactive user session ever lands on the DC (satisfied by the §1–2 separate RDSH pool). This is the load-bearing condition; it is what makes the collapse defensible at all.
2. **DC hardening** — infra host is not general-purpose: RDGW is the *only* external surface (443, behind Traefik/TLS, §D item 5, §13 layer 5); RDSH and the DC are not otherwise public; drop RDWeb if unused to shrink surface.
3. **SPOF is explicitly accepted for beta** — the availability limit (not capacity) is owned as a **known, accepted beta risk** (§SEV-1), gated by the **daily infra-VM snapshot** prerequisite (P6, §D item 6), with SEV-1 restore = Red/Nuno.
4. **DC-relaunch caveat acknowledged** — automated terminals survive an infra-VM outage but a mid-outage relaunch needs the DC; no crisp continuity % is claimed (§SLO linkage, availability caveat).
5. **RDGW is first on the split list** the moment availability or exposure justifies it (§3).

**Bottom line:** the collapse is limited by **availability, not resources** — 16 GB / 4 vCPU / 80 GB is generous for control-plane-only load and scales well past 5 users; the DC co-hosting risk is real but bounded, its single sharpest form (RDSH-on-DC) is already designed out, and its remaining sharp edge (RDGW-on-DC) has a clear first-to-split remedy.

---

**Governance.** PLANNING for approval only. This authorises **no** procurement, VM creation, licence purchase, or architecture-dependent (Phase 2+) code. Onboarding stays **CLOSED** (`BETA_ONBOARDING_ENABLED` off), `can_deploy_automation` stays **False**; Nuno's production box, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO are out of scope and untouched. All resource/cost figures are **ESTIMATE** (vendor-class typicals / SPLA list bands), never quotes; the infra host is unbuilt, so **nothing here is MEASURED on our estate**. Consistent with §§1, 2, 4 of `docs/BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md` and §D item 2 of `docs/BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md`; no files were written.

### Is the RD Connection Broker required at 2-host scale? (procurement item 9)

**Short answer:** For beta at ~2 RDSH hosts / ~2 users each, the RD Connection Broker (RDCB) is **not strictly required and should be DEFERRED** — provided interactive access is delivered per-host (direct/pinned routing) rather than as a load-balanced farm. Add it only when the pool grows past a couple of hosts or when even, automatic session distribution becomes a real requirement.

#### What the RDCB actually provides

1. **Session reconnection to the same host.** Its headline function: when a user drops and reconnects, the broker looks up their existing disconnected session in its DB and routes them back to the *same* host holding that session, instead of landing them on a fresh session on a different host.
2. **Load balancing across the pool.** Distributes new logons across pool members by session count / weight, so hosts fill evenly rather than one host taking everyone.
3. **RemoteApp / collection publishing.** Owns the "session collection" abstraction — a single published set of RemoteApps/desktops that the RD Web feed and `.rdp` files point at, with the broker resolving which host serves each launch.

It is, in Microsoft's own model, the control-plane role of an RDS deployment: you don't get a supported multi-host *farm* without it.

#### Can a 2-host pool run WITHOUT the RDCB?

Yes. Two workable brokerless patterns:

- **Per-host RemoteApp / direct routing.** Each RDSH host publishes its own RemoteApps or accepts direct RDP, and each user is assigned a specific host by DNS name or a per-user `.rdp` file (e.g. `host1.internal`, `host2.internal`). RD Gateway can still front the connection for TLS/tunnelling; it just routes to a named host instead of asking a broker.
- **Static host-pinning via our own layer.** Because our design already pins each broker account's **automated** MT5 terminal to a host through `AccountRuntime` (durable 1:1, Inc2 #150), the placement decision for the automation is *already made outside RDS*. RDS/RDCB never governs where a strategy runs — that's our orchestration.

What breaks without the broker:

- **Reconnect-to-the-right-host** becomes a client-side concern. If a user is statically pinned to `host1` and always launches the `host1` `.rdp`, reconnect is fine — they return to the same box and any disconnected session resumes normally (session persistence is an RDSH property, not an RDCB one). It only breaks if a user could be sent to *either* host non-deterministically; then a reconnect can land on the wrong host and orphan the prior session.
- **Even load** is no longer automatic — you balance by hand (assign user A→host1, user B→host2). At 2 users this is trivial and static.
- **Single collection / unified RD Web feed** is lost — you publish per-host instead of one pooled feed. Cosmetic at this scale.

#### Consequence of INCLUDING it vs DEFERRING it (beta scale)

**Including RDCB**

- Adds one more Windows role — realistically co-located on the **infra host**, which the capacity/SLO doc already treats as the single point of failure (SPOF). That makes the SPOF heavier and puts *interactive-access routing* behind the same box whose failure is already a SEV-1 in the runbooks.
- Adds a broker DB (WID or SQL) to back up, patch, and reason about.
- **Licensing:** in a proper RDS deployment the broker is normally paired with an **RD Licensing** role issuing RDS CALs/SALs. This is an **ESTIMATE, not verified** here — our procurement package (§D per memory) assumes **5 RDS Per-User SALs via SPLA**, and those SALs are required for the RDSH session hosts *regardless* of whether the broker is present. So the CAL/SAL cost is not what the broker decision turns on; the broker adds role/operational weight, not a separate must-buy licence line. Treat exact SPLA mechanics as unconfirmed until validated with the SPLA reseller.

**Deferring RDCB**

- Interactive access = per-host `.rdp` / RD Gateway direct routing, with a static user→host map maintained by hand (2 entries).
- **Reconnect risk** exists only if host assignment is non-deterministic; with static pinning it is effectively nil.
- Keeps the infra host lighter: fewer roles on the SPOF, fewer moving parts to back up and monitor, faster host-failure recovery (one less stateful role to rebuild).
- The one genuine loss is *automatic* even distribution and a single pooled feed — neither of which is load-bearing at 2×2.

#### Why deferral is the right call for beta specifically

The RDCB's marginal value here is almost entirely **interactive-session reconnection convenience**, because the part that would normally justify a broker — deciding *where each account's workload lands* — is already handled deterministically by `AccountRuntime` host-pinning in our own control plane. The automation does not touch RDS scheduling at all. So we'd be paying SPOF weight, an extra role, and backup/patch surface to buy reconnect-to-same-host for ~2 interactive users who can each be handed a fixed host. That trade is not worth it at beta scale.

#### Recommendation

- **DEFER the RD Connection Broker for beta.** Deliver interactive access via per-host `.rdp` / RD Gateway with a static user→host assignment, leaning on `AccountRuntime` for all automation placement.
- **Document it as a deliberate deferral, not an omission**, with a concrete re-add trigger: introduce the RDCB (and RD Licensing role formalisation) when **(a)** the pool exceeds ~2–3 hosts, **(b)** users need to float across hosts rather than being statically pinned, or **(c)** automatic even load-distribution / a single pooled RemoteApp feed becomes a stated requirement.
- **Keep it visible in the BoM as an optional/deferred line (procurement item 9)** so the cost and role are pre-scoped for the growth path, but ordered/stood-up only on that trigger — consistent with the standing gate that Nuno approves the §D package before any procurement.

#### MEASURED vs ESTIMATE

- **MEASURED / established in our design:** `AccountRuntime` pins each account's automated terminal 1:1 to a host (Inc2 #150, deployed); density target ≤2 users/host; infra host is the SPOF in the capacity/SLO doc.
- **ESTIMATE / unverified:** RDS CAL/SAL licensing mechanics and SPLA pairing of the broker with RD Licensing; exact behaviour of RD Gateway direct-routing in *our* specific network topology; any price figure. None of these is asserted as fact and none should gate the deferral decision.

---

Governance note: this is planning analysis only. It authorises no procurement, no licence purchase, no paid server, and no architecture-dependent code. Onboarding stays CLOSED; Nuno's existing host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO are untouched and out of scope. The §D procurement package still requires Nuno's approval before any Phase-1/2 procurement work.

**Git Status:** No repository changes made — analysis only, no files written, no commits, working tree untouched (branch `main`).

### Failure-domain analysis: the infra host SPOF (procurement item 10)

**Status:** PLANNING for approval. This section authorises no procurement, no purchase, no paid server, no licence buy, and no architecture-dependent implementation. Onboarding stays CLOSED. Nuno's existing Windows host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes, and AUTO_DEMO are untouched and out of scope.

#### 1. The failure domain

Procurement item 10 is a **single infrastructure VM** that co-locates five Windows roles:

- **AD DC** — Active Directory Domain Controller (domain authentication, Kerberos/NTLM, DNS, the per-account `guvfx_u_<id>` identities)
- **RDCB** — Remote Desktop Connection Broker (session brokering / assignment)
- **RDGW** — Remote Desktop Gateway (external RDP-over-HTTPS ingress)
- **RDWeb** — RemoteApp/RD Web Access feed (the published-app launch surface)
- **RD Licensing** — per-user/per-device RDS CAL issuance

Because all five roles sit on one VM with no redundant peer, that VM is a **single point of failure for authentication and RemoteApp access** across the entire beta cohort. There is no secondary DC to answer domain logon, no second broker, and no gateway farm member to absorb the loss.

**This is an ESTIMATE of the topology as designed in the Option A plan, not a MEASURED property of a running host** — item 10 is not yet procured, built, or observed. No availability figure below is measured.

#### 2. What STOPS when the infra host dies

Everything that requires a *fresh* domain interaction fails for the duration of the outage:

| Capability | Why it stops |
|---|---|
| **New user logins** | No DC → no Kerberos/NTLM authentication, no DNS for domain resolution. New interactive/RDP logons are refused. |
| **RemoteApp launches** | RDWeb feed and RDCB brokering are down → no published-app resolution, no session assignment. |
| **Session brokering / reconnect** | RDCB gone → new sessions cannot be placed; a dropped session cannot be re-brokered to its host. |
| **RDS licence issuance** | RD Licensing down → new sessions that need a CAL grant (grace-period dependent) are refused. |
| **External RDP ingress** | RDGW down → the HTTPS gateway path for remote users is closed. |

Net effect: **the beta onboarding and interactive-access surface is fully unavailable.** No beta user can log in, launch a RemoteApp, or (re)establish a brokered session while item 10 is down.

#### 3. What CONTINUES — and the precise caveat

**Already-running automated AUTO_DEMO terminals keep trading.** A terminal process that is *already authenticated and running* under its `guvfx_u_<id>` identity holds its session; MT5 stays connected to the broker over its own outbound network path; the strategy/routing/execution pipeline that drives it is not hosted on item 10. So an in-flight automated execution stream is **not immediately severed** by the DC dying.

**But continuity is best-effort, not guaranteed.** The moment a terminal must **relaunch mid-outage** — crash, Windows/MT5 restart, session host reboot, credential/token refresh, or any path that forces a fresh interactive/domain logon — it needs the DC to authenticate `guvfx_u_<id>`. With the DC down, that relaunch **cannot complete** until the host is restored. Cached-credential behaviour on a member host can cover some logon paths but is not a dependable substitute for a live DC for service/interactive domain logon under RDS, and must not be assumed as coverage.

Therefore: **automated-execution continuity during a DC outage is best-effort and cannot be assigned a crisp availability number until the secondary-DC HA path exists.** The honest statement for beta is *"running terminals are expected to continue; any terminal that must relaunch during the outage will not recover until item 10 is restored."* That is an ESTIMATE of behaviour, not a MEASURED SLO.

#### 4. Blast radius

- **Scope:** **entire beta cohort** — the SPOF is shared, not per-account. One VM failure removes login and RemoteApp access for every beta user simultaneously.
- **Immediate (t=0):** all interactive access lost; onboarding halted; in-flight automated terminals still trading.
- **Degrading (over outage duration):** each terminal that happens to need a relaunch drops out of continuity and stays down until restore — so the *executing* population erodes gradually the longer the outage runs, even though it is not cut off at t=0.
- **Not in blast radius:** Nuno's existing production estate (existing Windows host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes, AUTO_DEMO) — out of scope and not dependent on item 10.

#### 5. Beta acceptance rationale (accepted SPOF at beta)

The single infra host is **a knowingly accepted SPOF for the beta phase**, on these grounds:

- **Cost/complexity proportionality (ESTIMATE):** a full HA control plane (second DC, redundant broker, gateway farm) roughly doubles the control-plane footprint and licence/operational surface. At beta scale, that expenditure is not yet justified — no verified BoM or price is asserted here.
- **Bounded, non-financial-loss failure mode:** the failure blocks *access and onboarding*; it does not, by itself, place, size, or cancel trades. Automated execution already in flight continues on a best-effort basis, and no LLM/automated live-order authority is affected — governance controls sit outside item 10.
- **Onboarding is CLOSED regardless**, so the user-facing availability exposure during beta is intrinsically limited.

Acceptance is conditional on the mitigations below and on the SEV-1 runbook being defined before item 10 carries beta load.

**Mitigations (beta):**

- **Daily VM snapshot** of the infra host (full role state: AD database, RDCB/RDWeb/RDGW config, licensing DB) — enables restore rather than rebuild-from-scratch. *(Snapshot cadence and retention are an ESTIMATE pending the operational runbook; RPO = up to 24h by design of a daily snapshot.)*
- **Fast rebuild path** — documented redeploy of the five roles from snapshot to a replacement VM. *(Rebuild/restore time is an ESTIMATE, not a MEASURED RTO — it cannot be claimed as fact until a restore drill is run.)*
- **Explicit "best-effort continuity" expectation** communicated for automated terminals, per §3.

#### 6. Concrete post-beta HA path

Removing the SPOF after beta is a well-defined, standard RDS HA build (not speculative infrastructure — each element directly redundifies an existing single role, no new heavy machinery):

1. **Secondary Domain Controller** — a second DC (AD + DNS replica) so domain authentication survives loss of either host. *This is the element that lets automated-execution continuity finally carry a crisp availability number*, because relaunch-time domain logon no longer depends on one machine.
2. **Redundant RDCB** — Connection Broker in HA mode (broker pair backed by a highly-available SQL store) so brokering/reconnect survives a broker loss.
3. **RDGW farm** — two or more RD Gateway members behind a load balancer so external ingress has no single choke point.
4. *(Supporting)* redundant RD Licensing and RDWeb, and separation of the DC role off the RDS session-host/broker box, so the control-plane roles no longer share one fate.

Sequencing note (ESTIMATE): the **secondary DC is the highest-leverage first step** — it converts automated-execution continuity from best-effort to measurable and unblocks a stated availability target. Broker/gateway HA follow to remove the remaining interactive-access single points.

#### 7. Cross-reference: the §B host-failure runbook (SEV-1)

The **§B host-failure runbook EXISTS** (authored in `BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md` §B, "Windows host (RDSH) failure & pool recovery"), and it already defines the SEV-1 infra-host case: detection, SEV-1 declaration, the accepted-SPOF posture, the snapshot-restore vs rebuild decision with role-recovery ordering (DC/DNS first, then RDCB/RDGW/RDWeb/Licensing), the "best-effort continuity" message for already-running automated terminals, and post-restore continuity accounting. Loss of item 10 is a **SEV-1** event (total loss of authentication and RemoteApp access for the whole beta cohort).

What remains OPEN (validation + HA, not documentation) before item 10 carries real beta load:

- **A tested restore DRILL** — the §B runbook's RTO/RPO stay **ESTIMATE** until an actual infra-VM snapshot-restore is exercised end-to-end; only then are they measured. (This ties to the O8 estate DB-backup gap: the snapshot regime itself is a hard precondition, not yet in place.)
- **The secondary-DC HA path** — until a second DC exists, automated-execution continuity during a DC outage remains best-effort and cannot carry an availability number.

**Until the §B SEV-1 restore drill has run AND the secondary-DC HA path exists, the correct posture is: SPOF explicitly accepted for beta; snapshot + fast-rebuild as the only mitigations; and no availability SLO claimed for automated-execution continuity during a DC outage.**

---

**MEASURED vs ESTIMATE summary:** No figure in this section is MEASURED — item 10 is not yet built or observed. Topology, failure behaviour, blast radius, snapshot/rebuild timings, and HA sequencing are all ESTIMATES from the Option A design. No price, RTO, or RPO is asserted as fact; each becomes MEASURED only after the host is built and a restore drill is run.

### Backup & restore design (procurement item 11)

> **Status — DESIGN FOR APPROVAL. Authorises nothing.** No procurement, no backup infrastructure creation, no architecture-dependent (Phase 2+) implementation. This extends §D item 6 ("Backups / snapshots") from a one-line BoM entry into a per-asset backup/restore design, and closes out open item **O8** / precondition **P6**. Every RPO/RTO/size/duration below is an **ESTIMATE / proposed target** — **no measured baseline exists** (every `AccountRuntime` is `NOT_PROVISIONED` today; there is zero provisioning telemetry, §E). Values are marked **MEASURED** or **ESTIMATE**. Nuno's existing Windows box, MT5 runtimes, broker accounts, Guacamole access, strategies, routing, lot sizes and AUTO_DEMO operation are **out of scope and excluded**. Onboarding stays **CLOSED**; `can_deploy_automation` stays **False**.

#### Design principle — one authoritative source, everything else is a cache

Per the data rule (*"derivatives are rebuildable… treat derived data as a cache, not a source"*), the five assets are **not equal backup targets**. The **GuvFX Postgres DB** (asset 4) is the single authoritative anchor: it holds the durable `AccountRuntime` state machine (§8) plus the Fernet-encrypted broker credentials and Windows-identity passwords. From that DB the design can **re-materialise** the MT5 runtime dirs (§7/§14) and **re-derive** the Guacamole mappings (GuvFX is source of truth, §5/§10), and can even reconstruct AD identities via idempotent re-provisioning. So the backup strategy is: **protect the DB (and the Fernet key) hard; snapshot the Windows control-plane; treat the runtime dirs as a discardable cache.**

Two invariants govern storage: **(i) cross-fault-domain** — a backup never lives on the host it protects (infra-VM snapshot off the infra VM; DB backup off the DB host); **(ii) key separation** (security rule) — `GUVFX_FERNET_KEY` is backed up to a **separate secret store, never co-located with the DB dump**; without it the encrypted cred backups are useless, and co-located with it a leaked backup exposes credentials.

#### Asset 1 — Active Directory (identities, groups, RemoteApp/CAL entitlement)

- **Tier:** source of truth for `guvfx_u_<uid>` identities + `GuvFX-BetaUsers` group + Per-User CAL entitlement. Low change rate (identities are created only at onboarding). Lives on the collapsed infra VM (§1).
- **RPO (ESTIMATE):** ≤24h via daily system-state backup; **~0 (live)** if a **second DC** is added for AD-replication redundancy (post-beta §17 option). For beta's single infra VM, daily system-state + the daily infra-VM snapshot.
- **RTO (ESTIMATE):** bounded by infra-VM snapshot restore (SEV-1, Red, Nuno approval — §SEV-1). No measured baseline.
- **Method (candidate — confirm exact tooling in Phase 2, no-assumption rule):** Windows Server Backup **system-state** (AD DS + SYSVOL + registry) *and/or* `ntdsutil` IFM, plus the daily infra-VM snapshot; **post-beta: a secondary DC** for live redundancy (removes the DC-relaunch SPOF caveat, §SEV-1). Fallback rebuild path: because each identity maps 1:1 to a GuvFX user and the identity password is Fernet-stored in the DB, a total AD loss is also **reconstructable by re-running idempotent provisioning** (create-if-absent identity) — slower, but DB-anchored.
- **Storage:** system-state export + snapshot to an **off-infra-VM** store (separate fault domain).
- **Restore sketch:** SEV-1 path — restore infra VM from most recent snapshot → DC resolves identities → reconciler re-verifies `AccountRuntime` against RDSH reality (§SEV-1 steps 2–4). Or: non-authoritative AD DS restore / DC rebuild + SYSVOL resync; or full re-provision from the DB.

#### Asset 2 — RDS configuration (Connection Broker DB, RemoteApp collection, deployment)

- **Tier:** mostly-static config — the `GuvFX-MT5` RemoteApp collection, CB routing/pool config, Licensing. Stored in the Connection Broker database (WID/SQL) on the infra VM. Low change rate.
- **RPO (ESTIMATE):** ≤24h (captured in the daily infra-VM snapshot); effectively **rebuildable** because the RDS role install is documented, repeatable IaC.
- **RTO (ESTIMATE):** bounded by infra-VM restore; or re-run the (idempotent) RDS-role provisioning scripts + re-import the collection config.
- **Method (candidate):** (a) daily infra-VM snapshot (captures CB DB + collection + Licensing state); (b) a versioned **config export** of the RemoteApp collection + CB/pool config to an off-host store; (c) treat the whole RDS deployment as **re-scriptable** (documented build). The precise export cmdlet/mechanism is a Phase-2 detail to confirm, not asserted here.
- **Storage:** config export **off the infra VM**, versioned.
- **Restore sketch:** restore the infra-VM snapshot (control-plane restore, SEV-1); *or* rebuild RDS roles from the deployment script → re-import collection config → re-point Licensing → RDCB routes each user to their collection → verify (§SEV-1 step 4).

#### Asset 3 — Guacamole connection mappings (per-user connection + entitlement records)

- **Tier:** projection/cache of GuvFX entitlement — **GuvFX is the source of truth** (§5/§10/§12). The Guacamole DB lives on the **existing estate Postgres** (Ubuntu VPS), so it is inside the same DB-backup regime (and the same gap, O8).
- **RPO (ESTIMATE):** ≤ the estate DB backup interval (nightly ≤24h, tighter with PITR); also **re-derivable at RPO~0** by re-running entitlement sync from GuvFX.
- **RTO (ESTIMATE):** bounded by Guac-DB restore; or by re-running per-user connection provisioning (idempotent create-if-absent, §10).
- **Method:** logical dump (`pg_dump`) of the Guacamole DB on the **same BACKUP-RECOVERY-BASELINE schedule that closes O8**; plus the re-derive-from-GuvFX rebuild path.
- **Storage:** off-VPS encrypted DB backup store (shared with asset 4).
- **Restore sketch:** restore the Guac DB from the latest logical backup; *or* (rebuild) re-run provisioning's Guacamole-connection + grant creation for every active `(user, account-runtime)` from GuvFX — idempotent, converges to one connection per pair. Verify each user resolves **only their own single connection** (isolation invariant, §13 layer 3; no shared connection object).

#### Asset 4 — AccountRuntime state (GuvFX Postgres DB) — the authoritative anchor

- **Tier:** **AUTHORITATIVE / crown jewel.** Durable §8 state machine + immutable `RuntimeEvent` trail (app-layer refusal + DB BEFORE-UPDATE trigger, migration `terminal_provisioning/0005` — **MEASURED**) + **Fernet-encrypted broker creds + Windows-identity passwords**. Everything else (assets 1, 3, 5) is rebuildable *from* this. Lives on the **Ubuntu VPS — a different host from the Windows infra VM** (MEASURED framing, §SEV-1).
- **Ties directly to the OPEN estate gap:** newest DB backup was **~4.5 months stale at last audit** (**MEASURED**, KNOWN_ISSUES / operations estate) = **O8 / precondition P6**, a **hard precondition** before any real provision.
- **RPO (ESTIMATE, proposed):** tight — this holds the only rebuild anchor *and* credentials. Target **≤1h** with WAL archiving / PITR (Postgres 16); **≤24h minimum** with nightly `pg_dump`. No measured baseline — targets only.
- **RTO (ESTIMATE):** standard Postgres restore; unmeasured.
- **Method:** automated Postgres backup — **nightly dump minimum, ideally continuous WAL/PITR**; backup **encrypted at rest**; **`GUVFX_FERNET_KEY` backed up to a separate secret store, never with the dump** (security rule); **restore-tested** (an un-restored backup is not a backup — evidence rule).
- **Storage:** off-VPS, encrypted, separate fault domain; key in a distinct secret store.
- **Restore sketch:** restore the DB (PITR to just before the incident, or latest nightly) → `AccountRuntime` state + `RuntimeEvent` history + Fernet creds recovered → the reconciler (§8.2, extends `execution_health`) re-verifies every runtime against RDSH reality → drifted runtimes follow `DEGRADED→REPAIRING→RUNNING` or the forward path → missing per-account dirs re-materialised (asset 5). **Orphaned order jobs reconciled against broker Trades, NEVER replayed** (trade-safety). The immutable `RuntimeEvent` trail survives backup/restore as the forensic timeline.

#### Asset 5 — Per-account MT5 runtime directories (`D:\GuvFX\users\<uid>\accounts\<id>\mt5\`)

- **Tier:** **CACHE / derivative — not a primary backup target.** §7/§14: the runtime is *"rebuildable from config + Fernet creds."* Per the data rule, back up the config + creds (asset 4) and **re-materialise the dirs**; do not preserve them as a source.
- **Contents & handling:** portable MT5 program (~150–300 MB, **ESTIMATE** — re-copyable); per-account config (~5–20 MB — regenerated); history/logs (grows, **discardable cache**); injected creds (re-injected from the DB). Positions are **broker-side**, never in the dir (§14).
- **RPO:** **N/A as a primary asset** — the authoritative RPO is asset 4's. History/logs are discardable.
- **RTO (ESTIMATE):** bounded by **re-materialisation** — the idempotent `PROVISIONING → STARTING → AUTHENTICATING → RUNNING` path (copy portable dir + inject cred + relaunch + broker re-auth). This is the **repair-storm** cost, which is **unmeasured** (§3.7, a Phase-4 item).
- **Method:** **no per-dir backup for recovery** — re-materialise idempotently from the state machine (create-if-absent dir, overwrite config, re-inject cred, ensure-task). *Optional:* ship `Journal/Experts` logs to the estate log store for **retention/forensics only**, not recovery.
- **Storage:** none required for recovery; optional log retention to the estate log store.
- **Restore sketch:** on host loss (§14 / SEV-2) the reconciler re-materialises the account's runtime on a healthy RDSH from the durable `AccountRuntime` + DB-held Fernet cred — no dir restore. **Hard dependency:** this rebuild reads state+creds from the DB, so it **depends entirely on asset 4's backup being intact (P6)** — an unbacked DB voids the "rebuildable" guarantee (§7 rollback note).

#### Consolidated backup table

| # | Asset | Tier | Proposed RPO (ESTIMATE) | Proposed RTO (ESTIMATE) | Method | Storage location |
|---|---|---|---|---|---|---|
| 1 | Active Directory (identities/groups/CAL entitlement) | Source of truth (rebuildable from DB) | ≤24h (daily) / ~0 with 2nd DC | Infra-VM snapshot restore (Red) | System-state backup + infra-VM snapshot; post-beta 2nd DC; fallback = re-provision from DB | Off the infra VM, separate fault domain |
| 2 | RDS config (CB DB / RemoteApp collection / deployment) | Static config, re-scriptable | ≤24h (in snapshot) | Snapshot restore *or* re-run role scripts | Daily infra-VM snapshot + versioned config export; re-scriptable IaC | Off the infra VM |
| 3 | Guacamole connection/entitlement mappings | Cache of GuvFX (source of truth) | ≤ estate DB interval / ~0 re-derive | Guac-DB restore *or* re-sync from GuvFX | `pg_dump` on the estate DB schedule (O8); re-derive from GuvFX | Off-VPS encrypted DB backup store |
| 4 | **AccountRuntime state + Fernet creds (GuvFX Postgres DB)** | **AUTHORITATIVE anchor** | **≤1h (PITR) / ≤24h min** | Postgres restore (unmeasured) | **Nightly dump + WAL/PITR, encrypted, restore-tested; Fernet key in separate secret store** | **Off-VPS, encrypted, separate fault domain** |
| 5 | Per-account MT5 runtime dirs | **Cache / derivative** | N/A (rebuild from #4) | Re-materialise (repair-storm, unmeasured) | **No per-dir backup — idempotent re-materialise**; optional log retention only | None for recovery; optional log store |

#### The DB-backup gap is BLOCKING (O8 / P6)

The **newest GuvFX DB backup was ~4.5 months stale at last audit** (**MEASURED**, estate RED gap). This is **not a step — it is a hard precondition (P6)**, and it is the **single dependency the whole design rests on**: the restore path for assets 1, 3, 4 and 5 all ultimately reduce to *"recover the GuvFX DB"* (AD is re-provisionable from it, Guac mappings re-derive from it, runtime dirs re-materialise from it). While the gap is open:

- **No real beta provision may proceed** — the §7/§14 "rebuildable from config + Fernet creds" recovery guarantee is **void** without a recoverable DB, so onboarding recovery, single-runtime recovery, and SEV-1/SEV-2 host recovery are all unbacked.
- Closing it requires the **BACKUP-RECOVERY-BASELINE** work: automated Postgres backup (nightly + PITR), the **separate `GUVFX_FERNET_KEY` secret-store backup**, the daily **infra-VM snapshot regime** (which does not yet exist — the infra VM is not procured), **and a proven restore drill** (evidence rule: mark PASS only when a restore actually ran — currently **EVIDENCE-PENDING**, no drill has been performed).

**Bottom line:** this backup/restore design is **decision-ready as a plan**, but assets 1/3/4/5 have no defensible recovery until **O8/P6 closes**. Approving procurement item 11 means approving the BACKUP-RECOVERY-BASELINE (automated DB backup + Fernet-key backup + infra-VM snapshots + a restore drill) as a **Phase-1/Phase-2 hard gate before any real beta account is provisioned**. Authorises nothing further; onboarding stays CLOSED; Nuno's production estate is untouched and excluded.

---

Source docs (all absolute paths):
- `/Users/nunoamaral/Documents/Programming/Python/trading/guvfx/docs/BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md` (§7 runtime layout, §9 credential boundaries, §14 host failure & recovery)
- `/Users/nunoamaral/Documents/Programming/Python/trading/guvfx/docs/BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md` (§B SEV-1/SEV-2 + backup-gap caveat lines 1186–1193, §D item 6 lines 1586/1637, §E open item O8 line 1706, P6 line 301)

---

## 5. Comparison, cost breakdown, recommendation & exact procurement action

> **GOVERNANCE — PLANNING FOR APPROVAL ONLY.** This section authorises **no** procurement, **no** purchase, **no** paid server, **no** licence buy, and **no** architecture-dependent implementation. Onboarding stays **CLOSED**; `can_deploy_automation` stays **False**. Nuno's existing Windows host, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes and AUTO_DEMO are **out of scope and untouched**. Every figure below is drawn only from the verified provider briefs. Each is tagged **MEASURED** (confirmed on a public/authoritative page), **ESTIMATE** (derived, not a quote), or **quote-required** (no confirmable public price — must be obtained in writing). No quote-required figure is presented as exact, and no number is a purchase authority. All prices **ex-VAT, USD** unless stated; a UK/EU deployment adds VAT (UK 20%).

**Reference BoM (§D, beta):** 1× infra host (4 vCPU / 16 GB / 80 GB SSD, collapsed AD DC + RDGW + RDWeb + RDCB-if-included + Licensing) + 2× RDSH session hosts (add a 3rd only once density is *measured*) + 5× RDS per-user access licence + storage + daily infra-VM snapshot + backup.

> **⚠️ Price confidence & key risks (read before treating any total as committed):**
> - **The dominant line — the Windows host rate — is quote-required, not confirmed on an AWS-owned page.** On-demand vs 1–3 yr Reserved swings total OPEX **~2×** (~$915–955/mo vs ~$450–630/mo). Pin the region and confirm on the live AWS Pricing Calculator.
> - **Only the EC2 RDS SAL ($10.00/user/mo) is a verified figure** (AWS Marketplace, 2026-07-20). All host/storage/egress figures are ESTIMATE or quote-required.
> - **Region + currency unresolved:** US-east USD anchor vs a likely UK/EU (eu-west-2 London, GBP) deployment; all compute/storage figures shift with region and add VAT (UK 20%).
> - **Hetzner and the SPLA-VPS trio publish NO RDS SAL/CAL per-user price** — the single most load-bearing licensing line is *unquotable* on the cheap routes; any use of them needs a written per-user RDP quote first (blocking).
> - **Cloudzy VPS prices are 50%-off promotional** (non-promo ~2×); do not rely on them for ongoing cost.
> - **OVH incumbent (~$40–90/mo)** is a USD band over an EUR-billed service with undocumented FX and unstated VAT basis — replace with the real invoice line before any total; correctly excluded from incremental spend.
> - **incl-VAT** figures are shown only as worked examples until a binding quote fixes the ex-VAT base and jurisdiction.

---

#### 1. Side-by-side comparison (per verified route)

Legend: **M** = MEASURED · **E** = ESTIMATE · **QR** = quote-required (no confirmable public price).

| Line item (§D) | OVH (incumbent) | Azure (AVD) | AWS (EC2 RDS) | Hetzner | SPLA-VPS (Kamatera) |
|---|---|---|---|---|---|
| **Infra host / mo** (4 vCPU/16 GB, Windows) | N/A — Linux control plane; ~$40–90/mo whole-VPS **QR (indicative)** | ~$270–280/mo Windows D4s v5 **QR (E)**; Linux base $0.192/hr **M** | ~$275/mo Windows m5.xlarge **QR (E)**; Linux base $140.16/mo **M** | CCX23 ≈ €24.49/mo compute-only **QR (indicative)**; **Windows not available on Cloud (BYOL/manual)** — needs Robot dedicated **QR** | Windows tier ~$8.79–35.19/mo **QR (indicative)**; Windows OS included, no premium **M** |
| **RDSH / mo ×2** | N/A | ~$540–560/mo (2× Windows) **QR (E)** | ~$550/mo (2× Windows) **QR (E)**; Linux floor $280.32/mo **M** | ≈ €48.98/mo (2× CCX23, BYOL) **QR (indicative)** | ~$18–70/mo (2× Windows tier) **QR (indicative)** |
| **Windows Server licensing** | **$0 (Linux)** **M** | Bundled in AVD Windows-multi-session rate; **no separate WS licence line** **M** | License-Included (WS OS + **2 admin RDP only**) bundled in host rate **M** | Robot per-**core**: 8-core €27.90/$31, 16-core €55.90/$63 per mo **M**; Cloud = none/BYOL **M** | Kamatera included **M**; (Vultr add-on ~$16/mo **QR**; Cloudzy trial/BYOL, not SPLA) |
| **RDS SAL / CAL ×5** | N/A | **Not required for AVD multi-session**¹ **M**; AVD external per-user ~$5/user/mo **QR (indicative)** | **AWS-provided EC2 RDS SAL $10.00/user/mo** (via License Manager, cancel-anytime, elastic) **M (verified 2026-07-20)** → **$50/mo for 5** | **No public price — per-user RDP by support request** **QR (blocking)** | **Unpriced by all three** **QR (blocking)** |
| **Storage** | Not itemised on incumbent line | Managed OS disk **QR** | EBS gp3 ~$8–12/mo per host **QR (indicative)** | 160 GB NVMe in CCX23 **M**; Robot **QR** | Included in tier **QR** |
| **Backup** | Not itemised | **QR** | EBS snapshot (part of ~$10–40/mo) **QR (indicative)** | 20% of server price **QR (indicative)** | **QR** |
| **Bandwidth / egress** | **QR** | **QR** | ~$10–40/mo w/ backup **QR (indicative)** | Generous included, exact **QR** | **QR** |
| **Public IP** | usually 1 included | usually included | ~$3.6/mo/IP (IPv4 ~$0.005/hr) **E** | 1 IPv4 included; extra ~€1–2/mo **QR** | usually 1 included |
| **Snapshot (daily infra-VM)** | — | **QR** | **QR** | **QR** | **QR** |
| **Setup one-off** | $0 (already running) | $0 PAYG **E** | $0 PAYG **E** | $0 Cloud **E**; Robot may carry setup **QR** | $0 **E** |
| **Min term** | Existing | None (PAYG); 1–3 yr for Reserved discount | None (PAYG); 1–3 yr for Reserved/Savings Plan | None (hourly/monthly) | None (hourly/monthly); Cloudzy prices are 50%-off **promotional** |
| **Cancellation** | Existing | Cancel anytime on-demand; Reserved = committed | Cancel anytime on-demand; Reserved = committed | Cancel anytime | Cancel anytime |

¹ **Azure AVD architecture caveat:** AVD (Windows 10/11 **multi-session**) does **not** use the RD Session Host role, so its "no RDS CAL" property is **not** a drop-in cost saving against the approved **RDSH/RemoteApp** Option A design — it is a *different architecture*. AVD is therefore a **fallback/comparison only**, not a like-for-like route; adopting it would be a deviation from the approved design basis.

**Route-level licensing reality (the load-bearing correction):** only **AWS** publishes a confirmed, elastic, per-user RDS access price for the **EC2 RemoteApp** architecture we approved: the **AWS-provided EC2 RDS SAL is $10.00/user/mo** via AWS License Manager (cancel-anytime, no end date — verified 2026-07-20 on the AWS Marketplace listing). *(Note: an earlier draft cited $4.19 — that is the Amazon **WorkSpaces** SAL, a different service; the EC2 figure is $10.)* **Azure AVD** needs **no RDS CAL at all** for Windows multi-session (compliance-simplest) but is a **different architecture** (see ¹) and every Azure *price* here is QR/indicative and region-unresolved. **Hetzner** and the **SPLA-VPS** trio (Kamatera/Vultr/Cloudzy) bundle at most the **Windows Server OS** licence (which grants only 2 admin RDP sessions) and **publish no RDS SAL/CAL price** — the single most load-bearing §D line is unquotable on them. The provider-agnostic licensing bands are: **Route A (SPLA)** ~$350–700/mo all-in (RDS SAL ~$4.50–7/user/mo **M-band**, VM host ~$80–160/mo **QR**); **Route B (owned)** ~$4.2–5.5k one-off (WS Std ~$1,069 MSRP **M** + RDS User CAL ~$110–160 **M-band** + base WS User CAL ~$40 **M**, per 5 users).

---

#### 2. Cost breakdown (procurement item 6) — anchored to the recommended AWS route

Beta = 3 Windows hosts (1 infra + 2 RDSH) + 5 RDS SAL. Ex-VAT, USD.

| Bucket | Monthly | First month | Basis / tag |
|---|---|---|---|
| **(a) Direct infrastructure CASH** | On-demand ~$865–905/mo | ~$865–905 (no setup) | 3× Windows m5.xlarge ~$825/mo **QR (E)** (Linux floor ~$420/mo **E** — derived from a 3rd-party-mirrored hourly, confirm on AWS calculator; Windows adder **QR**) + storage ~$24–36 **QR (E)** + egress/backup ~$10–40 **QR (E)** + public IPv4 ~$3.6/mo/IP **E** |
| **(b) Software / licensing** | **$50/mo** | same | **5× AWS-provided EC2 RDS SAL @ $10.00/user/mo = $50/mo** **M (verified 2026-07-20)**. Windows Server OS licence is bundled in the host rate under (a) — **not** double-counted |
| **(c) Operational LABOUR** | Not independently costed | ~1–2 days initial | **ESTIMATE only** — internal (Nuno) setup + ~2–4 hrs/mo ongoing ops; **no verified labour rate applied**; cash-marginal ~$0 if self-run |
| **(d) Existing GuvFX (sunk)** | ~$40–90/mo | ~$40–90 | OVH incumbent VPS **QR (indicative)** — **EXCLUDED from incremental** |
| **(e) TOTAL INCREMENTAL beta** | **On-demand ~$915–955/mo** (indicative) · **1–3 yr Reserved ~$450–630/mo** (indicative) + uncosted labour | **≈ same as monthly (AWS setup $0)**; any Reserved upfront **QR** | Sum of (a)+(b)+(c). Note: this is **above** the §D ~$350–700/mo SPLA envelope on-demand; the envelope is reachable only via 1–3 yr Reserved **or** a SPLA-hoster route whose RDS SAL price is quote-required |

Only line (b)'s RDS SAL is **MEASURED (verified $10.00/user/mo)**; the (a) Linux compute floor is an **ESTIMATE** derived from a third-party-mirrored hourly (confirm on the AWS Pricing Calculator); the dominant cost — the Windows host rate — is **quote-required** and swings total OPEX ~2× between on-demand and a 1–3 yr commitment. **Honest headline: AWS on-demand (~$915–955/mo) runs ~30–170% above the §D envelope; a 1–3 yr Reserved plan (~$450–630/mo) lands near the top of it.**

---

#### 3. Recommended supplier + licensing route (procurement item 12)

**Recommendation: place the new Windows RDS pool on AWS (EC2) using the AWS-provided RDS Per-User SAL (SPLA-style, monthly, elastic) — Route A — and retain OVH as the incumbent Linux control-plane host (sunk, unchanged).**

Reasoning, against the stated criteria:

- **KNOWABLE pricing.** AWS is the only route with a **confirmed, public, per-user RDS access price for the approved EC2 RemoteApp architecture** ($10.00/user/mo EC2 SAL, verified 2026-07-20) and a bracketed compute floor. Hetzner and the SPLA-VPS trio each have a **blocking** unpriced RDS SAL/CAL line; Azure's licensing model is cleanest but is a **different architecture** (AVD multi-session, see ¹) and *all* its prices are QR/indicative and region-unresolved. AWS's one QR gap (the Windows host adder) is bracketed and closes with a single binding calculator quote.
- **COMPLIANT for 5 beta users.** AWS RDS SAL is a real per-user/month licence via License Manager user-based subscriptions — compliant for 5 provisioned users, elastic, no capital, no CAL-with-Software-Assurance/License-Mobility dependency. (Azure AVD would also be compliant and needs *no* RDS CAL, and is the recommended **fallback** if a live Azure quote beats AWS.)
- **Incumbency / consolidation with OVH.** OVH is the incumbent and consolidation is desirable, **but no verified OVH Windows/SPLA/RDS product exists in these briefs** — so OVH cannot be the licensing route today without its own quote. The pragmatic split keeps the **incumbent OVH Linux control plane** (sunk ~$40–90/mo, QR) and adds only the Windows pool elsewhere.
- **Cost.** AWS on-demand (~$915–955/mo) is honest-worst-case and **sits above** the §D ~$350–700/mo envelope; a 1–3 yr Reserved/Savings Plan (~$450–630/mo) lands near the envelope top and is the realistic steady state once density is proven. The only route that could clearly *beat* that is a **SPLA hoster whose per-user RDS SAL price is quote-required** — so obtain **one SPLA-hoster RDS SAL quote as a price benchmark** before committing. Route B (owned CALs) front-loads ~$4.2–5.5k with no beta justification yet.
- **SPOF.** Provider choice does **not** remove the single-infra-host SPOF (item 10) — that is mitigated by the daily infra-VM snapshot + fast-rebuild regime and the post-beta 2nd-DC/HA path, independent of supplier. No route is preferred on SPOF grounds.

**Deferred within the route:** RD Connection Broker (item 9) — **DEFER for a 2-host beta** (static per-host `.rdp`/RDGW routing + `AccountRuntime` pinning), re-add on the growth trigger; this trims one role off the SPOF and one licensing/ops surface.

---

#### 4. Exact procurement ACTION + expected costs

**When — and only when — Nuno approves the §D procurement package:**

> **Procure from AWS (EC2, region pinned to lowest MT5-broker latency — us-east-1 or eu-west-2/London, to be fixed in the binding quote):**
> **1× Windows m5.xlarge infra host + 2× Windows m5.xlarge RDSH + 5× AWS-provided EC2 RDS Per-User SAL + ~80–120 GB gp3 per host + daily EBS snapshots**,
> for **~$0 setup one-off** and **~$915–955/mo on-demand (indicative)** — reducible to **~$450–630/mo with a 1–3 yr Reserved/Savings Plan (indicative)** — of which only the **5× EC2 RDS SAL ($50/mo, verified $10.00/user/mo) is MEASURED**; the **Linux compute floor (~$420/mo) is an ESTIMATE** and the **Windows host rate is quote-required**, both to be confirmed on the live AWS Pricing Calculator before any figure is treated as committed.

- **Expected first-month cost:** **≈ the recurring monthly** (~$915–955 on-demand), because AWS carries **no setup fee**; any Reserved-plan upfront is **quote-required**.
- **Expected recurring monthly cost:** **~$915–955/mo on-demand** or **~$450–630/mo committed** (both **indicative, ex-VAT**), **+ ~$40–90/mo sunk OVH incumbent (excluded from incremental)**, **+ uncosted internal labour (ESTIMATE)**. Incl-VAT (UK 20%) at quote time ≈ **~$1,098–1,146/mo on-demand** / **~$540–756/mo committed**.

**Before this action executes, obtain in writing:** (i) the AWS Windows m5.xlarge on-demand + 1–3 yr Reserved rate for the chosen region; (ii) single- vs multi-session RDS SAL selection at $10/user/mo; (iii) storage/egress/snapshot/public-IP line items; **and (iv) one SPLA-hoster per-user RDS SAL quote as a price benchmark** (the route that could beat AWS but publishes no RDS SAL price). **No purchase, no server, no licence, and no procurement of any kind happens until Nuno explicitly approves** — this remains Nuno's standing gate. Nothing here opens onboarding or touches the production estate.

---

## 6. Phase 2 implementation plan (non-procurement software)

> **Governance banner.** This is a **plan for approval**. It authorises **no** procurement, purchase, paid server, licence buy, or architecture-dependent implementation *against real infrastructure*. Onboarding stays **CLOSED** (`BETA_ONBOARDING_ENABLED` off); `can_deploy_automation` stays **False** for the `beta` plan. Nuno's production Windows box, MT5 runtimes, broker accounts, Guacamole, strategies, routing, lot sizes, and AUTO_DEMO operation are **untouched and out of scope**. Every Phase-2 increment lands additive, behind flags, and ends **CLOSED**. Onboarding opens only after the **Phase-4 isolation + load gate** (§19 item 8). Sources of authority: Option A architecture (§8/§11/§13/§19) and the Operations/Capacity/SLO extension (§A/§B/§E). Notion remains authoritative for lifecycle status; nothing here advances it.

### Scope discipline: what "non-procurement software" means here

The §19 sequence and §E open items are the backbone. Phase 2 builds the **control-plane software** (models, contracts, worker engine, allocation logic, entitlement models, observability wiring, test/IaC preparation) so that *if and when* Nuno approves the §D procurement package and Phase 1 stands up a pool, the code is ready and reviewed. Everything runs in Phase 2 against **fakes, in-memory simulators, and read-only proof harnesses** — never a live/paid Windows host, never a real Guacamole connection, never the production execution path.

**MEASURED baseline (shipped in Phase 0, in-code):** `AccountRuntime` / `RuntimeEvent` / `RuntimeState` data model; the self-service onboarding step chain (`onboarding/services.py`, `STEP_ORDER`/`REQUIRED_STEPS`), which auto-creates the durable `AccountRuntime` at `NOT_PROVISIONED` on broker-account connect; the fail-closed `_get_user_mt5_instance` (resolves to the user's own runtime or **None**); per-account lot-override model; tenant-scoped/admin-only reliability endpoints; the Account Status panel scaffold. In production **every `AccountRuntime` is `NOT_PROVISIONED`** and no user has ever been provisioned.

**ESTIMATE / design-target (all of Phase 2 below):** every state-machine transition beyond `NOT_PROVISIONED`, all latency/SLO figures, all capacity/density numbers, all costs. None is a measurement. Do not present any of it as observed behaviour.

---

### Global invariants (hold across every increment — fail-closed)

These are hard DO-NOTs. Any increment that would breach one is **Red** and stops for Nuno.

| # | Invariant | Enforcement |
|---|---|---|
| INV-1 | **No licence purchase / no paid server / no procurement.** | No IaC `apply`; no SPLA/CAL provisioning; §D package stays "for approval". |
| INV-2 | **No external onboarding.** | `BETA_ONBOARDING_ENABLED` stays off; email-verify send stays disabled; gate never opened in Phase 2. |
| INV-3 | **No per-account sizing wired to production execution.** | Sizing override stays a resolvable model value; the live auto-router/execution path is not repointed. |
| INV-4 | **No migration of Nuno's runtime.** | Nuno's Windows box and MT5 runtimes are excluded from the pool and from all allocation/reconciler logic. |
| INV-5 | **No change to TI Signals / Wayond execution.** | Source-scoped routing (ADR-0011) and both listeners left byte-for-byte; isolation asserted in tests. |
| INV-6 | **No shared Windows desktop.** | Entitlement model is RemoteApp-only, per-(user, account-runtime); no shared-desktop or shared-connection object is representable. |
| INV-7 | **No fallback to any unowned runtime — fail-closed to None.** | Routing/resolution returns the user's own leased runtime or **None**; never another user's or a shared box. |
| INV-8 | **No LLM live/paper order authority.** | Model output informs design only; provisioning never places/sizes/approves an order. |
| INV-9 | **Onboarding opens only post Phase-4.** | Every increment ends CLOSED/behind flags; `can_deploy_automation` stays False for `beta`. |

**Proposed feature flags (all default OFF/CLOSED; names are ESTIMATE, to be fixed in I1's ADR):** `PROVISIONING_WORKER_ENABLED`, `PROVISIONING_AGENT_LIVE` (never set true in Phase 2), `HOST_CAPACITY_ENFORCE`, `ENTITLEMENT_SYNC_ENABLED`, `RUNTIME_HEARTBEAT_ENABLED`. Existing `BETA_ONBOARDING_ENABLED` and the `beta`-plan `can_deploy_automation` are the master gates and remain off.

---

### Increment I1 — Provisioning contracts & APIs

**What it does.** Freeze the machine-readable contracts the rest of Phase 2 depends on: the `ProvisioningJob` op enum (`PROVISION`/`START`/`STOP`/`REPAIR`/`DEPROVISION`), the §8.1 state set, the §8.2 transition edges, and the internal API surface (serializers + service functions) for requesting/reporting provisioning ops. Closes **O2** (freeze the `reason_code → {user, system, capacity, broker}` taxonomy that scopes every error budget), **O7** (draw the cred-change re-auth edge and a launch-outcome event, currently missing from §8.2), and **O11** (decide the `STOPPED → re-provision` edge for broker migration — Amber ADR).

**Key modules.** `strategies`/`trading` provisioning contract module (new, additive); DRF serializers for op-request/status; a curated `reason_code` map (sanitised user message ↔ admin raw-error-ref); ADR fixing the state/transition/taxonomy freeze.

**Tests.** Contract/serializer unit tests; exhaustive transition-legality table test (every §8.2 edge allowed, all others rejected); `reason_code` taxonomy coverage test; property test that no serializer ever emits a raw agent string to a user field.

**Adversarial review.** 6-lens: can an illegal transition be requested via the API? can a raw broker error leak to a user? does the taxonomy leave an unclassifiable failure (silent bucket)? Confirm no side-effect is reachable from I1 at all.

**Does NOT touch.** No worker, no agent, no host, no DB state transitions executed — contracts and validation only. Production execution, TI/Wayond, Nuno's runtime untouched.

**Gate / end state.** Merged additive; **CLOSED** (no code path drives a runtime). ADR recorded. Green.

---

### Increment I2 — Durable ProvisioningJob queue

**What it does.** Ship the `ProvisioningJob` durable queue mirroring the proven `ExecutionJob` pattern (lease + single-flight claim, `attempt`/`last_error`/`next_retry_at`, terminal/idempotent completion). Closes **O5** (define the lease TTL policy — proposed to mirror `EXECUTION_LEASE_TTL_SECONDS=300`, fixed by ADR, and the orphan-page threshold).

**Key modules.** `ProvisioningJob` model + migration (additive); enqueue service (called only from the shipped onboarding chain's trigger point, but **gated off**); lease/claim primitives; admin read-only queue view.

**Tests.** Enqueue/claim/lease-expiry/single-flight concurrency tests; idempotent re-enqueue test; orphan-detection (lease expired, no completion) test; migration up/down test.

**Adversarial review.** Double-claim race; lease-steal after crash; can a job be claimed by anything that would touch a real host? (No — no worker is wired yet.) Confirm the enqueue trigger cannot fire while `BETA_ONBOARDING_ENABLED` is off.

**Does NOT touch.** No worker consumes the queue in prod (flag off); no host contacted; no execution-path change.

**Gate / end state.** Migration additive; queue inert in prod (nothing claims). **CLOSED**, `PROVISIONING_WORKER_ENABLED` off. Green.

---

### Increment I3 — Windows provisioning-agent DESIGN + read-only proof harness

**What it does.** Specify the idempotent **WinRM/PowerShell** endpoint contracts (create identity, materialise per-account portable-MT5 runtime, inject Fernet-decrypted cred at injection time only, start/stop/repair/remove, health probe) that will *replace* today's hand-run `Provision-GuvfxAccount.ps1`. Deliver a **read-only proof harness** only — schema/dry-run validation of the agent request/response contract — **NOT a live agent, and NOT run against any paid host** (none exists; none is procured). Closes **O10** by making the launch-task credential-model decision (prefer **gMSA/managed identity** over stored-password Scheduled Task) as a documented design choice.

**Key modules.** Agent API contract doc + JSON schemas; PowerShell endpoint *signatures/skeletons* (idempotent create-if-absent semantics described and unit-checkable); a read-only proof harness that validates request/response shapes against fakes; ADR for the launch-task identity model (O10).

**Tests.** Schema-validation tests for every op; harness dry-run tests proving each op is *shaped* idempotently; a guard test asserting no code path can invoke a live WinRM session in Phase 2.

**Adversarial review.** Security lens (credential injection boundary: plaintext only transient inside the isolated runtime, never a shared path, never logs — kills the shared-handoff class C16); confirm the harness cannot reach a real host; confirm no secret is ever written to a tracked file or log.

**Does NOT touch.** No live host, no WinRM session, no real identity/dir/cred created. `PROVISIONING_AGENT_LIVE` never set true. Nuno's box and legacy `Provision-GuvfxAccount.ps1` untouched.

**Gate / end state.** Design + harness merged; **CLOSED** (no live agent). Amber (touches credential/identity design → ADR required). Awaits Nuno for anything live.

---

### Increment I4 — Idempotency & reconciliation (worker engine, fakes only)

**What it does.** Build the provisioning **worker engine** that drives the §8 state machine **one durable step per iteration**, **persist-then-act**, reconcile-after, with the retry/DEGRADED/FAILED policy and **no swallowed errors** (never `pass`; every failure writes a durable `RuntimeEvent`). Add the **reconciler** (extends `execution_health`) that re-drives durable state after a simulated host/terminal loss (idempotent materialisation). Closes **O1** (fix retry count `N`, backoff base/cap/jitter by ADR) and designs **O4** (per-RDSH host-death detection signal). Runs exclusively against the I3 **fake agent** in tests.

**Key modules.** Provisioning worker (extends `terminal_provisioning`); reconciler extension of `execution_health`; retry/backoff policy module; idempotency helpers (create-if-absent / overwrite / ensure-task semantics).

**Tests.** Crash-resume test (kill between persist and act → converges); idempotent re-entry from every state; retry-exhaustion → `FAILED`/`DEGRADED` with truthful reason; reconciler re-materialisation against a simulated host-down; no-swallowed-error assertion (every failure path emits a `RuntimeEvent`).

**Adversarial review.** Persist-then-act ordering under partial failure; double-drive/idempotency violation; can the reconciler ever target Nuno's runtime or another user's host? (Must be structurally impossible — INV-4/INV-7.) Confirm the worker only ever talks to the fake agent while `PROVISIONING_AGENT_LIVE` is off.

**Does NOT touch.** No real host; no production execution; no live orders. Reconciler excludes Nuno's box and any unowned runtime.

**Gate / end state.** Merged; worker flag off in prod → queue stays inert. **CLOSED**. Green→Amber (retry policy ADR).

---

### Increment I5 — Host-capacity allocation logic (concurrency-cap enforcement)

**What it does.** Implement the allocation logic that assigns a `QUEUED` runtime to a host slot **only** within the capacity envelope, and moves it to `BLOCKED` (truthful reason) when no slot exists — enforcing the **hosted-account concurrency cap** from §A: **≤ 2 users per RDSH** (density is a Phase-4 proof, not an assumption — §3.6) and the per-user account bound. Fail-closed: **no slot ⇒ BLOCKED, never a shared/overflow placement.** Relates to **O9** (density is UNPROVEN; the 70% RAM / 60% CPU raise thresholds are Phase-4 load-test acceptance criteria, not operational SLOs — the allocator hard-caps at 2/host and refuses to raise density without a documented ADR).

**Key modules.** Allocation/slot-accounting service; capacity model (host inventory as *configuration*, not hard-coded paths — currently an empty/simulated inventory since no pool exists); `QUEUED ↔ BLOCKED` driver hooked to the worker.

**Tests.** Cap-enforcement table (fill to 2/host → next is `BLOCKED`); no-oversubscription assertion (RAM never oversubscribed per §3.6); `BLOCKED → QUEUED` on cleared capacity; property test that allocation can never place onto Nuno's box or an unowned host.

**Adversarial review.** Race between two claims for the last slot; can a density raise happen without an ADR? (Must be blocked in code.) Can capacity be spoofed to overflow? Confirm empty-inventory default is fail-closed (everything `BLOCKED`, nothing placed).

**Does NOT touch.** No real hosts to allocate onto (inventory is simulated/empty in prod); no procurement; density stays hard-capped at 2/host.

**Gate / end state.** Merged; `HOST_CAPACITY_ENFORCE` on is safe because inventory is empty ⇒ all `BLOCKED`. **CLOSED**. Amber (capacity/density policy → ADR).

---

### Increment I6 — Per-account routing foundations (not wired to live execution)

**What it does.** Extend the Phase-0 fail-closed `_get_user_mt5_instance` so a runtime **endpoint** is resolved from `AccountRuntime` (per §11: each `TradingAccount` → its own runtime, or **None**). This is **foundation only** — the resolver exists and is unit-proven, but the **live auto-router/execution path is not repointed to it** (INV-3). The per-account sizing override (Phase-0 model) is resolvable but **not wired to production execution**.

**Key modules.** Routing-resolution extension over `_get_user_mt5_instance`; `AccountRuntime`-endpoint resolver; sizing-override resolver (read-only).

**Tests.** Resolver returns the user's own runtime or **None** — never shared/other-user (INV-7); no-fallback-to-unowned property test; assertion that the live execution path (and TI/Wayond source-scoped routing) is byte-for-byte unchanged; sizing override resolves but is not consumed by any live placement.

**Adversarial review.** Cross-tenant resolution attempt; can any live order path reach the new resolver? (Must be no in Phase 2.) Confirm ADR-0011 source-scoped routing and both listeners are untouched (INV-5).

**Does NOT touch.** Live execution/auto-router, TI Signals, Wayond, lot sizes, AUTO_DEMO — all unchanged. No order is ever placed via the new resolver.

**Gate / end state.** Merged; resolver dormant w.r.t. execution. **CLOSED**. Amber (routing is shared structure → ADR + handoff flag).

---

### Increment I7 — RemoteApp + Guacamole entitlement models

**What it does.** Model the strict 1:1 entitlement chain (§10/§13): **GuvFX user ↔ broker account ↔ `guvfx_u_<uid>` ↔ MT5 runtime ↔ RemoteApp resource ↔ Guacamole connection**, with GuvFX as source of truth (provisioning *would* create the connection + grant; offboarding *would* revoke). **Model + lifecycle logic only** — no real Guacamole connection is created, and **no shared connection object is representable** (INV-6).

**Key modules.** Entitlement data model (per-(user, account-runtime) connection mapping); grant/revoke lifecycle service (against a fake Guacamole client); RemoteApp-only resource descriptor (no shared-desktop field exists).

**Tests.** 1:1 chain invariant test (no shared connection, no cross-user grant); grant/revoke idempotency against the fake client; assertion that no shared-desktop entitlement can be constructed (INV-6); Nuno's Guacamole config untouched.

**Adversarial review.** Can two users map to one connection? Can a grant outlive offboarding (leak)? Can the model express a shared desktop? Confirm `ENTITLEMENT_SYNC_ENABLED` off ⇒ zero real Guacamole calls.

**Does NOT touch.** Real Guacamole, Nuno's Guacamole connections, RD infra (none procured). No live grant.

**Gate / end state.** Merged; sync flag off. **CLOSED**. Amber (entitlement/security posture → ADR).

---

### Increment I8 — Observability & admin controls

**What it does.** Wire the Phase-0 Account Status panel scaffold to **live `AccountRuntime.state` + latest `RuntimeEvent`** so stages render truthfully (Queued/Provisioning/Starting/Authenticating/Running/Degraded/Failed/Blocked) — in prod this shows `NOT PROVISIONED` for every account because nothing is provisioned. Add read-only admin estate visibility (user-scoped in user views, no cross-tenant leak). Closes **O3** (per-runtime heartbeat emission + wiring), **O4** (host-heartbeat/`ComponentHealth` for RDSH — design/emit side), and **O6** (reconcile the two coexisting heartbeat-staleness formulas — `heartbeat.py` binary ×2.5 vs `operations_summary.py` tiered ×2.0/×4.0 — into one canonical formula, packet-owner decision, flagged not silent).

**Key modules.** Account Status panel binding; heartbeat emitter + staleness evaluator (single canonical formula); admin read-only estate endpoint (tenant-scoped); `RuntimeEvent` surfacing (sanitised user message, admin raw-error-ref).

**Tests.** Panel renders exact state/`RuntimeEvent` (never false success — unimplemented stages show `NOT PROVISIONED`/`BLOCKED`); no-cross-tenant-leak test on user views; single-formula staleness regression; admin estate read-only (no mutation path).

**Adversarial review.** Can a user see another user's runtime/failure? Can the panel ever show success for an unprovisioned account? Confirm no admin control mutates a runtime (read-only in Phase 2). Confirm the reconciled formula matches both prior call-sites' intent.

**Does NOT touch.** No provisioning is triggered from the UI; no cross-tenant exposure; production dashboards for Nuno's live estate unchanged.

**Gate / end state.** Merged; panel truthfully shows `NOT PROVISIONED` everywhere. **CLOSED**. Green (with O6 formula ADR).

---

### Increment I9 — Test harnesses & infrastructure-as-code PREPARATION

**What it does.** Build the reusable **fake-agent / simulated-host test harness** used by I3–I8, and author the Phase-4 **load-test scaffolding** (5-user simulation, red-team isolation hooks) so the isolation+load gate is executable the moment a pool exists. Author **IaC** (host inventory, RDS pool, entitlement wiring) and **validate it statically** (`terraform validate` / `plan` against no real backend, lint) — **NOT applied to any paid infra** (INV-1). Track **O8/P6** (GuvFX/provisioning DB backup gap + no infra-VM snapshot) as the hard recovery precondition that must close before any *real* provision — captured here as a documented prerequisite, not actioned against paid infra.

**Key modules.** Fake WinRM/agent + simulated-host harness; Phase-4 load/isolation test scaffolding (skipped/marked until pool exists); IaC modules (validated, never applied); backup-baseline design note referencing BACKUP-RECOVERY-BASELINE.

**Tests.** Harness self-tests; `terraform validate`/`plan` runs clean with **no apply** and no real credentials; CI guard asserting no `apply`/no live-backend/no procurement call is reachable; Phase-4 scaffolding executes against the fake harness end-to-end (state machine → allocation → entitlement) with no real host.

**Adversarial review.** Can any harness/IaC path reach a paid resource, a real host, or Nuno's estate? (Must be structurally impossible.) Can a secret enter IaC/tracked files? Confirm load-test scaffolding cannot open onboarding or set `can_deploy_automation`.

**Does NOT touch.** No `terraform apply`; no paid infra; no real host/Guacamole/DB-of-record change; no procurement. Backup gap is *documented*, not remediated against paid infra here.

**Gate / end state.** Merged; IaC validated-not-applied; harness green against fakes. **CLOSED**. Amber (IaC/infra design → ADR; O8 backup precondition flagged to Nuno).

---

### Sequencing, dependencies, and the exit gate

```
I1 contracts ─▶ I2 queue ─▶ I4 worker+reconcile ─▶ I5 capacity ─▶ I8 observability
      │                    ▲                     │
      └▶ I3 agent design ──┘ (fakes)    I6 routing foundations (parallel, dormant)
                                        I7 entitlement models (parallel, model-only)
      I9 harness + IaC prep underpins I3–I8 (fakes/validate only)
```

- **O1–O11 (§E) are the Phase-2 workload** threaded above: O1/O4→I4, O2/O7/O11→I1, O3/O6→I8, O5→I2, O9→I5, O10→I3, **O8→I9 (and P6 is a hard precondition before any real provision)**.
- **Every increment ends CLOSED / behind flags.** No increment opens onboarding or flips `can_deploy_automation`.
- **Exit gate (Phase 4, §19 item 8 — Red, Nuno):** per-user terminal isolation + per-account runtime isolation red-team, 5-user load test capturing real p50/p95/max (replacing the ESTIMATE density of ~2/host and the target SLOs), no cross-tenant data leak, existing production unaffected. **Only after this gate passes, and only with Nuno's explicit approval, may `BETA_ONBOARDING_ENABLED` open and `can_deploy_automation` become True for the `beta` plan.**

### Explicit non-authorisations (restated for the approver)

This plan does **not** authorise, and no increment performs: licence purchase, paid server, any procurement or IaC `apply`; external onboarding; wiring per-account sizing to production execution; migrating Nuno's runtime; any change to TI Signals / Wayond execution; any shared Windows desktop; any fallback to an unowned runtime (resolution is fail-closed to **None**). Costs, latencies, density, and SLOs herein are **ESTIMATE / design targets**, not measurements. Approval of the §D procurement package and the Phase-2 sequence — and the Phase-4 gate — remain Nuno's explicit, out-of-band decisions.

## 7. What Nuno is asked to approve

This package **returns for approval** the five items Nuno requested. None executes any purchase.

1. **The final deployment topology (§2)** — 1 infra host + 2 RDSH + Guacamole/RDGW browser path, Nuno's box outside the pool — including the two open decisions below.
2. **The initial concurrency & account limits (§1):**
   - **Configuration cap (shipped):** ≤10 broker accounts/user.
   - **NEW hosted/running concurrency cap (proposed):** **~16 concurrently-running automated terminals** across the 2-host pool (~3 active accounts/user avg), enforced server-side at the provisioning-admission boundary — **distinct** from the 10/user config cap and from the ~4 concurrent interactive-session ceiling. Raised only by measured Phase-4 density evidence.
3. **The recommended supplier & licensing route (§5):** **AWS EC2 + AWS-provided EC2 RDS SAL** ($10/user/mo verified), OVH retained as the incumbent Linux control-plane, **RDCB deferred** for the 2-host beta — pending the binding quotes in §5.
4. **The Phase-2 implementation plan (§6)** — the authorised non-procurement software increments and their DO-NOT invariants.
5. **The exact procurement action + costs (§5):** when approved, buy 3× Windows m5.xlarge + 5× RDS SAL on AWS for **~$0 setup** and **~$915–955/mo on-demand** (→ ~$450–630/mo on a 1–3 yr Reserved plan), *after* obtaining the binding AWS calculator quote + one SPLA-hoster RDS SAL benchmark quote.

### Two decisions embedded for Nuno

- **RD Connection Broker — include or defer?** Recommendation: **DEFER** for the 2-host beta (static per-host routing + `AccountRuntime` pinning; trims one role off the SPOF). Re-add on the growth trigger. (§4 item 9.)
- **2 vs 3 RDSH at launch?** Recommendation: **launch with 2** (as approved) — the ~16-terminal cap and all 5 users' automated terminals fit; only a *5th simultaneous interactive viewer* would queue briefly. Launch with 3 only if a hard 5-concurrent-interactive guarantee is wanted from day one. (§1.)

### Honest headline for the decision

- The §D **~$350–700/mo envelope is reachable**, but on the route with **confirmable, compliant** RDS licensing (AWS) it requires a **1–3 yr Reserved commitment** (~$450–630/mo); **AWS on-demand is ~$915–955/mo** (above envelope). The cheaper SPLA-hoster route could beat it but its RDS SAL price is **quote-required (blocking)**.
- The **only decision-driving figure that is fully verified** is the EC2 RDS SAL ($10/user/mo). The **dominant cost (the Windows host rate) is quote-required** and swings OPEX ~2×; it closes with one binding AWS calculator quote at a pinned region.
- **Two hard prerequisites** (independent of supplier) must close before any real provisioning: the **estate DB-backup gap (O8)** and a **tested infra-VM restore drill** (§4 items 10–11).

### Reaffirmation

**No procurement, no paid server, no licence purchase, and no architecture-dependent spend happens until Nuno approves the §5 action.** The Phase-2 *software* work in §6 is separately authorised and proceeds behind flags with onboarding CLOSED — it touches no paid infrastructure and no production estate. Nothing here opens onboarding, wires per-account sizing to execution, migrates Nuno's runtime, or alters TI Signals / Wayond.
