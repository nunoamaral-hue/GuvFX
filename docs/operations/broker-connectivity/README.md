# Broker Connectivity — Trusted-Beta Operations Package (WP5.4)

**Status:** Documentation + operational controls only. **Nothing here arms anything.**
**Programme:** GuvFX Broker Connectivity Capability, Sprint 5, WP5.4.
**Authored:** 2026-08-04. **Authority:** the merged repository (code/tests/ADRs) is authoritative;
production/host state is **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL**.

---

## What this package is

The Broker Connectivity engineering plane (WP1A→WP5.3) is complete and merged, and **every capability
ships DARK with all feature flags OFF**. This package is the operational readiness layer that must exist
**before WP6 multi-tenant certification and before any Sponsor-authorised Trusted-Beta arming**. It tells an
authorised operator what is deployed, which flags exist, the safe enablement order, how to verify each
stage, how to stop or roll back, how incidents are classified, and what evidence to retain.

It does **not** deploy, rebuild, restart, enable a flag, mutate runtime/execution state, touch Customer
Zero, access credentials, or begin WP6. See **Hard boundary** below.

## Documents in this package

| File | Purpose | Workstream |
|------|---------|-----------|
| [`feature-flags.md`](feature-flags.md) | Definitive flag inventory (the 6 arming flags + adjacent toggles) | A |
| [`feature-flags.json`](feature-flags.json) | Machine-readable flag inventory | A |
| [`arming-runbook.md`](arming-runbook.md) | Step-by-step arming order + dependency graph, stop/go, rollback per step | B, D |
| [`rollback-matrix.md`](rollback-matrix.md) | Rollback for every partial-arming state | E |
| [`incident-response.md`](incident-response.md) | SEV-1/2/3 classification + response | F |
| [`support-playbook.md`](support-playbook.md) | Operator workflows for each customer-facing situation | G |
| [`monitoring-spec.md`](monitoring-spec.md) | Monitoring signal specifications (no deployment) | H |
| [`trusted-beta-readiness.md`](trusted-beta-readiness.md) | Pre-arming checklist + WP6/Trusted-Beta entry/exit + capacity | C, I, J |
| [`evidence-pack.md`](evidence-pack.md) | Release evidence pack definition | K |
| [`readiness-checklist.json`](readiness-checklist.json) | Machine-readable checklist + arming sequence + partial states | C, B, E |

Validation test: `backend/operational_events/tests_wp54_readiness.py` (runs in `make check` / CI `backend`).

## The capability, in one paragraph

A customer adds a broker account; the backend runs an **in-place, runtime-independent login validation**
(`VALIDATE_LOGIN`) against an isolated validation terminal and records durable per-account validation state.
A **continuous health engine** folds that evidence into a per-account health state. An **execution gate**
refuses to open exposure unless the account is `VALIDATED` and eligible, re-checking at job creation, at
claim, and again immediately before `order_send`. An **operational-event read model** projects non-secret,
customer-safe events that an **internal Operations & Support UI** displays read-only. Every one of these is
independently flag-gated and OFF by default.

## The six flags at a glance (all default OFF — see [feature-flags.md](feature-flags.md))

| Flag | Layer | Arms |
|------|-------|------|
| `BROKER_CONNECTIVITY_ENABLED` | backend runtime | Customer broker-account journey (WP1A) |
| `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` | frontend build-time | Customer Broker Accounts UI (WP4.2) |
| `OPERATIONS_EVENTS_ENABLED` | backend runtime | Operational-event recording + API (WP5.1/5.2) |
| `NEXT_PUBLIC_OPERATIONS_ENABLED` | frontend build-time | Internal Operations & Support UI (WP5.3) |
| `BROKER_CONNECTIVITY_HEALTH_ENABLED` | backend runtime | Continuous broker-health engine (WP3) |
| `BROKER_CONNECTIVITY_EXECUTION_GATE` | backend runtime | Execution eligibility enforcement (WP1B/WP2) |

**Cross-flag rule:** broker-health-driven pause/resume and dispatch refusal require **both**
`BROKER_CONNECTIVITY_EXECUTION_GATE` and `BROKER_CONNECTIVITY_HEALTH_ENABLED` ON. Backend runtime flags are
read live (toggle without restart); the two `NEXT_PUBLIC_*` flags are compiled in and require a **rebuild**.

## Authoritative sources (ADRs)

ADR-0026 (capability) · 0027 (login-validation primitive + timeout contract) · 0028 (WP1A backend
lifecycle) · 0029 (execution gate + pause/resume) · 0030 (health engine) · 0031 (frontend source-of-truth +
parity) · 0032 (operational-event model + operator UI). See each doc for line-level citations.

## What this package does NOT duplicate

Existing production operations docs remain authoritative and are **referenced, not restated**:

- **Restart / recovery / emergency-stop / health-check procedures** → `docs/OPERATIONS_RUNBOOK.md` §1–13.
- **Estate map, service inventory, healthcheck gaps, risk register, backup gap** → `docs/OPERATIONS_DASHBOARD.md` §1–9.
- **Logging / correlation-id / metrics foundation** → `docs/OBSERVABILITY.md`.
- **Traefik 502 stale-routing fix** → `docs/RUNBOOK.md`.
- **Secrets (names/locations only)** → `docs/SECRET_INVENTORY.md`.
- **Exceptions ledger format** → `docs/OPERATIONAL_EXCEPTIONS.md`.
- **Handoff + evidence schema** → `docs/HANDOFF_TEMPLATE.md`, `evidence/schema/evidence-manifest.schema.json`.

## Hard boundary (this package)

Do **not**, on the basis of this package: deploy, rebuild production images, restart services, enable flags,
mutate runtime/execution state, modify Customer Zero, access credentials, run `NEGOTIATE` / `VALIDATE_LOGIN`,
place test orders, start/pause/resume runtimes, create production alerts or external notifications, change
customer-facing behaviour, create WP6 test accounts, or begin multi-tenant certification. Every arming
action is **Sponsor-gated** and executed only under a separate, explicit authorisation.

## Conventions

- **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL** marks any fact the repository cannot prove (production flag
  values, host image/task state, keyring provisioning, deploy status).
- No secret or environment-variable **values** appear anywhere in this package — flag/secret **names** only.
- "Rollback = set flag OFF" is preferred over any destructive database rollback wherever the merged design
  supports it (all six flags do, for the DARK→armed direction).
