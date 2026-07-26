# HOSTED-MVP-BASELINE — GuvFX Hosted Execution MVP

- **Status:** BASELINE v1.0 · **Date:** 2026-07-26 · **Owner (lifecycle):** Sponsor (Nuno)
- **Authority:** the **architecture decision** is [ADR-0018 §0 — Accepted & Certified (rev 5)](ADRs/0018-interactive-runtime-architecture.md);
  the **certification** is [INFRASTRUCTURE_RESEARCH_CERTIFICATION_REPORT.md](INFRASTRUCTURE_RESEARCH_CERTIFICATION_REPORT.md).
  This baseline is the single authoritative **operational** definition the production phases build against. Where this
  doc and an older doc disagree, this doc + ADR-0018 rev 5 win.
- **Scope discipline:** design for **< 10 hosted customers**; prefer the simplest thing that works; **avoid
  irreversible security debt**. No Kubernetes, fleet orchestration, or multi-tenant Windows runtime sharing at MVP.
- **Programme:** [Hosted MVP Completion Programme](#), Phase 1 deliverable. Production remains untouched until the
  final Sponsor Go-Live Gate (Phase 14).

---

## 1. Supported customer topology
**One hosted customer → one dedicated Windows VPS → one non-admin runtime identity → one `/portable` MT5 runtime
(pinned golden, slot-contained) → one broker account → one headless Execution Plane → optional Visual Plane.**

- **Physical isolation** between customers is the VPS boundary (no shared Windows host, no shared desktop, no shared
  runtime identity). Option A (shared runtime) and RDS/RemoteApp host pools are **rejected/superseded**.
- Two identities per box: the **agent/service** (signed lifecycle, provenance, audit, HMAC secret) and the
  **non-admin runtime** (desktop/MT5 only). The runtime identity has **no** access to the agent secret and cannot
  drive the lifecycle except through the signed agent.
- Management plane is **Tailscale-only**; **no public RDP**; admin/management ports never exposed to the internet.
- The `(slot, generation)` occupancy invariant is enforced **per-VPS** (temporal dimension is mandatory even with one
  slot): re-onboarding a slot is `STOP → TOMBSTONE → RELEASE` then re-materialise with `generation + 1` (monotone),
  and every mutating op checks slot DB + ownership marker + runtime UUID + slot + generation agreement or fails closed
  and quarantines.

## 2. Supported broker / account model
- **MVP broker provider:** MetaTrader 5 (MT5) terminals; first validated broker **Pepperstone** (demo certified).
  Additional brokers are added behind the existing provider abstraction (`trading/brokers/`, MT5-first), not by
  forking the runtime.
- **One broker account per VPS at MVP.** A customer needing multiple accounts (e.g. demo + live, or prop-firm
  challenges) maps to **multiple VPSs** — recorded as a pricing/topology constraint, not silently multiplexed onto one
  runtime (that would re-introduce shared-identity risk).
- Broker connection uses the **attach model** proven in certification: the terminal authenticates once; the bridge
  `mt5.initialize(path=…, portable=True)` attaches to the already-logged-in terminal (no login args on the hot path),
  exact-path bound to the customer's slot.

## 3. Demo / paper / live classification
Three **distinct** classifications with **distinct** credentials and permissions (never interchangeable — Security
rule: separate research / paper / live):

| Class | Meaning | Money | Default execution authority |
|---|---|---|---|
| **DEMO** | broker demo account (`trade_mode=0`) | none (virtual) | enabled once provisioned + verified |
| **PAPER** | GuvFX-internal simulation / shadow (no broker order_send) | none | enabled; never reaches a real broker |
| **LIVE** | broker live account | real | **DISABLED by default**; requires an explicit human-gated activation state |

- Classification is the **platform's authoritative determination**, cross-checked against the broker
  (`account_info().trade_mode`), not a box self-report. A mismatch (e.g. demo assignment reconnects to a live account)
  is a **critical alert** and **fails closed**.
- **No LLM/model live-trading authority.** Model output informs research/suggestions only; it never places, sizes, or
  approves live or paper orders without an explicit human-gated control path.

## 4. Customer tenancy boundary
- Tenancy boundary = **the VPS**. One customer's runtime, broker state, credentials, backups, and audit live on that
  customer's VPS and its own scoped backup destination. No customer-shared credential store; no credential reuse
  across VPSs; a per-customer encryption boundary.
- Backend data is user-scoped (existing `TradingAccount` user-scoping; staff/superuser bypass is operator-only).
  Frontend never receives credentials, full account numbers, or bridge tokens.
- **Blast radius = one VPS** for *runtime* compromise. The control plane (backend, assignment DB, Tailscale) and the
  backup plane are **aggregation points** and are governed as higher-value assets (Phases 5, 8, 11).

## 5. Execution authority boundary
- Execution flows **only** through: signal → backend intent (authoritative assignment) → agent job → **per-VPS bridge
  `order_send`** → the customer's exact-path-bound `/portable` terminal.
- A working interactive chart desktop is a **manual-trading side-door**; the **Visual Plane carries no execution
  authority by default** (read-only or absent at MVP), demo is enforced **at the broker** (account/group) not only at
  the platform, and no operator manual trading on hosted boxes (Phase 9/11 controls).
- Every execution request **fails closed** where: no assignment, stale assignment, generation mismatch, bridge/account/
  server mismatch, unhealthy runtime, suspended account, inactive customer, or absent execution permission.
- MVP execution limits (preserved/implemented in Phase 2): demo/live guard, max lot size, max trades/day, SL/TP where
  strategy policy requires, symbol allow-list, account allow-list, emergency execution suspension.

## 6. Backend → agent trust boundary
- The backend resolves **customer → VPS → bridge-endpoint + token** through an **authoritative assignment** (Phase 5),
  replacing today's single global `GUVFX_WINDOWS_AGENT_BASE_URL`.
- **Distinct token per VPS** (Security RULE 3 — own secret, fail-closed, no silent substitution; several env *names*
  for the same secret are permitted and must agree; falling back to a *different* secret is forbidden).
- Agent authenticates every request; auth **fails closed** (unset credential denies all + refuses startup);
  comparison is constant-time; rejections are logged.

## 7. Bridge trust boundary
- The bridge binds to the intended portable terminal path before every execution and verifies expected terminal path,
  portable data path, account, broker server, account classification, runtime generation, and customer assignment
  (Phase 2). Unqualified `mt5.initialize()` is forbidden where another MT5 terminal could exist.
- The bridge listens only on the local/management plane (never public); its token is per-VPS and stored in the VPS's
  own secret store, not in Git or shared config.

## 8. Ownership
| Concern | Owner | Notes |
|---|---|---|
| **Credentials** | GuvFX operator, per-VPS encrypted boundary | intake → storage → rotation → revocation → destruction (Phase 3); customer supplies broker login; operator never exposes it to the model or frontend |
| **Backup** | GuvFX operator, **per-VPS scoped** destination | each VPS its own backup account writing only to its own ACL-scoped, append-only/immutable folder (Phase 8) — no shared `Backups` share |
| **Monitoring** | GuvFX operator | per-VPS health + critical alerts (Phase 7); reuse the existing monitoring estate, no second stack without justification |
| **Lifecycle / provisioning** | GuvFX operator (signed agent) | native `NEGOTIATE…RELEASE`; passwords are Nuno-gated and never seen by the model |
| **Broker account** | Customer | customer owns the broker relationship; GuvFX operates the runtime |

## 9. Operator vs customer responsibilities
- **Operator (GuvFX):** VPS provisioning + hardening; runtime lifecycle; credential custody + destruction; backups;
  monitoring + incident response; demo/live classification enforcement; execution suspension; offboarding.
- **Customer:** owns and funds the broker account; supplies broker login for a hosted account (secure intake, Phase 3);
  accepts product disclosures + risk acknowledgement; requests activation; requests offboarding.
- **Sponsor (Nuno):** approves credentials, production changes, infrastructure changes, and go-live / live-execution
  activation. These are the only mandatory stop points in the programme.

## 10. Supported failure & recovery model
- **Zero-exposure recovery is a hard invariant** (certified): after any post-entry failure the runtime must end flat —
  `try/finally` + idempotent force-flat, bounded retries, explicit residual-exposure state, critical alert if
  flattening fails, no false PASS with exposure, and shutdown ordered strictly after recovery (Phase 2). Ordinary
  customer positions are **not** flattened merely because a non-critical app error occurs — the model distinguishes
  certification/test orders, strategy-owned positions, orphaned execution, partial fills, and emergency flattening.
- **Reboot / lost-session / build-drift:** reboot recovery, LiveUpdate **prevention** (egress block; golden pinned),
  and a modal-dialog watchdog are **unproven** and are Phase 7 gates with measured RTOs; a VPS that reboots with an
  open position must reconcile to a known-safe state on recovery.
- **Backup / restore / offboarding:** per-VPS scoped backups; restore to a **clean isolated target** with no
  cross-customer contamination; verified credential destruction on offboarding (Phase 8).
- **Order-state reconciliation** across backend intent → agent job → broker order → broker deal → open position →
  final result, authoritative after any worker/network/agent/bridge restart (Phase 2/5), with a durable idempotency
  key per order intent (no duplicate execution on retry/redelivery).

## 11. Required ADR amendments
None required for this baseline: **ADR-0018 rev 5** already carries the architecture decision, and this doc is the
operational consolidation. New ADRs are expected where later phases make load-bearing decisions (e.g. the credential
storage mechanism in Phase 3, the per-customer routing/assignment model in Phase 5, and any Visual-Plane access
decision in Phase 9) — each via the normal design → review → ADR path, not an in-passing edit.

## 12. Open items carried into later phases
Per-customer routing/assignment (Phase 5) · credential lifecycle (Phase 3) · provisioning automation (Phase 4) ·
reboot/LiveUpdate/modal recovery (Phase 7) · per-VPS backup isolation (Phase 8) · manual-trading governance / Visual
Plane (Phase 9/11) · commercial model (Windows licensing, per-account = per-VPS, manual ops labour). See
[Certification Report §14](INFRASTRUCTURE_RESEARCH_CERTIFICATION_REPORT.md).
