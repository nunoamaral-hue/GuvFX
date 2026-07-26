# GuvFX Infrastructure Research and Certification Report — v1.0

- **Status:** FINAL · **Date:** 2026-07-26 · **Owner (lifecycle):** Sponsor (Nuno)
- **Companion decision:** [ADR-0018 §0](ADRs/0018-interactive-runtime-architecture.md) — *Accepted and Certified (rev 5)*
- **Scope:** closes the **Infrastructure Research** phase and certifies the **Hosted Execution Mechanism**. It does
  **not** claim the production platform is built; that is the Hosted MVP Completion Programme. Redaction: broker
  account shown only as suffix `****3489`; no secrets reproduced.

---

## 1. Executive summary
GuvFX needed a way to run each customer's MT5 execution on infrastructure it governs, in isolation, without a human at
a desktop. A multi-workstream research programme (B3P → B3P-2 → MT5 demo validation) established a governed,
least-privilege, per-slot Windows runtime and then answered the central open question: **can execution happen headless,
without charts or an interactive desktop?** The answer is **yes**, and it is now **certified**: on 2026-07-26 a single
0.01 EURUSD market order was opened and closed through a governed, non-admin, Session-0, `/portable` runtime via
exact-path MetaTrader5 Python IPC, with independently verified zero residual exposure, clean native teardown, and no
impact to production or the reference runtime. The architecture is a **Separated Execution and Visual Plane** model on
a **one-VPS-per-customer** topology (ADR-0018 rev 5). Infrastructure Research is complete; the production *platform*
remains to be built under a separate, phase-gated programme, with go-live reserved as a Sponsor decision.

## 2. Original problem
- The platform was **single-tenant** and unsafe for external customers (shared Guacamole/XRDP MT5 desktop; one broker
  identity; no per-customer isolation).
- MT5 execution appeared to require an **interactive, charted, logged-in Windows desktop**, which is unattended-hostile
  (screen-lock vs auto-logon), a manual-trading side-door, and a poor multi-customer isolation boundary.
- Requirement: **per-customer isolation**, **least privilege**, **unattended operation**, **auditable governed
  lifecycle**, and a **demo/live boundary** — without giving a model or a viewer the authority to place orders.

## 3. Investigation timeline (condensed)
1. **B3P / B3P-2** — per-slot execution model: non-admin `guvfx_b_slot<N>` identities, `NT SERVICE\GuvFXBetaAgent`
   service account, `(slot, generation)` immutable-occupancy invariant, signed lifecycle (`_drive2.py`, HMAC),
   present-attribution launch wrapper, enabled-but-triggerless tasks. Hardening paid for by real incidents → security
   RULES 1–11.
2. **Disaster Recovery** — pre-reboot forensic backup; 3 hash-verified copies (host, Mac, NAS); image feasibility
   assessed. Recovery confidence HIGH.
3. **MT5 demo validation (WS-A/B/E)** — no-trade diagnostic EA; Session-0 vs Session-1 characterisation.
4. **Chart-causality experiment** — proved Session-0 chart failure is a **GUI/window-station** limit, **not** a
   broker-init failure (adversarially verified).
5. **Portable-runtime validation** — distinguished the intended `/portable` slot runtime from a manually launched
   non-portable Administrator instance; the "successful" reference was non-portable (AppData), which correctly
   **stopped** the first headless-execution attempt (no portable subject).
6. **Execution-plane analysis** — code-grounded finding that the production bridge (`scripts/mt5_signal_bridge.py`) is
   **pure IPC** with zero chart/EA/MDI references → **charts are not part of execution**.
7. **Portable demo-state relocation** — relocating persisted broker state into a governed `/portable` slot yields
   unattended Session-0 reconnect + path-scoped IPC + `order_check` (retcode 0). Verdict: **Headless Execution Plane
   PASS**.
8. **Execution Plane Certification (this report)** — one live disposable-demo `order_send` round-trip. Verdict:
   **CERTIFIED**.

