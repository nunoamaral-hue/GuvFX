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

> **Hardening (reviewed 2026-10-23, F1/F2/F3; adversarially reviewed then re-rehearsed 17/17 — see §13/§14).**
> Every privileged function is `SECURITY DEFINER` with a **fixed empty `search_path`** and all non-catalog
> objects fully schema-qualified; `PUBLIC` (and every non-bootstrap role) has **no EXECUTE** on any of them; there
> is **no dynamic SQL**. **Detection keys on stable catalog OIDs, not object names** (the adversarial review found
> and we fixed a rename-then-drop name-match bypass — §14). The audit row carries **bounded metadata only** (§8).
> The whole install runs in **one transaction** (atomic rollback on any failure) and is **idempotent** (version
> marker `guvfx-evidence-audit/2`, OID registry re-synced each run), with a **fail-closed guard** that refuses a
> pre-existing foreign `audit` schema. Self-checking harness: `ops/erasure/audit_evidence_controls_test.sql`.

### Layer A — always-on, bounded, capacity-safe (PRIMARY)
A dedicated schema **`audit` owned by the bootstrap role** (NOT `guvfx_schema_owner`), containing an append-only
`audit.evidence_audit` table, a SECURITY DEFINER writer, DML triggers, and event triggers
(`ops/erasure/audit_evidence_controls.sql`):
- `AFTER DELETE` + `AFTER TRUNCATE` statement triggers on both evidence tables → one bounded audit row per event.
- `sql_drop` event trigger → audits any DROP of a protected object — matched on the **stable catalog OID**
  (registry `audit.protected_objid`), never the mutable name, so a `DROP` after `ALTER … RENAME` is still caught.
- `ddl_command_end` event trigger → audits any DDL on a protected object (again by OID), **including
  `ALTER TABLE … RENAME` and `ALTER TABLE … DISABLE TRIGGER`** — so an owner who tries to rename-then-drop, or to
  defeat the DML triggers, is recorded (tamper-evident). The four DML triggers' OIDs are pinned too, so **dropping
  a DML audit trigger is itself captured** (closing the rename→drop-trigger→silent-DELETE path).
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

---

# Privileged-SQL review (Directive D, 2026-10-23)

## 7. F1 — SECURITY DEFINER hardening: ownership / ACL matrix + fixed-search-path proof
Exact catalog state after installing `audit_evidence_controls.sql` as the bootstrap role `guvfx` (disposable PG15;
`pg_get_userbyid`, `prosecdef`, `proconfig`, `proacl`, `relacl`, `nspacl`):

| Object | Owner | SECURITY DEFINER | search_path | ACL (grantee=privs/grantor) | PUBLIC EXECUTE? |
|---|---|---|---|---|---|
| schema `audit` | `guvfx` (superuser) | n/a | n/a | `{guvfx=UC/guvfx}` | no (REVOKE ALL FROM PUBLIC) |
| table `audit.evidence_audit` | `guvfx` | n/a | n/a | `{guvfx=arwdDxt/guvfx}` (owner only) | no |
| sequence `evidence_audit_id_seq` | `guvfx` | n/a | n/a | `{guvfx=rwU/guvfx}` (owner only) | no |
| table `audit.protected_objid` (OID registry) | `guvfx` | n/a | n/a | owner only (REVOKE ALL FROM PUBLIC) | no |
| function `audit._write` | `guvfx` | **true** | **`""` (empty)** | `{guvfx=X/guvfx}` | no |
| function `audit.on_evidence_dml` | `guvfx` | **true** | **`""`** | `{guvfx=X/guvfx}` | no |
| function `audit.on_sql_drop` | `guvfx` | **true** | **`""`** | `{guvfx=X/guvfx}` | no |
| function `audit.on_ddl_end` | `guvfx` | **true** | **`""`** | `{guvfx=X/guvfx}` | no |
| event trigger `et_evidence_sql_drop` | `guvfx` | n/a | n/a | n/a | enabled |
| event trigger `et_evidence_ddl_end` | `guvfx` | n/a | n/a | n/a | enabled |

