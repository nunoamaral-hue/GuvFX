# ADR-0018 — Hosted Runtime Architecture for GuvFX MT5

- **Status:** **ACCEPTED AND CERTIFIED — rev 5** (2026-07-26). The two-plane (Execution + Visual) per-VPS
  architecture is **accepted**, and the **Execution Plane is functionally certified** by a live disposable-demo
  `order_send` round-trip on the production Windows host (see **§0**). The rev-4 §11 items are **not** blockers to the
  architecture decision; they are carried forward as the production-implementation work of the **Hosted MVP Completion
  Programme** (§0.4). Production is unchanged; go-live remains a separate Sponsor gate.
- **Date:** 2026-07-25 (rev 4) · **2026-07-26 (rev 5 — accepted & certified)**
- **Revision history:** rev 1 shared runtime identity (Option A) — **rejected** (broke the per-slot identity
  invariant). rev 2 RETURN-FOR-REDESIGN — surfaced per-slot-single-session (D) and per-tester VPS (E). rev 3 adopted
  **E (one VPS per customer)** per the programme decision. **rev 4** corrects rev 3 after a second adversarial
  review found the topology sound but several load-bearing claims false/incomplete and two whole planes missing
  (execution, manual-trading governance). **rev 5 (this) — ACCEPTED AND CERTIFIED:** the Execution Plane is now both
  *designed* (§6) **and proven** by a headless disposable-demo `order_send` round-trip (§0); status moves
  Proposed → Accepted; the §11 conditions are re-cast as owned phases of the Hosted MVP Completion Programme.
- **Relates to / preserves:** ADR-0013 (agent service), ADR-0016 (present-attribution launch + cross-account grant),
  ADR-0017 (enabled-triggerless tasks), the permanent `(slot, generation)` invariant, the signed lifecycle, the
  completed DR workstream, security RULES 1–11.
- **Scope:** Architecture only — no code/task/launcher/service/identity/session/credential/infrastructure change.

---

## 0. Certification (rev 5 — ACCEPTED AND CERTIFIED)

**Decision:** the GuvFX hosted runtime is a **Separated Execution and Visual Plane** architecture on the per-VPS
topology of §1. This decision is **accepted**, and the Execution Plane is **functionally certified**.

**Certified Execution Plane** — proven, not asserted:
- **headless** (no charts, no MDI, no interactive desktop required);
- **Session 0**;
- **`/portable`** (slot-contained data, pinned golden binary);
- **non-admin runtime identity** (per-slot `guvfx_b_slot<N>`; the `(slot, generation)` occupancy invariant holds);
- **exact-path MetaTrader5 Python IPC** (`mt5.initialize(path=…, portable=True)`), independently path-verified;
- **broker-connected** (unattended reconnect from relocated persisted broker state);
- **chart-independent** (execution is pure IPC — `order_check` → `order_send` → close).

**Visual Plane** — **optional**, **separately governed**, **not required for execution**, and carrying **no execution
authority by default** (read-only or absent at MVP; a working chart desktop is a manual-trading side-door and is
governed as such — §5).

### 0.1 Certification evidence (2026-07-24 → 2026-07-26, production Windows host WIN-RD8VDS93DK7)
- **Portable reconnect proof** — governed Session-0 `/portable` slot runtime reconnects to `PepperstoneUK-Demo`
  unattended from relocated Stage-1 broker state; first Session-0 broker socket ever established.
- **Exact-path IPC + `order_check` proof** — `initialize(path=…)` binds to `slots\2\terminal` (not production);
  `order_check` retcode 0 "Done".
- **Disposable-demo `order_send` round-trip (2026-07-26 21:33 UTC)** — one 0.01 EURUSD market order:
  OPEN retcode **10009 (DONE)** ticket **80896575** deal **53610479** @1.13990 (22.1 ms) →
  position validated (0.01 BUY, margin 38.07) → CLOSE retcode **10009 (DONE)** ticket **80896576** deal **53610480**
  @1.13908 (11.8 ms); realized −$0.82 (off-hours spread cost).
