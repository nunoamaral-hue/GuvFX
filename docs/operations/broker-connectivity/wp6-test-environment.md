# WP6-A — Certification Environment

The environment in which WP6 certification runs. **Disposable only** — no production, no Customer Zero, no
live accounts. Matrix cases: `ENV-1..4` in [`wp6-test-matrix.json`](wp6-test-matrix.json).

> **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL.** Standing up this environment requires the Windows host and
> a Nuno-provided disposable demo account; the repository cannot provision or verify it. This document
> specifies what the environment must be; the Operator stands it up under a separate Sponsor-gated
> authorisation.

## Composition

- **Disposable demo accounts** — one or more broker accounts that are genuine demo accounts
  (`trade_mode == 0` → `is_demo=true`), created solely for certification and discarded afterwards. Never a
  live/contest account (`live_detected`), never Customer Zero (account #12).
- **Disposable users** — test users that own the demo accounts; not production users.
- **Runtime allocation** — disposable beta slots (the per-slot low-privilege identity / directory / task
  model), never a production or Customer-Zero slot. The RUNNING runtime is broker-independent/view-only;
  exposure opens only later at `ExecutionJob.save` (see [wp6-execution-safety.md](wp6-execution-safety.md)).
- **Validation image** — the governed build-5833 isolated validation terminal, `verify_image` PASS, with the
  6073 baseline retained as rollback (`ENV-2`).
- **Execution path** — the standard pipeline (creation → claim → dispatch), exercised only against demo
  accounts, with flags armed **only inside the disposable environment** for the duration of a certification
  run (never production).
- **Disposable agent + bridge + worker host** — a **dedicated, isolated** Windows agent, signal/demo bridge
  and ingest worker, separate from the shared production host. This is **load-bearing for failure injection
  and recovery**: `FAIL-2` (agent unavailable), `FAIL-6` (bridge unavailable), `FAIL-11` (network
  interruption) and `REC-2` (agent restart) **stop/interrupt the agent/bridge/host**, so they MUST run
  against a disposable agent/bridge/host — **never the shared production Windows agent** (`100.79.101.19`),
  which Customer Zero and production execution depend on. Stopping the shared agent would disrupt Customer
  Zero's live estate. If a fully separate host is not available, these four cases are **BLOCKED** until one
  is — they must not be run against production. HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL.
- **Monitoring** — the log plane (`guvfx.operational_events`, `guvfx.execution.*`), the DB/endpoint plane
  (`operations-summary`, `AuditEvent`, `AlertEvent`), observable in the disposable env (`ENV-4`).
- **Logging + evidence capture** — every run emits an evidence manifest conforming to
  `evidence/schema/evidence-manifest.schema.json` (`ENV-3`; see [wp6-evidence.json](wp6-evidence.json)).

## Exclusions (mandatory)

- **Customer Zero excluded** — account #12 is never enrolled, never validated, never executed against; a
  golden STOP-check before/after proves no drift; CZ order-count stays 0 (cross gate `GATE-CZ`).
- **Live production accounts excluded** — no live/contest broker account participates.
- **Production estate excluded** — certification never runs against the production containers or the
  production database; a disposable environment stands in.
- **Shared production Windows agent/bridge/host excluded from disruptive tests** — agent/bridge/host-stopping
  cases (`FAIL-2`, `FAIL-6`, `FAIL-11`, `REC-2`) run only against a **disposable agent/bridge/host**, never
  `100.79.101.19`. Disrupting the shared agent would take down Customer Zero and production execution — a
  contamination path this environment is designed to prevent.

## Environment readiness checks (ENV-1..4)

| Case | Check | PASS criteria | Repo evidence |
|------|-------|---------------|---------------|
| ENV-1 | Disposable demo accounts + users provisioned; CZ + live excluded | ≥1 demo account (all `is_demo=true`); CZ + live never touched | `trading/tests_broker_connectivity.py`, `terminal_provisioning/tests_beta_capacity.py` |
| ENV-2 | Validation image staged + verified | `verify_image` PASS (≥100 run-in `.ex5`, source hashes match); 6073 retained | `terminal_provisioning/tests_validation_image.py` |
| ENV-3 | Evidence capture wired | Each run emits a schema-valid manifest (status ∈ PASS/PARTIAL/FAIL) | `evidence/schema/evidence-manifest.schema.json` |
| ENV-4 | Monitoring + logging observable | Log/DB/endpoint planes observable; recorder-failure log empty on healthy runs | `reliability/tests_operations_summary.py` |

## Evidence

An environment-readiness manifest recording: the disposable account/user inventory (masked), demo
classification, `verify_image` output, a sample evidence manifest, and monitoring/log samples. CZ-absence and
production-absence are recorded explicitly (limitations section of the manifest). No secret or account number
appears verbatim.
