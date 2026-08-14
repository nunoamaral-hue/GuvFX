# Beta Readiness — Go / No-Go Checklist (first five external users)

> **Single success criterion (Programme Directive):** *Can five external beta users autonomously register,
> provision, connect, and begin using GuvFX safely?* Everything on this page gates that. This is a **consolidation
> + go/no-go**, not new architecture — it points to the authoritative runbooks and records the live gate state.
> Architecture is FROZEN (ADR-0033/0034/0041/0042/0043). No new platform features.

## 1. The certification chain (critical path) — current state

Beta go-live requires this chain, in order. Each marker is emitted **only** on real on-host evidence (NO-FAKE-READY);
the observation path is **code-gated** on the isolation marker (`live_observe.py`, ADR-0041), so the order is
enforced, not advisory.

| # | Marker / gate | Repo work | Marker | Blocked on |
|---|---------------|-----------|--------|-----------|
| 1 | `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED` | **DONE** — G5v2 ACL + W^X (Exe+Dll) + golden gate + escape battery + turnkey runbook (PR #354, #355) | **WITHHELD** | On-host escape battery on a **disposable** host + **demo broker login** → **Nuno/Sponsor** |
| 2 | `HOSTED_OBSERVATION_CERTIFIED` (WAITING_FOR_LOGIN → BROKER_CONNECTED → WORKSPACE_READY) | **DONE** — live observation bridge, single-writer, fail-closed (PR #352, ADR-0041) | **WITHHELD** | (1) + a real broker-connected workspace |
| 3 | `AUTONOMOUS_ONBOARDING_CERTIFIED` / `FIRST_UNASSISTED_USER_CERTIFIED` (Register → Verify → Provision → Login → Ready) | **DONE** — onboarding journey + provisioning + delivery (PR #316/#318, ADR-0034) | **WITHHELD** | (2) + real email inbox + one unassisted user completing end-to-end |

**All three are host + credential gated (packet stop conditions #4 Nuno credentials, #5 Sponsor host action).** No
repository change advances them past WITHHELD — the evidence must come from the host session.

### 1a. Interim isolation posture — supervised beta *before* full cert (ADR-0043 Addendum B)

`HOSTED_REMOTEAPP_ISOLATION_CERTIFIED` is fundamentally a requirement for **co-residency** (multiple untrusted
tenants sharing one host) and for **public** launch. A small, supervised, **trusted** beta can start earlier **iff
beta users do not co-reside with Customer Zero's live terminal**, using a compensating control:

- **Repo (DONE):** the host-level **co-residency guard** — `hosted_workspace/tenant_isolation.py` +
  `HOSTED_TENANT_NODE_ISOLATION_ENABLED` (DARK). When ON, a non-Customer-Zero workspace can **never** be allocated
  to Customer Zero's node; it fails closed (`ALLOC_CZ_NODE_FORBIDDEN`) rather than co-reside. Enforced at the
  execution-node single writer, so the allocator **and** the `provision_hosted_execution` command are both covered.
- **Infra (Sponsor):** a **separate physical host** for the beta pool (not `100.79.101.19`) — ideally the STREAM
  10E disposable cert host, promoted to the beta pool after it passes. Add its rdp_host to
  `HOSTED_BETA_FORBIDDEN_RDP_HOSTS` as belt-and-suspenders and flip `HOSTED_TENANT_NODE_ISOLATION_ENABLED` ON.
- **What this buys:** the un-certified-but-applied W^X controls then only need to hold **between disposable beta
  tenants on the throwaway host**, never between a beta tenant and Customer Zero's money.
- **What it does NOT do:** it is **not** a substitute for `REMOTEAPP_ISOLATION_CERTIFIED`. Public launch, and any
  plan to co-reside tenants on one host, still require the on-host escape-battery cert. Weak isolation (a separate
  *session* on Customer Zero's box) is **rejected** — a code-execution escape there could still reach the live
  terminal.

## 2. What is DONE and merged (DARK, flags OFF)

- **Isolation:** ADR-0043 W^X (`TENANT-WRITABLE ⇒ NON-EXECUTABLE`) — per-tenant Exe `Deny(*)` positive allowlist,
  G5v2 inverted NTFS ACL, MetaEditor `BinaryName` pin, per-tenant Dll `Deny(*)` (reducible-half closure),
  fail-closed golden gate. Repo-complete, 0 HIGH/0 MEDIUM across ≥6 adversarial passes.
- **Host cert package (STREAM 10E):** [`STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md`](hosted-workspace/STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md)
  + `backend/terminal_provisioning/windows/escape_battery/` (tenant runner, admin evidence collector, fingerprint).
- **Observation:** ADR-0041 trust model + live bridge (fail-closed, gated on the isolation marker).
- **Onboarding/provisioning/delivery:** ADR-0034 journey + signed host executor + host provisioning engine.
- **Operational readiness:** ADR-0035 7-state health aggregator + preflight + rollback planner (staff DARK API).

## 3. Beta acceptance checklist — per user (the actual journey)

Tick ALL, unassisted, for each of the 5 users, before that user is "in":

- [ ] **Register** on `guvfx.com` via the public UI (controlled-admission allowlist, not open signup).
- [ ] **Verify email** through a real inbox (the mail-send path must be live — historically it never sent; confirm before beta).
- [ ] **Hosted journey** routes the admitted user to the hosted flow (not the legacy broker form) — PR #350.
- [ ] **Workspace request → automatic provisioning** to a **prepared, isolated** slot (G5v2 + W^X applied) — no engineer step.
- [ ] **Broker login** (the customer's own demo/live credentials, entered by the customer) → `BROKER_CONNECTED`.
- [ ] **Workspace ready** via the certified observation path → `WORKSPACE_READY`.
- [ ] **Ready to trade** — strategy assignable at safe default sizing; execution behind its own arm (stays DARK until explicitly armed).
- [ ] **Isolation holds** for this tenant (the per-tenant W^X + ACL applied and read-back verified during provisioning).

## 4. Production go-live checklist (operator)

- [ ] **Certification chain §1 all GREEN** (markers emitted on real host evidence). **HARD GATE — no beta without this.**
- [ ] Feature-flag matrix reviewed: only the intended beta flags on; execution/arming flags OFF unless explicitly armed.
- [ ] Golden image pinned + vetted-empty (`Test-GuvfxGoldenMql.ps1` = `vetted_empty`, RULE 10 clean-install provenance).
- [ ] Capacity: enough pre-provisioned isolated slots for 5 users (see `BETA_ONBOARDING_V1_OPERATIONS_CAPACITY_SLO.md`).
- [ ] Monitoring live: agent health probes + operational-readiness health view + alert delivery **actually tested** end-to-end.
- [ ] Backups: automated DB backup verified restorable; Customer Zero rollback anchor captured (ADR-0021 plan).
- [ ] Rollback rehearsed on the disposable host (AppLocker/ACL/tenant-fragment rollback all read-back verified).
- [ ] Support path + runbooks in operators' hands (see §6).
- [ ] Customer Zero fingerprint captured **before** any production rollout and re-verified **after** (STREAM 10E §9).

## 5. Known issues / accepted residuals (gate awareness)

Authoritative register: [`docs/KNOWN_ISSUES.md`](../KNOWN_ISSUES.md). Beta-relevant standing items:

- **Signed-DLL COM-hijack (ADR-0043):** irreducible half accepted (in-process use of a resident signed OS DLL,
  #import-parallel); reducible half (planted-from-writable) closed in code but its exact exception set is
  **soak-derived on the host** and must be applied + shown blocked in the 8004 battery before the isolation marker.
- **`%WINDIR%` LOLBIN residual (ADR-0042):** accepted, battery-exercised.
- **Single-VPS SPOFs / secret rotation / 2 of 11 healthchecks:** operational-estate items (see KNOWN_ISSUES) — assess
  acceptable-for-closed-beta vs must-fix with the Sponsor.
- **Email verification send path:** confirm it genuinely sends before relying on it for external users.

## 6. Runbooks / support pointers (do not duplicate — use these)

- Host isolation cert: `STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md` · AppLocker: `hosted-workspace/APPLOCKER_HARDENING.md`
- Golden image: `GOLDEN_IMAGE_RUNBOOK.md` · Operations: `OPERATIONS_RUNBOOK.md` · Deploy/rollback: `ADR-0021-DEPLOY-ROLLBACK-PLAN.md`
- Onboarding architecture: `BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md` + `..._OPERATIONS_CAPACITY_SLO.md`
- CZ recovery: `CZ_RECLAIM_RECOVERY_RUNBOOK.md` · Handoff/next: `HANDOFF.md`, `NEXT.md`

## 7. Nuno / Sponsor-only actions that unblock the whole chain

0. **(Interim beta, optional — ADR-0043 Addendum B)** Provision a **separate beta-pool host** distinct from
   Customer Zero, register it as a `TerminalNode`, add its rdp_host to `HOSTED_BETA_FORBIDDEN_RDP_HOSTS`, and flip
   `HOSTED_TENANT_NODE_ISOLATION_ENABLED` ON. This isolates a supervised beta from Customer Zero *before* the full
   cert; it does not replace it (see §1a).
1. Provision (or approve) a **disposable certification host** mirroring production.
2. Provide a **disposable demo broker account** + enter its credentials into MT5 (agents never enter credentials).
3. Authorize + perform the **Enforce flip + escape-battery run** on the disposable host → yields the isolation evidence.
4. Authorize the **production rollout** to Customer Zero (CZ before/after), only after a genuine disposable-host PASS.
5. Confirm the **email-send path** is live for external users.

Until 1–5, the certification chain (§1) stays WITHHELD and closed beta cannot start — by design, not by defect.