- **Zero-exposure proof** — independent fresh-process read: positions 0 / orders 0 / margin 0 / balance 49999.18;
  both deals present in broker history (IN `guvfx-cert` / OUT `guvfx-cert-close`).
- **Cleanup proof** — native STOP → secure credential wipe (0 residue in slot & all tombstones) → TOMBSTONE → RELEASE;
  slot 2 generation **3 → 4** (monotone).
- **Production-preservation proof** — reference pid 11888, production MT5 pid 4336, bridge :8788, agent :8791 all
  intact throughout; golden `FA9F3136…` unchanged; `order_send` never touched production.
- Full narrative + evidence manifest: **`docs/INFRASTRUCTURE_RESEARCH_CERTIFICATION_REPORT.md` (v1.0)**.

### 0.2 What certification proves — and what it does NOT
Certification proves the **execution *mechanism*** end-to-end on one governed runtime. It does **not** yet prove the
**production *platform*** (per-customer routing, credential lifecycle, provisioning automation, monitoring, backup,
onboarding, clean-baseline staging). Those are the Hosted MVP Completion Programme (§0.4). No production change was
made; go-live is a separate Sponsor decision.

### 0.3 One deviation recorded
The Sunday-reopen EURUSD spread was structurally wide (65–100 pts vs a normal ~10–20) and never met the original
`<50`-pt tradeability gate. The Sponsor **explicitly authorised** accepting the wider off-hours spread; **only** the
spread ceiling was raised (50 → 90 pts), every other gate unchanged. Recorded as a sponsor-approved deviation, not a
self-initiated relaxation.

### 0.4 rev-4 §11 conditions → Hosted MVP Completion Programme phases
The §11 "S1 gates" were pre-implementation conditions. They are now the production-implementation backlog, owned by
the numbered programme phases (production remains untouched until the final Sponsor Go-Live Gate):

| rev-4 §11 gate | now owned by |
|---|---|
| 1 — invariant/attribution wording, generation & agent-secret-ACL tests | Phase 2 (bridge hardening) + Phase 11 (security) |
| 2 — execution plane: per-VPS bridge, per-customer routing/token, control-plane inventory, real-order E2E | Phases 2, 5, 12 |
| 3 — manual-trading governance (read-only viewer, broker-enforced demo, no operator trading) | Phases 9, 11 |
| 4 — per-VPS backup credential isolation | Phase 8 |
| 5 — reboot recovery / LiveUpdate prevention / modal watchdog / RTOs | Phases 4, 7 |
| 6 — corrected commercial model | Phase 1 (baseline) |

---

## 1. Decision

**One dedicated Windows VPS per hosted customer:** one non-admin runtime account, one portable MT5 runtime
(`/portable`, pinned golden, slot-contained), one broker account, one GuvFX slot, one native signed lifecycle. No
RDS, multi-user desktop, token injection, shared identity, or shared customer environment.

**The topology is accepted** (physical inter-VPS isolation; production co-tenancy removed; Option A's sibling defect
cannot occur; correct economics < 10 customers). **What is NOT yet settled** and gates Stage S1 is listed in §11.

---

## 2. Two corrected load-bearing claims (rev 3 was wrong)

**2a. The per-slot `(slot, generation)` invariant is NOT "trivially satisfied."** One-customer-per-box only dissolves
the **spatial (sibling)** dimension. The invariant's **temporal** dimension is fully required per-VPS: on customer
churn the *same slot on the same box* is `STOP→TOMBSTONE→RELEASE`d and re-materialised (generation **+1**, monotone),
and before every mutating op the slot DB + ownership marker + runtime UUID + slot + generation + monotonicity must
agree or fail closed and quarantine. Dropping this "because there are no siblings" would let a re-onboard resurrect
the prior occupancy's ownership marker / stale `accounts.dat` / runtime UUID and **mis-attribute customer B's runtime
as customer A's occupancy — a temporal cross-customer identity confusion.** → **the generation/agreement machinery is
UNCHANGED and mandatory per-VPS** (S1 acceptance test: generation increments by exactly one on each RELEASE-after-
TOMBSTONE; the pre-mutation agreement + quarantine still run).

