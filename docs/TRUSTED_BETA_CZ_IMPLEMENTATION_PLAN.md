# Customer Zero Completion + Trusted Beta Platform Baseline — Implementation Plan

**Status:** planning deliverable (produced *before* any host mutation, per the Programme Directive).
**Author:** Claude (Technical Lead). **Owner of authorisation:** Programme Sponsor.
**Date:** 2026-07-30. **Grounding:** read-only repo map (this session) + host certification (2026-07-30).

> **AUTHORITATIVE PHASE ORDER (2026-07-30 evidence-driven directive) — supersedes this doc's original A–E
> letters.** A = Golden re-stage (6061) · B = keyring → **observe** QUEUED→PROVISIONING→MATERIALISING→
> STARTING→RUNNING · **C = Hosted-MT5 validation (Runtime RUNNING + healthy + broker attached = "Customer Zero
> platform proven")** · **D = BUG-001** (legacy `Mt5Instance` → beta-aware `AccountRuntime`; `docs/BUGS.md`) ·
> **E = Execution plane** (this doc's original Phase C content: C0 architecture ADR → Session-0 order_send spike
> → per-slot bridge → broker login → routing → sizing → strategy assignment → trade → ingestion → analytics →
> dashboard). Live status = `docs/CUSTOMER_ZERO_EVIDENCE_MATRIX.md`. Below, "Phase C" = execution plane maps to
> the directive's **Phase E**; the directive inserts hosted-MT5 validation (C) and BUG-001 (D) before it.

---

## 0. The honest reframe (read this first)

The directive widens Customer Zero from "runtime running" to a full **trade-execution → ingestion →
analytics** journey, and makes the golden the permanent Trusted-Beta baseline. A read-only map of the
current codebase establishes exactly what is built vs unbuilt for a **beta slot** (as distinct from Nuno's
single-tenant path):

**Built and deployable for a beta account:** registration → email-verify → login → onboarding → plan →
canonical account creation (ADR-0021) → `ProvisioningJob` → per-slot MT5 runtime `MATERIALISE/START/VERIFY`
→ **RUNNING** (process + session verified) + immutable Verification Report. Strategy assignment/arming
*code* exists (flag-gated OFF). The analytics/dashboard **read** layer is generic and user-scoped — it will
render a beta user's numbers **the moment `Trade` rows exist for that account**.

**NOT built for a beta slot — the entire back half of the journey:**
1. **Per-slot execution transport (largest gap, architectural).** The only order-placer is a *single* shared
   bridge (`:8788`) bound to *one* terminal + *one* account = Nuno's box; it selects its terminal from env
   and **ignores** the per-request username, and its fail-closed binding gate **rejects** any non-matching
   account (`account_login_mismatch`). The beta-agent protocol is **lifecycle-only** (`MATERIALISE, START,
   VERIFY, STOP, TOMBSTONE, RELEASE`) — there is **no order/trade/configure op** — and the beta agent "does
   not interact with the trade bridge." The slot runtime is view-only ("no autotrade / no EAs"). **A beta
   slot has zero order capability today.**
2. **Broker login into the slot's MT5** — deferred. `PROVISIONING_REQUIRE_BROKER_LOGIN` default OFF;
   `configure()` sends **no** credentials; there is no CONFIGURE op. No beta slot has *ever* logged into a
   broker.
3. **Per-account routing** — `AccountRuntime.bridge_identity` / `SessionAssignment.enabled` exist but are
   **write-only / dormant** ("live path does not read this yet"); a beta `AccountRuntime` has no
   `mt5_instance`/`TerminalNode`, so `ExecutionJob`s for a beta account are never even constructed/routed.
4. **Per-account 0.01 sizing** — not implemented; sizing is source-global (beta would inherit ti_signals'
   0.40/leg).
5. **Ingestion** — the single ingest worker reads the single bridge's one terminal; beta trades never reach
   `Trade`/analytics until a slot-scoped ingest path exists.
6. **Session-0 order feasibility — UNPROVEN (not just unbuilt).** GuvFX trades via Python-MT5-IPC
   `order_send`, *not* EA-on-chart, so the known Session-0 chart/MDI GUI failure does **not** inherently
   block execution. But that a Python `MetaTrader5` bridge running in Session 0 *as the slot identity* can
   `mt5.initialize()`-attach to the slot terminal and `order_send` has **never been tested** (it was
   explicitly out of scope of demo validation). This is the single biggest empirical unknown.

**Consequence for sequencing.** Golden re-stage + Phase-4 keyring reach **"runtime running" (directive
priorities 1–4)** and are well-scoped. **Priorities 5–10 (strategy → trade → ingestion → analytics) are a
substantial build**, not a quick follow-on: they need an **architecture decision**, a **feasibility spike**,
a **disposable demo broker account**, and per-account routing/sizing — several of them Red (money path,
credentials, possibly procurement). The plan front-loads the decision and the spike so we do **not** build
throwaway.

**Standing safety fact to preserve at every step:** today, beta execution is prevented only *incidentally*
(no `mt5_instance` → `windows_username=None` → fail-closed; `BETA_RUNTIMES_ENABLED` OFF; bridge binding gate).
Any enablement in Phase C must make cross-tenant isolation **explicit and robust**, and must never arm
Nuno's live route.

---

## DECISION GATE C0 — the pivotal architecture choice (needed before Phase C build)

How does a beta slot execute an order? Two documented options:

| | **Option A — per-slot bridge (shared OS)** | **Option B — one small VM per tenant** |
|---|---|---|
| Mechanism | A per-account Python MT5 bridge inside the slot (own port `:87xx`, own token), order_send to that slot's terminal | Reuse the proven `:8788` bridge design, one per VM (each VM its own address) |
| Procurement | **None** — uses the existing beta host | **Yes** — VMs + BoM + Sponsor approval (Red; none authorised) |
| Isolation | OS-level (slot identity + port/token + ACLs) | VM-level (strongest) |
| Time to prove CZ | Fastest (in-repo build) | Gated on procurement lead time |
| Rebuild risk | Low — the **per-account routing layer** (endpoint+token per `AccountRuntime`) is needed in *both* options, so it is not throwaway; only the transport host differs | — |

**Recommendation:** **Option A** to prove Customer Zero and the first handful of beta users on the existing
box, **with the per-account routing layer designed to generalise to per-VM** (so moving to Option B later is
a host swap, not a rebuild). This satisfies the directive's "prove the platform without procurement delay,
without building throwaway." Option B remains the recommended *scale* topology and can be adopted at the
Trusted-Beta-expansion milestone. **This choice is an ADR + a Sponsor authorisation point** and gates the
Phase-C build.

---

## Phase A — Golden re-stage (the permanent baseline MT5 image)

**Objective:** replace the drifted, used, auto-updating golden with a pristine, dedicated, never-launched
MT5 **build 6061** image that becomes the long-term Trusted-Beta baseline every slot materialises from.

**Deliverables:** a clean 6061 golden at a golden path; `.guvfx_golden_manifest` (pinned 6061) + empty
`.guvfx_portable`; no broker account / no bases history / no EA; RULE-10 validation PASS; re-pinned
`BETA_AGENT_GOLDEN_DIGEST`; agent bundle re-staged if config changed.

**Host mutations (Red/Sponsor-gated):**
1. Sever the current golden from runtime/tester use: **stop the running golden `terminal64.exe` (pid 5912)**;
   **remove** the `MetaTrader 5 Strategy Tester Agent` firewall rule targeting `…\newMT5\metatester64.exe`;
   ensure nothing (human or backtest) launches the golden install. Point any strategy-tester/backtest need at
   a **separate** tester install (never the golden).
2. Produce a **fresh clean 6061 install from a clean MetaQuotes source** (Sponsor-supplied clean installer
   output — I do not download/execute installers), in an isolated staging location; **disable/deny
   LiveUpdate** so it can never self-update; confirm it is never launched/logged-in.
3. Validate RULE-10 (`install_pool.ps1 -ValidateGoldenOnly`), promote to the golden path, re-pin
   `BETA_AGENT_GOLDEN_DIGEST` to its digest, re-run `install_pool.ps1 -VerifyOnly`.

**Rollback:** retain the old golden dir (renamed) until the new one verifies; the config pin is a one-line
revert; nothing on Nuno's estate is touched.

**Evidence:** RULE-10 validation PASS; `-VerifyOnly` digest == new pin; proof the golden was never launched
(no process, no firewall registration, clean logs); before/after estate untouched (Nuno MT5 + bridge alive).

**Stop gate:** `-VerifyOnly` PASS + digest matches the new pin + golden confirmed inert (never-run). **STOP.**

**Sponsor authorisation points:** (A1) the host mutations above; (A2) build **6061** confirmed (already
directed); (A3) source of the clean 6061 install (who produces the clean MetaQuotes install).

---

## Phase B — Phase 4 completion (keyring → Customer Zero runtime RUNNING)

**Objective:** clear the *first* provisioning gate (auth) and drive Customer Zero's runtime to **RUNNING**
against the clean golden — directive priorities 2–4.

**Deliverables:** `BETA_AGENT_KEYRING`/`BETA_AGENT_KEY_ID` provisioned on **both** sides (the Windows beta
agent **and** the `guvfx-beta-provisioner` container — the provisioner's empty keyring is the *current* CZ
stall); NEGOTIATE succeeds; CZ `ProvisioningJob` advances `MATERIALISE → START → VERIFY → RUNNING`; immutable
Verification Report; truthful Account-Status stepper shows the runtime healthy.

**Host mutations (Red/Sponsor-gated — credentials):**
1. Generate/provision the HMAC keyring into a **service-account-scoped** store on the agent host (per the
   security rule — not a bare machine env var) and into the provisioner container's secret-backed env.
2. Enable `BETA_RUNTIMES_ENABLED` for the beta cohort; re-drive CZ's `ProvisioningJob #1` (or the sanctioned
   re-enqueue) → runtime RUNNING.

