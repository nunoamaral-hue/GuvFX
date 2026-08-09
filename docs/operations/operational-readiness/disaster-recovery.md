# Disaster Recovery — GuvFX (ADR-0035)

Recovery procedures for the GuvFX estate. This document is **repository guidance**; every step is
performed by an operator through the sanctioned mechanism. Nothing here is automated by the platform, and
none of the Operational Readiness tooling executes a recovery — it only *reports*.

> **First action in any incident:** capture state before changing anything.
> ```bash
> python manage.py operational_health --json > incident-health.json
> python manage.py hosted_workspace_preflight --json > incident-preflight.json
> ```
> These are read-only and safe to run during an incident.

## Estate at a glance

| Component | Where | Recovery owner |
|-----------|-------|----------------|
| Backend / DB / frontend / Traefik / Guacamole | OVH VPS `guvfx-ubuntu` (100.119.23.29) | operator |
| PostgreSQL 16 | container on the VPS | operator |
| MT5 host + trade bridge (:8788) + beta agent (:8791) | Windows Server `guvfx-windows-mt5` (100.79.101.19) | operator |
| Off-site / forensic backup | Synology NAS `nas` (100.73.130.98) | operator |

## 1. Database recovery (highest priority)

**Known gap (RED, from the operations estate review): there is no automated DB backup.** Establishing one
is a Sponsor-gated production action and is the single most important DR investment. Until then:

1. **Before any restore, take a fresh dump** (never restore over an un-backed-up database):
   `pg_dump` the running database to a timestamped file and copy it off-box to the NAS.
2. Restore into a **new** database first and verify, then cut over — never restore in place blind.
3. After restore, run the Golden Reference STOP-check (ADR-0021) and `hosted_workspace_preflight` to
   confirm integrity (node-binding agreement, no orphaned state) before re-enabling anything.

## 2. Backend service recovery

1. Confirm the intended release with the deploy-parity oracle: `GET /api/version/` (staff) → compare
   `git_commit` across the shared backend image (guvfx-backend / trade-ingest / shadow).
2. Recreate the affected container from the intended image tag (or the rollback tag `rollback-preADR0021`
   if rolling back — see `rollback-runbook.md`). **Migrate-first** on any forward deploy.
3. Verify: `/health/` (liveness), `/api/version/` (provenance), `operational_health` (subsystem rollup).

## 3. MT5 host / bridge recovery

- **Never start a production service from an interactive SSH session** (Permanent RULE 1 — a
  session-bound process dies with the session). Use only the supported service/task mechanism
  (scheduled task / SCM service).
- The trade bridge and beta agent are supervised services; recovery is a supervised restart, then confirm
  the listener + NEGOTIATE health as documented in the agent monitoring runbooks.
- **Golden runtime integrity:** never promote a production terminal to a golden image (RULE 10); the
  golden manifest pin is authoritative.

## 4. Guacamole / delivery recovery

Guacamole is the delivery front door. It is not required for trade execution (viewer ≠ trading). Recovery
is a container recreate on the VPS; delivery descriptors are minted on demand and short-lived, so there
is no delivery state to restore. RemoteApp itself depends on the (not-yet-certified) RDS host.

## 5. Post-recovery verification (always)

```bash
python manage.py operational_health          # expect no unexpected FAULT subsystem
python manage.py hosted_workspace_preflight   # expect the pre-incident verdict (BLOCKED_ON_SPONSOR while DARK)
```
Compare against the `incident-health.json` captured at step 0. Record a short evidence manifest:
`python manage.py collect_operational_evidence --packet-id DR-<incident> --out evidence/manifests/dr-<incident>.json`.

## Standing DR gaps (returned to the Chief Architect / Sponsor)

- **No automated database backup** (RED) — the top DR priority; a production/Sponsor action.
- **Single-VPS SPOF** — backend, DB, Traefik and Guacamole share one host; no warm standby.
- **Alert delivery** is DARK by default (Telegram sink unarmed) — an incident may not page anyone until
  armed.

These are recorded here as known risks; remediating them is Sponsor-gated production work, out of scope
for this repository-only subsystem.