**2b. owner-SID attribution is NOT "fully discriminating" in Session 1 — it degenerates.** In an interactive session
`guvfx_runtime` owns the **entire desktop** (explorer, shell, helpers, *and* terminal64) — every process shares the
same owner-SID. So the **exe-path predicate carries all the discriminating work; owner-SID stops adding isolation**,
and the spoofing surface *grows* (a binary the tenant plants in the `Modify`-writable slot dir runs with the same
owner-SID and a craftable path). Liveness must key on the specific terminal64 image/PID, not "a process owned by the
runtime SID exists" (explorer always is). **Unresolved (S1):** ADR-0016's cross-account per-PID grant exists because
the *agent* and the *slot owner* were distinct low-priv accounts; rev 4 must state whether the agent remains a
distinct account still needing that grant (now to observe an interactive non-admin process — unproven) **or** whether
the grant is retired. "Attribution unchanged" was imprecise.

---

## 3. Reference architecture

```
Customer → Windows VPS (hardened, Tailscale-only mgmt, no public RDP)
        → non-admin runtime account (guvfx_runtime) — desktop owner
        → portable MT5 (pinned golden, /portable) → slot-contained runtime
        → broker account → signed native lifecycle (separate agent identity)
        → Monitoring · Backup · Recovery · ── EXECUTION PLANE (§6) ──▶ backend
```
Two identities per box: the **agent/service** (signed lifecycle, provenance, audit, HMAC secret) and the **non-admin
runtime** (desktop + MT5 only). **S1 acceptance test (asserted, not proven in rev 3):** the agent's HMAC/management
secret has an ACL that **excludes `guvfx_runtime`**, and `guvfx_runtime` **cannot** trigger START/STOP/TOMBSTONE/
RELEASE except through the signed agent (RULE 3 — own secret, fail-closed, no silent substitution).

---

## 4. Runtime account (`guvfx_runtime`) — non-admin, desktop owner
`RX` on golden/launcher/wrapper (not writable), `Modify` on its own slot dir only, no admin, no access to the agent
secret. **R1 — auto-logon is required for unattended reboot recovery and is irreducible:** the credential lands as an
LSA secret **recoverable in cleartext by any SYSTEM/admin on the box**, and auto-logon means a **permanently
logged-in, unlocked interactive trading desktop** reachable via the provider/hypervisor console with **no credential**
(the screen-lock layer is gone). A future "session broker" **relocates** the secret, it does not remove it. R1 is
**accepted**, its severity **bounded to demo-default** (§5): low while the box holds a *demo* login; **high the moment
a live login is persisted** (console/SYSTEM compromise then yields a live broker session).

---

## 5. Governance — the interactive desktop is a manual-trading side-door (new; rev 3 omitted)
Working charts + an unlocked auto-logged-in desktop = a **fully functional manual trading UI**. The signed lifecycle
governs only the *programmatic* path; nothing in it stops a human at that desktop (via the provider console, or a
GuvFX remote viewer) from switching to a live account, placing a manual order, attaching an arbitrary EA, or disabling
AutoTrading. In Session-0 headless the viewer (Guacamole; rule **PX-7A "viewer ≠ trading"**) was a weak trading
surface *only because charts failed*; **with charts working, any non-read-only viewer becomes a live manual-trading
path.** Required controls (S1): **(1)** remote viewer strictly **read-only or absent**; **(2)** demo-default enforced
**at the broker** (demo account/group), not only at the platform; **(3)** no operator manual trading on hosted boxes;
**(4)** provider-console access treated as privileged + logged. "No second desktop user / no shatter surface" holds
**only** while no shadow/RDP/viewer session attaches.

---

## 6. Execution / integration plane (new; the omission that made rev 3 mis-titled)
GuvFX's product *is* auto-execution: signal → backend → worker → **the order bridge (`order_send`) → MT5**. Today the
backend reaches **one** box via a single global `GUVFX_WINDOWS_AGENT_BASE_URL`. Per-customer VPSs require a designed
plane — **unresolved, gates S1:**
- **Per-VPS bridge** (each box runs its own order bridge) + **backend per-customer routing**: replace the singleton
  endpoint with a **customer → VPS → bridge-endpoint + token** resolution, a **distinct token per VPS** (RULE 3 — no
  shared substitution).