**Proof points (all asserted by the harness A1–A7, §13):**
- **Owner** is the bootstrap NOLOGIN-equivalent superuser `guvfx` for *every* object — never `guvfx_schema_owner`
  or any app/migration/erasure role. (`guvfx` is the cluster's sole superuser and is the trusted owner here.)
- **SECURITY DEFINER** is used on all four functions *because* writes must land in a schema the caller cannot
  reach; it is used *only* there.
- **Fixed safe search_path = `""`** (stored by PostgreSQL as `search_path=""`). With an empty path, `pg_catalog`
  is still searched first for built-ins and **no other schema is searched at all** — so no object a non-bootstrap
  role could create (in `public`, in `pg_temp`, anywhere) can satisfy an unqualified name. Every non-catalog
  reference is fully schema-qualified (`audit.evidence_audit`, `audit._write`); the only unqualified name is the
  trigger transition table `deleted`, which resolves as a transient range-table entry independent of search_path
  (empirically confirmed). ⇒ **the function body cannot be redirected through an attacker-created object.**
- **PUBLIC has no EXECUTE** on any function (`acl={guvfx=X/guvfx}` = owner only); **no app/migration/erasure
  role is granted EXECUTE** on any function, nor USAGE on the schema. They therefore cannot call `_write` to
  forge rows.
- **No dynamic SQL** anywhere (no `EXECUTE`/`format()` in any body) ⇒ no identifier-injection surface.
- **Detection is OID-based, not name-based** (A8 + C5/C6/C7): the event triggers match the dropped/altered
  object's `objid` against the bootstrap-owned registry `audit.protected_objid` (the two evidence tables + the
  four DML triggers, pinned at install). An OID survives `ALTER … RENAME` and is still reported at `DROP` time, so
  rename-then-drop (and rename-then-drop-trigger-then-DELETE) is captured; a lookalike-named object has a
  different OID and is ignored (no audit-table pollution / DoS).
- **Actor attribution** uses `session_user` (the authenticated login, unchanged by `SET ROLE` or by nested
  SECURITY DEFINER entry) plus `current_setting('role')` (the effective role). Verified: a `DELETE` by login
  `guvfx_migrate` under `SET ROLE guvfx_schema_owner` records `actor=guvfx_migrate, effective_role=guvfx_schema_owner`.
- **No sensitive data** enters the row (see §8).

## 8. F3 — Audit semantics + data-minimization
**Bounded-metadata record** `audit.evidence_audit`:
`id bigint, at timestamptz, actor text, effective_role text, action text, obj text, txid bigint, session_pid integer, window_id text, detail text`.
It carries **only**: timestamp (`at`), authenticated actor (`actor`=`session_user`), effective role
(`effective_role`=`current_setting('role')`), operation class (`action`), protected object identity (`obj`),
transaction id (`txid`), session identifier (`session_pid`), approval/window id where supplied
(`window_id`=`current_setting('audit.window_id')`, else NULL), and a bounded `detail` (`rows=<n>` | `statement` |
object type / DDL tag). **It never stores row contents, credentials, verifier values, tokens, or arbitrary SQL
text.** For a `DELETE` only the row *count* is recorded, never the rows.

**Four distinguished cases (documented + tested):**
| Case | What happens | Where recorded |
|---|---|---|
| Attempted destructive op **rejected** by immutability controls (e.g. app/erasure/migrate denied) | statement errors; no mutation | rejection is in the DB server log (and, in a canary window, `log_statement`); **no** `evidence_audit` row (nothing was destroyed) — tested B5/B6 |
| **Successful** `DELETE`/`TRUNCATE`/`DROP` of evidence | mutation occurs | one `evidence_audit` row via DML trigger (`DELETE`/`TRUNCATE`) or `sql_drop` event trigger (`DROP`) — tested C1/C2 |
| **Tamper** on an evidence table/trigger (disable trigger, `ALTER … RENAME`, drop a DML audit trigger) | DDL occurs (owner-available) | captured by the `ddl_command_end`/`sql_drop` event triggers via the pinned OID — tested C3 (disable), C5 (rename-then-drop), C6 (drop DML trigger) |
| **Privileged maintenance under an approved window** | mutation occurs with `audit.window_id` set | `evidence_audit` row carries the `window_id`, distinguishing sanctioned from unsanctioned — tested C1 |

Note: the "rejected" case is intentionally *not* an `evidence_audit` row — the DB-server log (Layer B canary)
captures the attempt; the table records only what actually touched evidence, keeping it bounded (§9).

## 9. F4 — Retention and bounded growth (enforced, not merely stated)
- **Growth is bounded by construction**, not by a cap: the DML triggers are `FOR EACH STATEMENT` (one row per
  destructive *statement*, never per row) and fire **only** on `DELETE`/`TRUNCATE` (not `INSERT`/`UPDATE` — proven:
  200 inserts + updates ⇒ 0 audit rows, §13). Destructive evidence ops are rare and human-gated, so expected
  growth is **~0 rows/day** in normal operation; even an active erasure window adds a handful of rows.