**Rollback:** unset the keyring → NEGOTIATE fails closed → reverts to the current stalled-but-safe state;
disable `BETA_RUNTIMES_ENABLED`. No estate impact.

**Evidence:** NEGOTIATE success (agent + provisioner logs, no `unknown_key_id`); ProvisioningJob state
transitions; Verification Report (process + session verified); Golden Execution Reference STOP-check on
Nuno's estate byte-identical (deploy discipline); **no order placed**.

**Stop gate:** CZ runtime **RUNNING** + estate STOP-check clean. **STOP** — this is the "runtime running"
milestone; the directive says it is *no longer sufficient*, so we do **not** proceed to execution without
the C0 decision.

**Sponsor authorisation points:** (B1) keyring provisioning (Red — credentials); (B2) `BETA_RUNTIMES_ENABLED`
on for beta; (B3) confirm the golden re-stage (Phase A) landed first (MATERIALISE needs the pinned clean
golden).

---

## Phase C — Beta execution plane (the large build; gated by C0)

**Objective:** give a beta slot a real, isolated order path — broker login, per-slot bridge, per-account
routing, per-account sizing — so Customer Zero can place and manage a real (demo) trade **without any impact
on Nuno's live route**.

**Deliverables (order matters):**
- **C0 — ADR: execution architecture** (Option A vs B, above). *Decision + ADR before any build.*
- **C-spike — Session-0 order feasibility proof (do this EARLY, low-cost, de-risks everything).** On one
  clean beta slot with a **disposable demo broker account**: prove a Python `MetaTrader5` bridge running in
  Session 0 *as the slot identity* can `mt5.initialize()`-attach to the slot terminal, log in, and
  `order_send` a single demo order, headless. **If this fails, the whole per-slot model needs rework** —
  which is exactly why it runs before the full build.