## 4. ADR chronology
| ADR | Subject | Outcome |
|---|---|---|
| 0013 | Beta-agent service host (WinSW) | Accepted |
| 0014 | Management-protocol release operation | Accepted |
| 0015 | Unprivileged process observation | Accepted |
| 0016 | Present-attribution launch + cross-account grant | Accepted |
| 0017 | Beta task enabled-but-triggerless (on-demand) | Accepted |
| **0018** | **Hosted runtime architecture** | **Accepted & Certified (rev 5)** |

ADR-0018 evolution: rev 1 shared-runtime **Option A** → *rejected* (broke per-slot identity); rev 2 RETURN-FOR-REDESIGN
→ surfaced per-slot-single-session (D) and per-VPS (E); rev 3 adopted **E**; rev 4 corrected false/incomplete claims
and surfaced the missing execution + manual-trading planes; **rev 5 accepts & certifies** the two-plane model.

## 5. Architecture evolution
Shared always-interactive desktop (rejected) → RDS/RemoteApp host pool (Option A/B, superseded) → non-interactive
headless automation (revised) → **one VPS per customer + separated Execution/Visual planes** (accepted). The decisive
correction was recognising execution and charts are **independent planes**: execution is IPC to a running terminal;
charts are an optional human-inspection surface with no execution authority.

## 6. Final architecture
`Customer → hardened Windows VPS (Tailscale-only mgmt, no public RDP) → non-admin runtime identity → /portable MT5
(pinned golden, slot-contained) → broker account → signed native lifecycle (separate agent identity) →` **Execution
Plane** (headless bridge IPC, per-slot identity, Session 0, chart-independent) `+` **optional Visual Plane** (interactive
charts, read-only/absent by default, no execution authority). Two identities per box (agent/service vs non-admin
runtime); `(slot, generation)` occupancy integrity enforced before every mutating operation. Details: ADR-0018 §§1–10.

## 7. Certification evidence
See ADR-0018 §0.1. Headline: OPEN `order_send` retcode **10009** ticket **80896575** deal **53610479** @1.13990
(22.1 ms); CLOSE retcode **10009** ticket **80896576** deal **53610480** @1.13908 (11.8 ms); realized −$0.82; both
deals in broker history; independent fresh-process re-read positions 0 / orders 0 / margin 0; native
STOP→TOMBSTONE→RELEASE (gen 3→4, 0 credential residue); reference + production + bridge + agent intact; golden
unchanged. Runtime: slot 2, gen 3, non-admin `guvfx_b_slot2`, Session 0, `/portable`, `PepperstoneUK-Demo` `****3489`,
`trade_mode=0 (DEMO)`.

## 8. Assumptions validated
- Charts are **not** required for execution (bridge is pure IPC).
- A Session-0 `/portable` non-admin runtime **can reconnect and execute** headlessly.
- Relocating persisted broker state yields **unattended reconnect** (no re-onboarding).
- **Exact-path IPC binding** cleanly targets the governed runtime (single terminal at the slot path; data-path verified).
- The `(slot, generation)` lifecycle with generation monotonicity holds under repeated cycles.
- Native teardown removes credential state with **zero tombstone residue**.

## 9. Assumptions rejected
- That the MetaTrader5 **Python** package exposes `SYMBOL_FILLING_*` constants — it does **not** (MQL5-only); the raw
  `filling_mode` bitmask (`& 2` IOC / `& 1` FOK) must be used, as the production bridge already does.
- That a weekend/Sunday-evening reopen would offer a **normal spread** — it was thin, wide (65–100 pts) and oscillating.
- That the first execution script's control flow was safe — it was **not** (`finish()` shut MT5 down before any close;
  several failure paths could have stranded a live position). Fixed before the live run.
- (Earlier) that Session-0 chart failure implied broker-init failure — **false**; it is a window-station limit.

## 10. Failures and corrections
- **Non-existent filling constants** (dead-on-arrival `AttributeError`) — caught by pre-execution adversarial review;
  fixed to raw-bitmask selection.
- **No guaranteed close** — added `try/finally` + idempotent `force_flat` backstop so no path leaves a live position;
  shutdown ordered strictly after flatten.
- **Non-portable reference mistaken for the portable subject** — correctly stopped the first headless attempt; resolved
  by relocating state into a genuine `/portable` slot.