- **Max expected rows/day:** single digits (one per owner-scoped erasure statement). **Alert threshold:** page
  on-call on **any** `DELETE`/`TRUNCATE`/`DROP` row, plus a rate alarm at **> 20 rows/hour** (far above design).
- **Append-only is enforced**, not assumed: the table grants nothing to any non-owner role (`{guvfx=arwdDxt/guvfx}`),
  and no role has USAGE on the `audit` schema. Proven: `guvfx_schema_owner` (hence `guvfx_migrate`),
  `guvfx_app`, `guvfx_erasure_exec` cannot `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE` the table (B5/B6).
- **Privileged archival/pruning:** only the bootstrap role can prune; pruning is an explicit operator step
  (`DELETE … WHERE at < now() - retention` then `VACUUM`), never automatic — audit evidence is **never**
  auto-deleted under growth pressure. **Proof ordinary roles cannot prune:** B5 (schema_owner DELETE/TRUNCATE
  denied); app/erasure have no access at all (B6).
- **Off-host export:** `\copy` (or a read-only `COPY`) of `evidence_audit` to the NAS alongside the recovery
  dumps; retained for the WV1 evidence/backup retention (recommended **≥ 90 days**, aligned to the retention
  schedule), and the audit rows are additionally captured by any routine `pg_dump` recovery point. **If export
  fails the DB is unaffected** (export is external/async) and rows remain in-table — proven (§13 step 9).
- **Table-growth / disk alerts:** monitor `pg_total_relation_size('audit.evidence_audit')` and row count; because
  normal growth is ~0, any increase is itself an alert signal.
- **Recovery + verification:** the audit table is included in the recovery dump; verify by row count and by the
  monotonic `id`/`at` ordering; reconcile destructive rows against the maintenance-window change transcript.
- **The always-on Layer A table is bounded; it is Layer B (`log_statement`) that must never be always-on** on the
  93 %-full host (§2). Layer A adds **no** cluster-wide log volume.

## 10. F5 — Ten-minute logging canary (reloadable-only; NO database-container recreation)
The production DB container has unbounded Docker JSON logging. This canary **does not recreate that container**
and **does not change the Docker log driver**; it uses **reloadable PostgreSQL configuration only**
(`log_statement` is SIGHUP/reloadable — `ALTER SYSTEM SET` + `pg_reload_conf()`, **no restart**). Design/rehearsal
only — **not run in production under this packet.** Rehearsed end-to-end (§13 step 8).

**Worst-case Docker-JSON overhead (≥ 4× safety factor):** ~85 B/stmt × ~12 mod-stmt/s × 600 s × **4** ≈ **2 MB**
for a full 10-minute window — negligible against the ≥ 12 GB free-space precondition. (Docker JSON per-line
wrapping is well inside the 4× factor.)

**Watchdog procedure (dedicated canary objects; destructive-free on real evidence):**
1. **Precondition:** start **only if** free space ≥ **12 GB**; else abort without enabling logging.
2. **Record** start configuration (`log_statement`, timestamp).
3. **Enable:** `ALTER SYSTEM SET log_statement='mod'; SELECT pg_reload_conf();` (reload — **not** restart; proven
   by unchanged `pg_postmaster_start_time()`).
4. **Validate** with **dedicated canary objects only** (`canary.probe`) — never a destructive op on real evidence.
5. **Suppress credential/verifier statements:** any `ALTER ROLE … PASSWORD` is wrapped
   `BEGIN; SET LOCAL log_statement='none'; SET LOCAL log_min_duration_statement=-1; …; COMMIT;` (proven: 0 leaks).
6. **Auto-disable** if free space falls below **10 GB** (floor), if a retained-log ceiling is hit, or at window end.
7. **Disable at end even if a step fails** (a `trap … RETURN`/`finally` always runs `ALTER SYSTEM SET
   log_statement='none'; pg_reload_conf()`).
8. **Verify** the final production setting is back to `log_statement='none'`.
9. **Remove** temporary canary objects under a defined rollback (`DROP SCHEMA canary CASCADE`).
10. **DB stays operational throughout** (proven after auto-stop).

**Rotation/off-host (separate, Sponsor-gated prerequisite, needs a container recreate — NOT in this canary):**
docker `--log-opt max-size=100m --log-opt max-file=5` (≈500 MB cap) **or** `logging_collector` with rotation;
ship rotated segments off-host; DB unaffected if shipping fails.

