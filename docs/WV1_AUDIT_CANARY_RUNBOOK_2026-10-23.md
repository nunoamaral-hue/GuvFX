# WV1 / ADR-0054 F-8 — Bounded audit-control canary runbook (DARK, 2026-10-23)

**Status: DARK PROPOSAL. NOT executed.** This is a precise, time-bounded production proposal for installing the
Layer-A evidence-destruction audit control and running a single ten-minute bounded logging canary. **Nothing here
runs** until the human roles are resolved (`WV1_SPONSOR_NOMINATION_FORM_2026-10-23.md`) and the two explicit GOs
below are given. It introduces **no destructive operation on real evidence**. Rehearsed end-to-end on disposable
PostgreSQL 15 — see `WV1_AUDIT_CANARY_REHEARSAL_2026-10-23.md`.

## Reconciliation with the merged designs
- **Runtime-role separation (F-8).** Roles and ownership are exactly as merged: bootstrap `guvfx` owns the `audit`
  schema and all its objects; `guvfx_schema_owner` owns `public` + the evidence tables; `guvfx_migrate` reaches
  the owner only via `SET ROLE`; `guvfx_app` / `guvfx_erasure_exec` own nothing. The canary changes **no** role,
  grant, ownership or password.
- **Recovery (`ops/recovery/`).** Stage 1 validates the most recent **certified** recovery point + checksum before
  any change; it reuses the existing streamed `pg_dump | gpg` artifact on the NAS. No new dump is taken here.
- **Manifest (`WV1_MANIFEST_REVIEW_PACK_2026-10-23.md`).** Stage 6 reconciles live ownership/identity against the
  reviewed manifest digest `1ffe99ac…`; a mismatch is a hard stop.
- **Audit-control design (`WV1_AUDIT_CONTROL_DESIGN_2026-10-23.md`) + SQL (`ops/erasure/audit_evidence_controls.sql`).**
  Layer A (always-on, bounded, OID-based) is installed in stage 5; Layer B (`log_statement='mod'`, reload-only,
  window-scoped) is the canary in stages 8–11. Capacity profile and worst-case overhead are from that design.

## Roles (resolve via the nomination form; Operator != Observer is mandatory)
Sponsor = Nuno Rodolfo Ferreira Do Amaral (Virtus Logic UK Ltd). Manifest Reviewer 1 = Nuno. **Manifest Reviewer 2,
Production Operator, Observer/rollback authority = named humans, UNRESOLVED.** The AI agent executes **nothing** and
holds **no** role.

## Required GOs (each explicit, separate, Sponsor-gated)
1. **Human roles resolved** (nomination form signed; Operator != Observer).
2. **Layer-A install GO** (stage 5 — production DDL, additive, reversible).
3. **Logging-canary GO** (stage 8 — reload-only `log_statement` window).

## Preconditions before anything mutates
Free space **>= 12 GB**; off-host (NAS) log destination reachable; a bounded log destination configured out-of-band
(json-file `max-size`/`max-file`, or `logging_collector` with rotation — a container recreate / restart that is its
own separately-gated change, **not** part of this reload-only canary); watchdog armed (disk floor **10 GB**, log
ceiling agreed, deadline **<= 10 min**); Operator and Observer both present and reachable.

## The 15 stages
Legend — **Class:** RO = read-only · RM = reversible mutation · D = destructive (there are **no** D steps on real
evidence). Durations are maxima.

