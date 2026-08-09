# 0035 — Operational Readiness Subsystem

- Date: 2026-08-09
- Status: Proposed (repository-complete, additive, read-only) — PM owns lifecycle status
- Relates to: ADR-0030 (broker-health engine), ADR-0032 (operational event model), ADR-0021
  (deploy/rollback plan), ADR-0034 (Hosted Workspace umbrella) and its Host Certification Record.

## Context

All four Hosted Workspace boundaries (State Core, Execution Engine, Workspace Delivery, Onboarding) are
repository-complete and DARK on `main`; host certification is blocked purely on infrastructure (a
disposable RDS host). While that gate is with the Sponsor, the **operational layer** around the platform
was still fragmented: broker health (WP3), the operational event model (WP5), agent monitoring, execution
readiness/health, and the Hosted Workspace read models each expose their own state vocabulary, and the
root `/health/` endpoint is a hard-coded `{"status":"ok"}`. There was **no single operator view** of
"is the system healthy, and is it safe to enable Hosted Workspace?", and no repeatable, machine-readable
readiness/rollback/evidence tooling that spans them.

## Decision

Add a small, **purely read-only, additive** Operational Readiness layer in `core/` that *aggregates* the
existing authoritative sources — it introduces **no new state store, no model, no migration**, mutates
nothing, contacts no host, and authorises nothing.

- **D1 — One honest 7-state health vocabulary.** `core/operational_health.py` maps every subsystem into
  exactly the operator states `HEALTHY · DEGRADED · MAINTENANCE · OFFLINE · MISCONFIGURED · BLOCKED ·
  AWAITING_SPONSOR`. The load-bearing rule is **no fake READY**: a subsystem is `HEALTHY` only when a
  real source confirms it; anything dark-by-design is `AWAITING_SPONSOR`, anything enabled-but-unobserved
  `DEGRADED` (with `observed=False`), and a raising probe fails **open** to `DEGRADED` — never a crash,
  never a fabricated pass. The overall rollup is the worst *fault* (OFFLINE > MISCONFIGURED > BLOCKED >
  DEGRADED); expected darkness never counts as health.

- **D2 — One authoritative, read-only pre-flight.** `core/preflight.py` answers "is everything required
  before enabling Hosted Workspace actually true?" from the live system (database, cache, active-node
  capacity, node-binding integrity, delivery config, flag posture) and is honest about the gates it
  cannot satisfy: host certification is always `BLOCKED`, the live bridge remains the sole order authority
  (`INFO`). It **fails closed** — a check that cannot run is `FAIL`, and the verdict is only `READY` when
  a repository can prove it; a host-cert-blocked system reports `BLOCKED_ON_SPONSOR` (`ready=False`).

- **D3 — Rollback is a plan, never an execution.** `core/rollback_planner.py` reads the current flag
  posture and prints the **flag-disable** DARK rollback (the rollback-matrix rule: prefer a flag toggle
  over any destructive DB rollback; every arming flag supports instant DARK rollback with no data loss).
  It changes no flag, touches no DB, contacts no host; the deploy-image lever (`rollback-preADR0021`) is
  reference-only.

- **D4 — Readiness as repeatable, schema-conformant evidence.** `core/operational_evidence.py` +
  `collect_operational_evidence` turn the three views above into an evidence manifest conforming to
  `evidence/schema/evidence-manifest.schema.json`; the manifest `status` is honestly `PARTIAL` while the
  host-cert gate is the only blocker.

- **D5 — Operator tooling + a DARK console data source.** Four always-available read-only management
  commands (`operational_health`, `hosted_workspace_preflight`, `rollback_plan`,
  `collect_operational_evidence`) and one staff-only (`IsAdminUser`) API `GET /api/operational-readiness/`
  gated DARK behind `OPERATIONAL_READINESS_API_ENABLED` (404-invisible while off; the CLI needs no flag).
  Everything returned is non-secret (subsystem states, flag booleans, node/counts).

## Security / governance boundary

- **Green:** additive read-only modules + commands + a flag-dark staff API + docs. No model, no migration,
  no write path, no host contact, no order authority.
- **Amber:** none — nothing here changes an established pattern, gate, or security posture.
- **Red (NOT taken):** enabling any flag, arming execution, deploying, or touching a host.

## Evidence / validation

- `core.tests_operational_readiness` — 27 tests: 7-state vocabulary; backend/DB genuinely healthy; dark
  subsystems `AWAITING_SPONSOR` (never HEALTHY); unobserved components never HEALTHY; a raising probe
  fails open to DEGRADED; pre-flight fails closed on missing capacity and is `BLOCKED_ON_SPONSOR` with
  capacity present; host cert always BLOCKED; rollback plan is non-destructive and executes nothing;
  evidence manifest schema-conformant (`status=PARTIAL`); all four commands run; the API is 404 while
  dark, staff-only, section-filterable. All pass.
- `make check`: backend suite green; frontend lint/build unchanged (backend-only change).
- **Not covered (stated limitation):** live host/guacd/RDS probing (out of scope — read-only backend);
  component-level health for workers/bridge/MT5 reflects recorded `ComponentHealth` rows only (absent
  rows read as DEGRADED/unobserved, never HEALTHY).

## Reversal path

Delete the additive modules/commands/route; there is no model, migration, or persisted state to unwind.
The staff API defaults OFF, so nothing is exposed until an operator sets `OPERATIONAL_READINESS_API_ENABLED`.

## Amendment (2026-08-09) — evidence-driven host-certification stage

The original `preflight._check_host_certification` hard-coded a **permanent** `BLOCKED` (Sponsor) status.
That was correct while host certification could not begin, but it can never turn green, so it would lie
once certification actually proceeds. Corrected: `core/host_cert.py` exposes a durable, config-driven
**stage** — `NOT_STARTED → IN_PROGRESS → BLOCKED_ON_HUMAN → CERTIFIED` — read from
`HOSTED_HOST_CERT_STAGE` (settings-first-then-env; unrecognised values **fail safe to NOT_STARTED**, never
CERTIFIED). The pre-flight `host.certification` check now reflects the stage: `BLOCKED` for
`NOT_STARTED`/`IN_PROGRESS`/`BLOCKED_ON_HUMAN`, and `PASS` only for a recorded `CERTIFIED`. The load-bearing
rule holds — a **feature flag never makes certification green**; only a recorded `CERTIFIED` stage (backed
by durable certification evidence) does. When the Customer-Zero host certification completes, setting the
stage to `CERTIFIED` clears the previously-permanent block and the pre-flight verdict can become `READY`.

## Revisit trigger

The disposable-host certification proceeds (the pre-flight `host.certification` check + the health
`delivery`/`execution` subsystems become genuinely certifiable), or a new subsystem is added that should
register a health probe.