## 11. F6 — Exact production mutation matrix + rollback
Two Sponsor-gated steps, kept separate. **Step A (Layer A install)** is the always-on control; **Step B (canary)**
is the window-scoped `log_statement` test. Nothing here runs under this packet.

### Step A — install Layer A (`audit_evidence_controls.sql`, one transaction, as bootstrap)
| Mutation | Precondition | Txn boundary | Verification | Rollback | Failure behaviour | Expected lock | Service impact |
|---|---|---|---|---|---|---|---|
| `CREATE SCHEMA audit` (+ comment, REVOKE) | no foreign `audit` schema (fail-closed guard) | inside the single `BEGIN…COMMIT` | `A1` | `DROP SCHEMA audit CASCADE` | txn aborts, nothing persists | catalog row locks only | none |
| `CREATE TABLE audit.evidence_audit` (+ REVOKE) | schema created | same txn | `A2` | cascaded by schema drop | txn aborts | catalog only | none |
| `CREATE FUNCTION` ×4 (SECURITY DEFINER, empty path, REVOKE) | schema created | same txn | `A3/A4/A5` | cascaded by schema drop | txn aborts | catalog only | none |
| `CREATE TRIGGER z_audit_*` ×4 on evidence tables | evidence tables exist | same txn | `A7` | `DROP TRIGGER …` | txn aborts | **SHARE ROW EXCLUSIVE** on each evidence table (blocks writes, not reads, for the ms of creation) | evidence **ingestion paused briefly** — run in the maintenance window |
| `CREATE EVENT TRIGGER et_evidence_*` ×2 | functions exist | same txn | `A6` | `DROP EVENT TRIGGER …` | txn aborts | cluster event-trigger catalog lock (brief) | none |

Install is **atomic** (any failure ⇒ full rollback; proven §13 step 6) and **idempotent** (re-runnable; proven step 4).

### Step B — bounded canary (config only, reloadable)
| Mutation | Precondition | Txn boundary | Verification | Rollback | Failure behaviour | Expected lock | Service impact |
|---|---|---|---|---|---|---|---|
| `ALTER SYSTEM SET log_statement='mod'` + reload | free ≥ 12 GB; Layer A installed; bounded log dest + off-host + auto-stop in place | own statement (ALTER SYSTEM cannot run in a txn block) | `SHOW log_statement`='mod'; start-time unchanged | `ALTER SYSTEM SET log_statement='none'` + reload | watchdog disables on any error | none (SIGHUP reload) | **no restart**; marginal log I/O only |
| `CREATE SCHEMA canary` + `canary.probe` + probe writes | logging enabled | ordinary txns | writes present in log | `DROP SCHEMA canary CASCADE` | cleaned by rollback | normal | none (dedicated objects) |
| `ALTER SYSTEM SET log_statement='none'` + reload (end) | always (trap/finally) | own statement | `SHOW log_statement`='none' | n/a (this *is* the reset) | runs even if prior steps failed | none | none |

### Full rollback (Layer A), bootstrap, event-triggers-first (one txn)
```
BEGIN;
DROP EVENT TRIGGER IF EXISTS et_evidence_sql_drop;
DROP EVENT TRIGGER IF EXISTS et_evidence_ddl_end;
DROP TRIGGER IF EXISTS z_audit_del    ON broker_intelligence_evidenceblob;
DROP TRIGGER IF EXISTS z_audit_trunc  ON broker_intelligence_evidenceblob;
DROP TRIGGER IF EXISTS z_audit_del2   ON broker_intelligence_brokerevent;
DROP TRIGGER IF EXISTS z_audit_trunc2 ON broker_intelligence_brokerevent;
DROP SCHEMA audit CASCADE;
COMMIT;
```
Event triggers are dropped **first** so no cluster DDL can transiently fail while the audit schema is being
removed. Proven clean with evidence intact (§13 step 10). (Archive `evidence_audit` off-host before dropping it.)

## 12. F7 — Human governance
**Manifest approval (gates the F-8 cutover, `WV1_MANIFEST_REVIEW_PACK_2026-10-23.md`):**
- Reviewer 1: **Nuno Rodolfo Ferreira Do Amaral** (Virtus Logic UK Ltd).
- Reviewer 2: **a separate named human with sufficient technical competence — STILL REQUIRED, not yet nominated.**
- The AI agent does **not** satisfy either signature. Status: **INCOMPLETE.**