| # | Stage | Role | Prerequisites | Exact proposed action | Expected evidence | Stop condition | Rollback action | Max | Class |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Validate recovery point + checksum | Operator (Observer witnesses) | GO#1 | Verify latest certified `pg_dump\|gpg` artifact on NAS; recompute SHA-256, compare to recorded | checksum match; artifact present | recovery missing/unverifiable | abort (no change made) | 10m | RO |
| 2 | Disk capacity + off-host destination | Operator | stage 1 | Check free space; confirm NAS/log target writable | free >= 12 GB; dest reachable | insufficient headroom; NAS unavailable | abort | 5m | RO |
| 3 | Capture images/identities/grants/schema/start-times | Operator | stage 2 | Read-only capture of container image ids, DB identities, grants, schema, `pg_postmaster_start_time()` | captured snapshot stored off-host | capture fails | abort | 5m | RO |
| 4 | Confirm no unapproved app writer sessions | Observer | stage 3 | Query `pg_stat_activity` for active non-approved writers | zero unapproved active writers | unapproved active writer present | abort | 2m | RO |
| 5 | Install Layer-A in one atomic transaction | Operator | **GO#2**, stages 1–4 | Run `ops/erasure/audit_evidence_controls.sql` as bootstrap (`BEGIN…COMMIT`) | install rc=0; schema/table/registry/functions/triggers/event-triggers present | install transaction failure | automatic (atomic) + `DROP SCHEMA audit CASCADE` | 5m | RM |
| 6 | Verify catalog: ownership/ACL/search_path/OID registry/event-trigger | Observer | stage 5 | Run catalog assertions (A1–A8) | all pass; registry pins 2 tables + 4 triggers; funcs secdef + empty search_path; event triggers enabled, bootstrap-owned | identity/ownership differs from manifest; any PUBLIC EXECUTE; unexpected search_path; OID registry mismatch | `DROP SCHEMA audit CASCADE` | 5m | RO |
| 7 | Confirm runtime/migration/erasure cannot tamper | Observer | stage 6 | Negative controls (B1–B7): attempt (and be denied) drop/disable/forge/suppress | every attempt denied; zero audit rows written by the attempts | any tamper attempt succeeds | `DROP SCHEMA audit CASCADE` | 5m | RO |
| 8 | Begin bounded logging canary (reload-only) | Operator | **GO#3**, stage 7, watchdog armed | `ALTER SYSTEM SET log_statement='mod'; SELECT pg_reload_conf();` | `SHOW log_statement`='mod'; `pg_postmaster_start_time()` unchanged (no restart) | cannot enable by reload alone; any restart observed | `ALTER SYSTEM SET log_statement='none'` + reload | 1m | RM (config) |
| 9 | Run approved synthetic canary operations | Operator | stage 8 | Writes to a dedicated `canary.*` schema only; **never** touch real evidence | synthetic statements appear in the log | any operation targets real evidence | reset logging; drop canary objects | 3m | RM (synthetic) |
| 10 | Monitor disk floor / log ceiling / health / audit events | Observer | stage 8 | Continuously watch free space, retained-log size, DB health, expected audit events | within floor/ceiling; DB healthy; only expected events | floor/ceiling breach; health degradation; unexpected audit event; watchdog failure | trigger stage 11 | 10m | RO |
| 11 | Auto-reset logging at deadline or on any failure | Watchdog (Operator owns; Observer can force) | stage 8 | `ALTER SYSTEM SET log_statement='none'; SELECT pg_reload_conf();` (runs even if a prior step failed) | final `SHOW log_statement`='none' | logging cannot be reset | escalate to full Layer-A rollback | 1m | RM (config) |
| 12 | Remove synthetic canary objects | Operator | stage 11 | `DROP SCHEMA canary CASCADE` | canary schema absent | objects remain | retry drop; escalate | 2m | RM |
| 13 | Export audit evidence off-host | Operator | stage 12 | Read-only `\copy`/`COPY` of `audit.evidence_audit` to NAS | export file on NAS (header + any destructive rows; a clean canary destroys nothing, so 0 data rows is expected and correct) | export target unavailable (DB unaffected) | retry later; DB continues | 3m | RO |
| 14 | Verify final log_statement / app health / no credential leakage | Observer | stage 13 | Confirm `log_statement`='none'; app health green; scan log for verifier/secret strings | none; healthy; zero credential/secret hits | credential/token/verifier/SQL-secret exposure; health degraded; logging not reset | full Layer-A rollback | 5m | RO |
| 15 | Accept / rollback decision | **Observer / rollback authority** (Sponsor ratifies) | stage 14 | Decide: **ACCEPT** (retain Layer A as the always-on control; logging stays off) or **ROLLBACK** (`DROP SCHEMA audit CASCADE` after off-host archive) | signed decision in the decision record | — | if ROLLBACK: `DROP SCHEMA audit CASCADE` (event triggers first) | 10m | decision (RM on rollback) |

## Hard-stop matrix (ABORT immediately; reset logging; hold for Observer)
Any one of these halts the canary and triggers the stage-appropriate rollback:

| # | Hard stop | Detected at | Response |
|---|---|---|---|
| 1 | Recovery point missing or unverifiable | 1 | abort before any change |
| 2 | NAS / off-host target unavailable | 2, 13 | abort (pre-change) / continue DB, defer export (post-change) |
| 3 | Insufficient disk headroom (< 12 GB) | 2, 10 | abort / auto-reset logging |
| 4 | Identities or ownership differ from approved manifest | 6 | `DROP SCHEMA audit CASCADE` |
| 5 | Unapproved active writer sessions | 4 | abort |
| 6 | OID registry mismatch | 6 | `DROP SCHEMA audit CASCADE` |
| 7 | Any PUBLIC function EXECUTE privilege | 6 | `DROP SCHEMA audit CASCADE` |
| 8 | Any unexpected search_path | 6 | `DROP SCHEMA audit CASCADE` |
| 9 | Installation transaction failure | 5 | automatic atomic rollback |
| 10 | Logging cannot be enabled by reload alone | 8 | reset; do not proceed |
| 11 | Database / container restart observed | 8–11 | reset logging; full rollback; incident |
| 12 | Log growth breaches the approved ceiling | 10 | auto-reset logging |
| 13 | Disk free space crosses the approved floor (10 GB) | 10 | auto-reset logging |
| 14 | Credential / token / verifier / SQL-secret exposure | 14 | reset; full rollback; rotate; incident |
| 15 | Unexpected audit event | 10 | halt; Observer decision |
| 16 | Watchdog failure | 10, 11 | manual reset; full rollback |
| 17 | Inability to reset logging | 11 | escalate; incident; manual intervention |
| 18 | Application health degradation | 10, 14 | auto-reset logging; full rollback |
| 19 | Operator or Observer unavailable | any | Observer (or Sponsor) orders rollback; do not continue single-handed |

## Approvals
- [ ] GO#1 — human roles resolved (nomination form signed; Operator != Observer). Sponsor: ______ Date: ______
- [ ] GO#2 — Layer-A install (production DDL, additive, reversible). Sponsor: ______ Date: ______
- [ ] GO#3 — logging canary (reload-only, <= 10 min). Sponsor: ______ Date: ______

**No GO is implied by any other.** Dashboard/FX, MT5 and Copy Trader are out of scope and untouched. Production
remains NO-GO until all three GOs are given and every precondition holds at execution time.