- **A Phase-1 control-plane / inventory** mapping `customer → VPS → slot → broker-account → bridge-endpoint → backup
  scope` — needed the moment one order is routed; "semi-manual" does not exempt this.
- **An S-stage acceptance that routes ONE real (demo) order end-to-end** to a customer box. *The current S1–S4 gates
  use the **non-trading** diagnostic EA and can all pass while real execution is still impossible — that gap must
  close.*
- **RESOLVED (rev 5, execution-plane packet) — execution and charts are SEPARATE planes; charts are NOT required for
  execution.** Code evidence: `scripts/mt5_signal_bridge.py` executes solely via the MetaTrader5 Python IPC
  (`mt5.initialize → symbol_select → symbol_info_tick → order_check → order_send`) with **zero** chart/EA/MDI/template
  references; its stated requirement is "terminal running + Algo Trading enabled", not charts. → adopt a **two-plane
  architecture**: an **Execution Plane** (headless-capable, bridge IPC, signed lifecycle — the proven B3P model,
  per-slot identity in Session 0, no charts) and an **optional, user-initiated Visual Plane** (interactive charts for
  human inspection, **no execution authority by default**). This **closes rev-4 gates 3 (manual-trading side-door),
  most of R1, §8 reboot-interactive-fragility, and the §2b attribution degradation** — all of which were artefacts of
  putting execution on an interactive desktop. **One residual UNKNOWN (bounded next step, not this packet):** the
  production bridge *attaches* to an already-logged-in terminal (`initialize()` with no login args); whether a fully
  headless terminal can *establish* the broker login is unproven in-programme (production login was interactive;
  Session-0 broker connect was never achieved). Close it with a headless `mt5.login()` + `order_check` **dry-run**
  (no real order) — the `order_check` primitive is already proven safe (shadow-worker retcode 0).

---

## 7. Hosted VPS lifecycle (14 deliverables) & backup
Provision → harden → golden deploy (RULE-9/10/11 gates) → runtime materialise (signed) → broker onboarding
(secret-safe, demo first, live only via the human gate) → first-run init → **diagnostic validation (non-trading EA)**
→ operational → monitoring → backup → recovery → updates → offboarding (`STOP→TOMBSTONE→RELEASE` + credential/secret
wipe) → destruction (destroy VPS).

**Backup — corrected (rev 3 broke its own blast-radius claim):** reuse the DR *mechanism* but **per-VPS distinct,
scoped credentials** — each VPS gets its **own** NAS backup account writing **only** to its **own** ACL-scoped
per-customer folder (**write-only / append-only / immutable snapshots** preferred). A shared `guvfx-backup` account +
shared `Backups` share stored on every box would make one runtime compromise reach **all** customers' snapshots
(`accounts.dat`, config, SQLite) — a cross-customer channel that contradicts "blast radius = one VPS" (RULE 6
coupling). Same caveat: the secret store and the Tailscale plane are **aggregation points** — "one-VPS" blast radius
holds for *runtime* compromise, not for *control-/backup-plane* compromise.

---

## 8. Operability — demoted from "mitigations" to UNPROVEN S1 gates (rev 3 overstated)
- **Reboot recovery has no permitted mechanism yet + no evidence.** ADR-0017 makes the per-slot launch task
  **triggerless** (nothing starts it on boot); the agent is **Manual-start** (nothing runs it at boot); the only
  proven auto-restore is **on-logon-triggered**, which ADR-0017 bans for beta slots; "persistence characterised"
  measured ~5-min *in-run* survival, **never a reboot**, never as non-admin `guvfx_runtime`. Making it work needs an
  **auto-starting agent + a boot-ordered reconciler that arms the run-as-interactive launch only after auto-logon
  completes** — a real posture change (Amber) that must reconcile with ADR-0017, then be **host-proven with a RULE-11
  positive control** (system actually down, then actually recovered).
- **LiveUpdate must be PREVENTED, not "patrolled"** (6061 is already staged on the reference vs the 6036 pin). No MT5
  switch disables it; needs a **network egress block to the update feed**, verified by the box *not* drifting.
