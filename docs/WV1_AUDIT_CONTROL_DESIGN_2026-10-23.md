# WV1 / ADR-0054 F-8 — Evidence-destruction audit-control design + runbook (DARK, 2026-10-23)

**Status: DARK DESIGN + RUNBOOK. NOT enabled in production.** This is the audit control required by the interim
TRUNCATE disposition (condition 7) when **pgAudit is unavailable** in production. Enabling any part of it in
production is a separate, Sponsor-gated step (prod DDL and/or a container restart). DARK SQL artifact:
`ops/erasure/audit_evidence_controls.sql`. Rehearsal evidence: `docs/WV1_RUNTIME_ROLE_STAGE8_RETURN_2026-10-23.md`
and `scratchpad/audit/phase4_audit2.sh`.

## 0. Why pgAudit is not used
Read-only production inspection: `pgaudit` is **not** in `pg_available_extensions` (image `postgres:15-alpine`
does not bundle it), `shared_preload_libraries` is **empty**, `logging_collector=off`, `log_statement=none`.
pgAudit cannot be used without a new image + restart. This design is the tested alternative.

## 1. Two-layer design

### Layer A — always-on, bounded, capacity-safe (PRIMARY)
A dedicated schema **`audit` owned by the bootstrap role** (NOT `guvfx_schema_owner`), containing an append-only
`audit.evidence_audit` table, a SECURITY DEFINER writer, DML triggers, and event triggers
(`ops/erasure/audit_evidence_controls.sql`):
- `AFTER DELETE` + `AFTER TRUNCATE` statement triggers on both evidence tables → one bounded audit row per event.
- `sql_drop` event trigger → audits any DROP of an evidence table (or the audit schema).
- `ddl_command_end` event trigger → audits any DDL on an evidence table, **including `ALTER TABLE … DISABLE
  TRIGGER`** — so an owner who tries to defeat the DML triggers is recorded (tamper-evident).
- **Capacity: zero cluster-wide log volume** — a handful of rows per *real* destructive event (which should be
  ~never). This is the control that makes always-on auditing safe on the constrained production disk.

**Non-bypassable by the trusted migration identity** (proven, Phase 4): `guvfx_migrate` (member of
`guvfx_schema_owner`, which owns schema `public`) **cannot** drop/disable the event triggers or drop/modify the
`audit` schema — schema ownership of `public` does not extend to another schema, and the event triggers are
bootstrap-owned. The audit-writer is SECURITY DEFINER so writes always land. `guvfx_app`/`guvfx_erasure_exec`
have no access to the `audit` schema at all.

### Layer B — `log_statement='mod'` window-scoped bounded canary (PD-specified, defense-in-depth)
`log_statement='mod'` is the **non-suppressible statement record** for the trusted migration identity: it is a
**SUSET** GUC, so `guvfx_migrate` cannot change it (proven: session `SET` and `ALTER SYSTEM` both denied). It
captures DELETE, TRUNCATE **and** DROP statements. It is enabled **only during a migration window** (a bounded
canary), never always-on, because of the capacity profile below.

## 2. Production capacity (read-only, 2026-10-23) + measured volume
- Host `/`: **193 GB, 93% used, ~15 GB free** — Docker, container logs and PGDATA all share it.
- Docker log driver: **`json-file` with NO rotation configured** (empty `Config`) → unbounded by default.
- `logging_collector=off`, `log_destination=stderr`, `log_statement=none`; `pg_stat_statements` absent.
- Write rate: ~65 commits/s but **~12 data-modifying statements/s** (SELECTs are not logged by `mod`).
- Measured (disposable PG15): **~85 bytes per logged statement** → **~84 MB/day always-on**, **~3.5 MB/hour
  windowed**, **~0.6 MB per 10-minute migration window**.
- **Verdict:** always-on `log_statement='mod'` with today's unbounded json-file driver on a 93%/15 GB-free disk
  is **unsafe**. Window-scoped + rotation + free-space auto-stop is **safe** (sub-MB per window).