**Future audit-canary roles (named separately; not collapsed; no invented people):**
| Role | Responsibility | Holder |
|---|---|---|
| Sponsor | authorises the canary / production enablement | **Nuno Rodolfo Ferreira Do Amaral** (Programme Director / Sponsor) |
| Operator | executes the gated steps on the host under the window | **[TO BE NOMINATED — named human]** |
| Observer / rollback authority | independently watches free space + audit rows, holds the stop/rollback decision | **[TO BE NOMINATED — named human, distinct from the Operator]** |

These three are **distinct roles**; Operator and Observer/rollback authority must not be the same person, and
neither is filled by the AI agent.

## 13. Hardening rehearsal evidence (Directive D, disposable PG15 15.19)
Harness `scratchpad/audit/rehearsal_d.sh` installs the **actual committed** `audit_evidence_controls.sql` as
bootstrap against evidence tables **owned by `guvfx_schema_owner`** (the post-cutover threat case) and runs the
**actual committed** `audit_evidence_controls_test.sql`. **Result: 17/17 PASS** (post adversarial fix).
- Catalog assertions **A1–A8**: ownership, append-only, SECURITY DEFINER + fixed empty search_path, no EXECUTE to
  PUBLIC/non-bootstrap, no dynamic SQL, enabled bootstrap-owned event triggers, enabled DML triggers, **OID
  registry** (bootstrap-owned, append-only, pins 2 tables + 4 triggers).
- Negative controls **B1–B7**: the migration identity cannot drop/disable the event triggers, change the writer,
  drop the audit schema, forge/update/delete/truncate audit rows; app/erasure have no schema access; migration
  identity cannot change `log_statement` (SUSET).
- Positive/tamper **C1–C7**: `DELETE` captured with `actor`=login + `effective_role`=SET-ROLE + `window_id` +
  txid/pid; `TRUNCATE` = single row (no recursion); trigger-disable tamper captured; unrelated DDL neither blocked
  nor audited (**Django migrations unaffected**); **C5 rename-then-drop captured** (ALTER + DROP rows); **C6 drop
  of a DML audit trigger captured**; **C7 lookalike-named object not audited** (no pollution).
- Direct adversarial replay (main db): the exact rename-then-drop attack that produced **0** audit rows against
  the name-based version now produces **4** rows (ALTER + table DROP + both trigger DROPs).
- Lifecycle: **idempotent** re-run (registry re-synced to 6 OIDs); **fail-closed guard** refuses a foreign
  `audit` schema; **atomic** rollback on mid-install failure; **ingestion unaffected** (INSERT/UPDATE ⇒ 0 rows);
  **canary watchdog** (reload-not-restart, final=`none`, verifier suppressed, canary captured, cleanup, DB
  operational); **off-host export** + ship-failure resilience; **clean full rollback**.

## 14. F2 — adversarial review results (against the actual SQL diff)
A 14-agent adversarial review (5 attack lenses — search_path hijack, privilege/forge, capture evasion,
availability/migration-block, correctness/atomicity — each finding then independently verified) was run against
the actual committed SQL. It surfaced **9 raw findings; 7 confirmed by reproduction, 2 rejected**.

**Confirmed and FIXED (all one root cause, 2 agents rated it P0):** the original event-trigger filters matched the
**mutable object name** (`object_identity LIKE '%…evidenceblob%'`). The evidence-table owner (reached by the
trusted migration identity via `SET ROLE guvfx_schema_owner`) could `ALTER TABLE … RENAME` first (itself
unaudited), then `DROP`/`DELETE` with **zero audit trail** — a complete bypass of the control's purpose; a related
P2 let a lookalike name pollute the audit table. **Fix:** detection now keys on the **stable catalog OID**
(registry `audit.protected_objid`, pinning the two tables + four DML triggers), matched in both event triggers.
Reproduced-before / proven-after: the exact attack went from **0** audit rows to **4** (§13); anti-pollution C7
passes. The name-based filter is gone.

**Rejected (verified non-exploitable):** (1) "substring filter spoofable with lookalike names" — subsumed by the
OID fix, which ignores foreign OIDs; (2) "`effective_role` records `none` when no `SET ROLE`" — cosmetic; the
writer now records the login in that case, and `session_user` always carries the authenticated identity. The
search_path lens was found **sound** (a planted `pg_temp.deleted` and a malicious `public` operator were both
ignored under `search_path=''`).