- **PowerShell 5.1 encoding traps (RULES 9/11)** — every host script is ASCII-verified before transfer; negative
  findings validated with positive controls.
- **Process win:** run the adversarial review **before** the live order, not after.

## 11. Security decisions
- Per-slot **non-admin** runtime identity; agent secret ACL excludes the runtime; **fail-closed** auth (RULES 1–11).
- **Path-scoped IPC** whenever another MT5 terminal may exist; production never addressed by the experiment.
- Broker credential state treated as secret: never printed/committed/screenshotted; account shown redacted; secure-wiped
  before teardown; **0 residue** verified in slot and all tombstones.
- Demo/live separation; live remains a distinct, human-gated authority.

## 12. Operational decisions
- Governed native lifecycle only (no `Start-Process`/SSH-launched production services — RULE 1).
- Golden image pinned (`newMT5` 5.0.0.6036, `FA9F3136…`); production MT5 must never be promoted to golden (RULE 10).
- Reboot recovery, LiveUpdate prevention, and modal-dialog watchdog are **unproven** and deferred to Phase 7.

## 13. Risks retired
- "Execution requires an interactive charted desktop" — **retired** (headless execution certified).
- "Session-0 cannot connect to a broker" — **retired** (unattended reconnect proven).
- "Cannot place/observe/close a real order headlessly" — **retired** (round-trip certified).
- "No disaster-recovery baseline" — **retired** (3 hash-verified copies).

## 14. Risks remaining
- **Per-customer routing** not built (backend still targets a single global agent endpoint).
- **Credential lifecycle** for real customers undesigned (relocation was an experiment, not the onboarding design).
- **Reboot recovery / LiveUpdate / modal watchdog** unproven.
- **Backup credential isolation** per-VPS not implemented (shared-account channel would break blast-radius).
- **Manual-trading side-door** if a working-chart viewer is ever attached without read-only enforcement.
- **Commercial model** (Windows licensing, per-account = per-VPS, manual ops labour) must inform pricing.

## 15. Technical debt
- Port the `try/finally` + `force_flat` guaranteed-close backstop into the **production** bridge.
- Session-aware liquidity/spread gating (the off-hours spread exposed a fixed threshold's fragility).
- Duplicate ADR file present (`0016-present-attribution-architecture 2.md`) — housekeeping.
- No automated per-VPS provisioning; no drift detection; onboarding/monitoring/dashboard for hosted customers unbuilt.

## 16. Recovery model
DR mechanism proven (forensic backup + backup-mode copy for locked files + off-host copies + hypervisor snapshot
feasibility). Zero-exposure recovery is proven at the runtime level (idempotent `force_flat`; independent
post-condition verification). **To extend for production:** per-VPS scoped/immutable backup credentials, measured RTOs,
and a proven restore-to-clean-target with no cross-customer contamination (Phase 8).

## 17. Recommended production implementation
Execute the **Hosted MVP Completion Programme** phase-by-phase, production untouched until the Sponsor Go-Live Gate:
Phase 1 authoritative MVP baseline → Phase 2 production bridge hardening (binding, guaranteed-close, filling, idempotency,
reconciliation, execution limits) → Phase 3 credential lifecycle → Phase 4 per-VPS provisioning → Phase 5 assignment &
routing → Phase 6 onboarding → Phase 7 monitoring/alerts → Phase 8 backup/restore/offboarding → Phase 9 optional Visual
Plane → Phase 10 dashboard → Phase 11 security review → Phase 12 clean-baseline staging certification → Phase 13 release
readiness → Phase 14 Sponsor gate.

## 18. Formal closure statement
The GuvFX **Infrastructure Research** phase is **COMPLETE** and the **Hosted Execution Mechanism** is **FUNCTIONALLY
CERTIFIED** by the evidence in §7 and ADR-0018 §0. ADR-0018 is **Accepted and Certified (rev 5)**. This closure
certifies the execution *mechanism* only; the production *platform* and go-live are governed separately by the Hosted
MVP Completion Programme and remain a Sponsor decision. Production and the reference runtime were preserved throughout.