## 3. Production risks — explicitly addressed
| Risk | Mitigation |
|---|---|
| Cluster-wide log-volume growth | Layer A adds **none**; Layer B is window-scoped (sub-MB/window), never always-on |
| Previously constrained storage (15 GB free) | window-scoping + json-file `max-size`/`max-file` rotation + a **free-space auto-stop** (disable `log_statement` when free < floor) |
| Sensitive values in SQL logs | Layer A rows carry only actor/action/object/row-count — **no row contents**. Layer B logs statement text; evidence is append-only so DELETE/TRUNCATE statements carry no evidence payload (at most an owner-id predicate) |
| `ALTER ROLE … PASSWORD` / verifier exposure | the cutover transaction wraps credential statements in `SET LOCAL log_statement='none'` + `log_min_duration_statement=-1` (proven: verifier never reaches the log); `guvfx_migrate` never sets passwords |
| Docker/container log retention | json-file `--log-opt max-size=100m --log-opt max-file=5` (≈500 MB cap) **or** `logging_collector=on` with `log_rotation_size/age` — rotation **proven** to bound retained logs |
| Off-host shipping | ship rotated segments to the NAS / a log collector; **DB is unaffected if shipping fails** (proven — shipping is async/external) |
| Rotation size/count | start `max-size=100m`, `max-file=5`; tune from the measured ~84 MB/day |
| Access control | `audit` schema `REVOKE ALL FROM PUBLIC` (only bootstrap reads); log files owner-restricted; off-host store access-controlled |
| Retention duration | align audit-row + shipped-log retention to the WV1 backup/evidence retention (30–90 days) |
| Alerting on protected-evidence ops | a scanner over `audit.evidence_audit` (and/or the shipped log) pages on-call on any DELETE/TRUNCATE/DROP of evidence (**proven** fires) |
| Safe disable/rollback | `DROP EVENT TRIGGER …; DROP SCHEMA audit CASCADE; ALTER SYSTEM RESET log_statement; pg_reload_conf()` — **proven** clean; evidence intact |
| Restart vs reload | `log_statement` via `ALTER SYSTEM` + **reload** (no restart); docker log-driver rotation needs a **container recreate/restart** (Sponsor-gated); `logging_collector` needs a **restart** |

## 4. Bounded-canary procedure (the next Sponsor-gated step — NOT run here)
**Preconditions (each Sponsor-gated):** (a) install Layer A in production (`audit_evidence_controls.sql`, bootstrap,
prod DDL); (b) configure a **bounded** log destination — either docker `max-size`/`max-file` (container recreate)
or `logging_collector` with rotation (restart); (c) an off-host shipping target; (d) a free-space **auto-stop**
monitor (disable `log_statement` when free < floor, recommended floor **≥ 10 GB** given 15 GB free).
**Canary:** enable `log_statement='mod'` (ALTER SYSTEM + reload) at the **start of a single short maintenance
window** (recommended **≤ 60 min**, ideally scoped to the actual migration window), with auto-stop conditions:
free-space floor breached, retained-log ceiling reached, or window end. **Disable** (`ALTER SYSTEM RESET` +
reload) at window end. Review `audit.evidence_audit` + the shipped log; confirm every evidence TRUNCATE/DROP/DELETE
that occurred is present.
**Auto-stop conditions:** (1) free space < floor; (2) retained logs > ceiling; (3) elapsed > canary duration.

## 5. Rehearsal evidence (disposable PG15 — Phase 4)
All proven: Layer A install (4 DML triggers + 2 event triggers, bootstrap-owned); destructive ops captured by the
audit table **and** the log; `guvfx_migrate` cannot suppress `log_statement` (SUSET) or drop/disable the event
triggers or drop the audit schema; **tamper (disabling an evidence DML trigger) captured by the ddl event
trigger**; `guvfx_app`/`guvfx_erasure_exec`/`guvfx_migrate` cannot modify audit records; **credential/verifier
operations suppressed** (0 leaks); **rotation bounds retained logs** (25 000 statements → 18 633 retained, ~1.5 MB
bounded); **free-space auto-stop** halts logging with the DB still serving; off-host shipping works and a shipping
failure does not affect the DB; **clean disable/rollback** with evidence intact.

## 6. Hard stop
This packet does **not** enable production logging, install extensions, create production event triggers/triggers,
alter production roles/privileges, run migrations, deploy/restart services, or touch OAuth/MT5/dashboard/Copy-Trader.
