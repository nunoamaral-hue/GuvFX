# Operational Readiness (ADR-0035)

A **purely read-only** operational layer that aggregates GuvFX's existing per-subsystem health signals
into one operator view and answers *"is the system healthy, and is it safe to enable Hosted Workspace?"*
It owns no state, writes nothing, contacts no host, and authorises nothing. It is safe to run at any time.

## The seven health states (no fake READY)

| State | Meaning |
|-------|---------|
| `HEALTHY` | a real source confirms the subsystem is up and nominal |
| `DEGRADED` | running but impaired / stale / partially observed (includes "enabled but no data") |
| `MAINTENANCE` | intentionally paused by an operator |
| `OFFLINE` | a source says the subsystem is down / disconnected / unreachable |
| `MISCONFIGURED` | required config / credential / flag posture missing or inconsistent |
| `BLOCKED` | an upstream dependency or gate prevents use |
| `AWAITING_SPONSOR` | intentionally DARK, waiting for a Sponsor / host gate (expected, not a failure) |

**Rule:** a subsystem is `HEALTHY` **only** when a real source says so. Dark-by-design subsystems are
`AWAITING_SPONSOR`; enabled-but-unobserved ones `DEGRADED` (`observed=false`); a probe that raises fails
**open** to `DEGRADED`. Darkness is never silently counted as health.

## Subsystems covered

`backend`, `database`, `cache` (live probes) · `workers`, `bridge`, `mt5`, `guacamole` (recorded
`ComponentHealth` / host-gated) · `broker_health` (WP3), `agent_monitor`, `operational_events` (WP5) ·
`hosted_workspace`, `delivery`, `execution`, `onboarding` (Hosted Workspace family, DARK by default).

## Tools (all read-only; the CLI needs no flag)

```bash
# Unified health rollup (7-state, per subsystem)
python manage.py operational_health              # table
python manage.py operational_health --json

# Authoritative Hosted Workspace pre-flight (fails closed; honest about the host gate)
python manage.py hosted_workspace_preflight
python manage.py hosted_workspace_preflight --json

# Safe flag-disable rollback plan (executes nothing)
python manage.py rollback_plan
python manage.py rollback_plan --json

# Repeatable, schema-conformant evidence manifest
python manage.py collect_operational_evidence --packet-id OPS-READINESS --handoff-id <id> \
    --out evidence/manifests/ops-readiness.json
```

Staff-only DARK API (set `OPERATIONAL_READINESS_API_ENABLED=1` to make it visible; 404 while off):

```
GET /api/operational-readiness/                 # health + preflight + rollback
GET /api/operational-readiness/?section=health  # one section: health | preflight | rollback
```

## What it does NOT do

- It never enables a flag, arms execution, deploys, mutates a row, or contacts a host.
- It never authorises an order — the live bridge gate remains the sole order-time authority.
- It does not fabricate health for components it cannot observe.

## Interpreting the current (DARK) baseline

With every hosted / broker / ops flag OFF and the disposable RDS host not yet provided, the honest
readout is: `backend`/`database` HEALTHY; local `cache` DEGRADED unless Redis is configured;
`workers`/`bridge`/`mt5` DEGRADED/unobserved until `ComponentHealth` rows exist; every Hosted Workspace
subsystem and the broker/agent/event engines `AWAITING_SPONSOR`. The pre-flight verdict is
`BLOCKED_ON_SPONSOR` once node capacity exists (host certification is the standing external gate) or
`NOT_READY` if a hard prerequisite (e.g. an ACTIVE execution node) is missing.

See also: `disaster-recovery.md`, `rollback-runbook.md`, `production-readiness-checklist.json`, and the
Notion **Hosted Workspace — Host Certification Record**.