- **C1 — per-slot bridge** (Option A): a reviewed per-account bridge process (own port + token), commissioned
  as an install artefact, started with the slot, order_send/close/modify against *that* slot's terminal only.
  Crosses the Windows-primitive boundary → **ADR + adversarial review + host commissioning**.
- **C2 — broker-login stage:** implement the CONFIGURE credential-inject secure path; flip
  `PROVISIONING_REQUIRE_BROKER_LOGIN` ON; `broker_login_verified` becomes a real platform determination.
- **C3 — per-account routing:** store `(endpoint, token)` per `AccountRuntime`; make the ingest worker +
  `ExecutionJob` construction/claim **read** the owning runtime's routing (activate the dormant
  `SessionAssignment`/`bridge_identity`); wire `AccountRuntime` ↔ execution handle so jobs route only to the
  owning slot. **Explicit cross-tenant isolation tests** (a beta job can *never* reach Nuno's terminal).
- **C4 — per-account 0.01 sizing:** per-assignment size field (or beta source config) so beta trades at 0.01
  while Nuno's route is unchanged.
- **C6 — enablement levers (staged, each its own gate):** `MULTI_ACCOUNT_ROUTING_ENABLED`,
  `BETA_SELF_SERVE_ARM_ENABLED`, per-source `command_engine`/arm, `auto_execution`, `signal_execution_mode`
  — enabled with proof at each step that **Nuno's live route is unaffected**.

**Host mutations:** per-slot bridge commissioning; broker credential injection; enablement flags. **Rollback:**
every lever defaults OFF and fails closed; the per-slot bridge is a separate process that can be stopped
without touching the runtime; broker-login flag revertible.

**Evidence:** C-spike order ticket on the disposable demo account (isolated); cross-tenant isolation tests
green; a beta slot places → manages → closes a demo order via its **own** bridge; Nuno's estate STOP-check
byte-identical throughout.

**Stop gates:** **after C0** (architecture chosen); **after C-spike** (feasibility proven — hard gate before
the full build); **after C1–C4** (isolated single-trade proof); **before each C6 lever**.

**Sponsor authorisation points:** (C0) architecture ADR; (C-spike) disposable demo account + authorise the
first real demo `order_send`; (C1) per-slot bridge ADR + host commissioning; (C2) broker-login enablement;
(C6) each enablement lever. If Option B is chosen, add **(C-proc) procurement approval (BoM)**.

---

## Phase D — End-to-end trading validation (Customer Zero, with evidence)

**Objective:** demonstrate, with evidence at every stage, the complete widened Customer-Zero journey.

**Deliverables / evidence checklist (the Trusted-Beta acceptance baseline):** broker connected → MT5
connected → **first trade opened** → managed correctly (breakeven/TP protection applies to the beta account)
→ **trade closed** → **ingested** into `Trade` (per-account) → **analytics** populated → **dashboard**
updated → customer experience validated end-to-end through the public UI.

**Host mutations:** none beyond Phase C (this is validation). **Rollback:** disable execution levers.

**Evidence:** a captured trade lifecycle for CZ's account (open/manage/close tickets), the ingested `Trade`
rows, the analytics/dashboard render, all scoped to CZ's account and **isolated from Nuno's**; the estate
STOP-check unaffected.

**Stop gate:** the full acceptance checklist demonstrated. **This is "Customer Zero complete."**

**Sponsor authorisation points:** (D1) confirm CZ acceptance; (D2) sign-off that the journey is the
Trusted-Beta acceptance baseline.

---

## Phase E — Trusted Beta readiness (platform baseline + RDS path)

**Objective:** turn the proven CZ path into a supportable multi-customer platform without a rebuild.

**Deliverables:**
- **Golden-as-baseline clarification + hardening:** the *golden* is the MT5 image; the broader "runtime
  baseline" (Windows config, beta-agent, slot infra, per-slot bridge, monitoring, hardening) is **host
  provisioning** (`install_pool`/`install_service`/agent), largely already built (B3P-2). Capture the full
  baseline as reproducible provisioning so onboarding a new slot/host is deterministic. *(If Option B/per-VM
  is later chosen, the baseline becomes a VM template image — the golden MT5 tree is reused inside it.)*
- **Supervisor/watchdog** for N runtimes + per-slot bridges (launch, health, self-heal) — generalising the
  single manually-started bridge to N supervised ones.
- **Monitoring/alerting prerequisites** per slot (heartbeat/sync stages currently stubbed in Account-Status
  become real).
- **RDS / RemoteApp / CAL** — **not a Customer-Zero dependency.** Design the runtime/access architecture so
  RemoteApp can be introduced cleanly (the Guacamole viewer path already provides browser access today).
  Treat RDS licensing + CAL configuration + RemoteApp publication as a **subsequent infrastructure
  milestone**; do not delay CZ for it, and avoid architecture that blocks it.

**Host mutations:** monitoring/supervisor commissioning; (later) RDS role + CAL + RemoteApp publication.
**Rollback:** additive services, disableable. **Evidence:** N-runtime soak; RDS design doc + a clean
integration point. **Stop gates:** per sub-item. **Sponsor authorisation:** (E1) supervisor/monitoring
commissioning; (E2) the RDS/CAL milestone (licensing = Sponsor); (E3) scale to 3–5 beta users.

---

## Critical path, dependencies, and the balance the directive asked for

```
A (golden 6061) ──► B (keyring → RUNNING) ──► C0 decision ──► C-spike (feasibility) ──► C build ──► D validate ──► E readiness
                         [priorities 1–4]        [ADR]         [HARD de-risk gate]     [the work]   [CZ complete]
```

- **A and B are independent of the big decision** and reach "runtime running" — do them first; they are
  well-scoped, reversible, and prove the provisioning plane for CZ.
- **C0 (architecture) and C-spike (feasibility) are the highest-value early moves** — they decide *what* to
  build and prove it is *possible* before spending the build. Running the spike early is the directive's
  "evidence before assumption" applied to the riskiest unknown.
- **Not throwaway:** the per-account routing/sizing layer (C3/C4) is required under *both* architecture
  options, so building it for Option A generalises to Option B — satisfying "don't build debt that gets
  rebuilt weeks later."
- **RDS stays visible but off the critical path** (Phase E), exactly as directed.

## What I will NOT do without explicit Sponsor authorisation
Provision signing keys; re-drive ProvisioningJob #1; re-pin/re-stage the golden; place any order (incl. the
C-spike demo order); enable any Class-B lever; touch Customer Zero or Nuno's estate; download/execute an MT5
installer; authorise procurement. Each is a named authorisation point above.

## Recommended immediate next action (one bounded step)
**Authorise Phase A (golden re-stage, build 6061).** It is the only phase with no upstream decision
dependency, it clears the confirmed MATERIALISE blocker, and it is the permanent baseline the directive
wants — while C0 (architecture) is deliberated in parallel.