- **Modal-dialog watchdog required** — a blocking modal (LiveUpdate/first-run/disconnect) freezes the GUI while the
  process still reports alive; `:8791` process-health can't see it.
- **Measured RTOs** for lost-session (R3) and build-drift (R2); decide **re-image** (destroys `accounts.dat` → forces
  broker re-login) vs **re-stage** (binary swap, keep slot) for golden updates; mitigate **management-RDP session
  steal** (TX-RDP class) tearing down the auto-logon console, and **patch-Tuesday** consent/OOBE screens.

---

## 9. Commercial — corrected (rev 3 §5 was best-case-as-the-case)
- **Windows Server licensing is the dominant hidden sub-cost** (~55–70 % of a budget bill). **€15–40/mo is budget-tier
  only**; production-grade (OVH/Vultr/DO/hyperscaler) is ~€25–70. Interactive desktop + daily forensic snapshot +
  Defender likely needs **6–8 GB**, not 4 → a tier up. *Name a reference provider + dated quote splitting compute vs
  Windows license before using any figure for pricing.*
- **"One broker account per VPS" is a pricing constraint:** multi-account customers (demo+live, prop-firm 3–10
  challenges) = N VPSs = N× cost — uncompetitive vs a customer self-hosting many terminals on one VPS. Must be modelled.
- **Support is low *per box* but linear and manual at MVP** (R4 snowflakes + R6 linear ops + semi-manual Phase 1) —
  the **manual per-box ops labour is the dominant MVP TCO**, not the €150–400 infra. DR is **not** negligible
  (per-GB snapshots, retention × N → 300 GB+, N boxes backhauling to one consumer NAS = bandwidth/SPOF).
- **Still the right MVP < 10:** absolute spend (whether €150 or €600) does not flip a decision driven by isolation +
  time-to-market; but §9 must not be quoted as best-case.

## 10. Scaling & future (corrected)
Phase 1 (1–10) per-VPS; Phase 2 (10–50) **automate** provisioning/fleet/monitoring — per-VPS model additive. **Phase 3
(50–250) is NOT additive:** consolidation (RDS Opt B / single-session Opt D) **re-introduces siblings — Option A's
defect returns**; the auto-logon console, per-VPS bridge, per-VPS backup, inter-VPS isolation are **rewrites**; only
the identity/lifecycle *concepts* carry. "Managed cloud instances" is **dropped** unless a model is specified (it
typically removes operator control of the Windows identity/session layer → discards B3P attribution). Token/desktop
injection permanently rejected. **BYO-credential boundary (S1):** define who enters the customer's broker login
without giving them RDP — the real leak surface is the **GuvFX operator** on the box, not siblings.

---

## 11. Recommendation & S1 gates

**ACCEPT the per-VPS/one-slot topology** (confirmed sound by four independent lenses: physical isolation, no
production co-tenancy, dissolves Option A, right economics < 10). **ACCEPT-WITH-CONDITIONS overall** — **before Stage
S1 is authorised, resolve these (Amber→Red):**
1. Reword the invariant (§2a) + owner-SID attribution (§2b); reconcile ADR-0016's cross-account grant; add the
   generation + agent-secret-ACL acceptance tests.
2. **Design the execution plane (§6)** — per-VPS bridge + per-customer routing/token + a control-plane inventory + a
   real-order end-to-end acceptance; and make the **pre-S1 determination** whether execution needs interactive charts
   at all.
3. Add the manual-trading governance controls (§5) — read-only viewer, broker-enforced demo, no operator trading.
4. Fix backup credential isolation (§7) before claiming "one-VPS" blast radius.
5. Prove reboot recovery / LiveUpdate prevention / modal watchdog / RTOs (§8) as host-verified S1 criteria, not
   assumed mitigations; reconcile the boot-relaunch with ADR-0017.
6. Correct the commercial model (§9) before it informs pricing.

Each stage remains separately governed (design → tests → mutation → adversarial review → make check → CI → merge →
re-stage → RULE-9/integrity → host validation). No implementation proceeds until these are resolved.
