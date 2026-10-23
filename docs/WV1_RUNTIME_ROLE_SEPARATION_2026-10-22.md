# WV1 — Bootstrap/Runtime-Role Separation Redesign (F-8) + Disposable Rehearsal (2026-10-22)

**Directive:** PostgreSQL bootstrap/runtime-role separation redesign (P0). **Disposable design + rehearsal only;
no production mutation.** Scope: Withdrawal Intelligence. **No MT5/copy-trading/OAuth change.** Production
remains **NO-GO**; this stops at the governed review gate.

---

## 2. Corrected role / ownership architecture
| Role | Login | Attributes | Owns | Purpose |
|---|---|---|---|---|
| `guvfx` (bootstrap, OID 10) | yes | SUPERUSER (unchanged) | system catalog (unchanged) | **Admin/recovery only.** NOT used by app/worker/migration after cutover. **Never demoted.** |
| `guvfx_schema_owner` | NOLOGIN | NOSUPERUSER/NOCREATEROLE/NOCREATEDB/NOBYPASSRLS | the application objects + application schema | Owns GuvFX app objects; the identity migrations act as. |
| `guvfx_migrate` | LOGIN | least-privilege, member of `guvfx_schema_owner`, `SET role=guvfx_schema_owner` | nothing (objects it creates are owned by the schema owner) | Governed schema migrations only. |
| `guvfx_app` | LOGIN | NOSUPERUSER/NOCREATEROLE/NOCREATEDB/NOBYPASSRLS, member of nothing | **NOTHING** | Web/worker runtime; operates via runtime grants + default privileges only. |
| erasure roles (`guvfx_erasure`, `guvfx_erasure_exec`, `guvfx_erx_*`) | per ADR-0054 | unchanged | per ADR-0054 | `guvfx_app` neither belongs to nor can assume any of them (#552 checks preserved). |

The in-place demotion of `guvfx` and any blanket `REASSIGN OWNED BY guvfx` are **prohibited**; a cluster
superuser is always retained (bootstrap `guvfx`).

## 3. Explicit application-object inventory + exclusions
- **Transferred to `guvfx_schema_owner`** (approved schema = `public`, explicit enumeration): **137 tables,
  130 sequences, 5 functions**, and the `public` schema itself. (Owned/identity sequences are NOT altered
  directly — their ownership follows their table's `ALTER TABLE … OWNER`; this was the rehearsal bug fixed:
  owned sequences carry a `pg_depend` dependency of type `i` **or** `a`.)
- **Excluded (never touched):** `pg_catalog`, `information_schema`, `pg_toast`, `pg_temp*` and any schema
  matching `^pg_`; extension-owned objects (`pg_depend` deptype `e`); objects outside the approved schema.
  (In prod these account for the 3233 `pg_catalog` + 11 `information_schema` functions, 140 system views and 68
  system tables that `guvfx` "owns" only because it is the bootstrap role.)

## 4. Faithful bootstrap-role reproduction evidence
Disposable PG15 started with `POSTGRES_USER=guvfx` → `guvfx` is **OID 10, `rolsuper=t`, superuser_count=1**,
database owner; `public` owned by `pg_database_owner`; after restoring the certified artifact and reproducing
prod, `guvfx` owns **137 public tables / 130 sequences / 5 functions** — matching production exactly.

## 5. Before / after matrices
| | guvfx (bootstrap) owns public | guvfx_schema_owner owns public | guvfx_app owns public | superusers |
|---|---|---|---|---|
| **Before** | 137 tbl / 130 seq / 5 fn | 0 | 0 | guvfx (1) |
| **After separation** | 0 | **137 / 130 / 5** | **0 / 0 / 0** | guvfx (1) — unchanged |

Privilege: `guvfx_app` NOSUPERUSER/NOCREATEROLE/NOCREATEDB/NOBYPASSRLS, member of nothing, runtime DML grants
only; `guvfx_migrate` `SET role=guvfx_schema_owner`; `guvfx` superuser retained.

## 6. Application / worker / migration smoke (under the separated roles)
- **Migrations** `broker_intelligence 0011→0022` applied **as `guvfx_migrate`** → leaf `0022`; a new table
  (`…_tradingaccountbrokercontact`) is owned by **`guvfx_schema_owner`** (migrations create schema-owner objects).
- **Provision + `verify_erasure_privilege.sql`** (app_role=`guvfx_app`) → **VERIFY OK**.
- **App smoke as `guvfx_app`:** `manage.py check` clean; `GET /health/` 200; email-JWT login 200 + token;
  `/api/auth/me/` 200; ORM reads users 9 / accounts 18 / trades 2277. No permission/ownership errors.

## 7. Negative-control results (all as `guvfx_app`, all DENIED)
CREATE in `public`; `SET ROLE guvfx_schema_owner`; `SET ROLE guvfx_migrate`; DELETE evidence; TRUNCATE evidence;
`ALTER ROLE guvfx_app SUPERUSER`; `ALTER TABLE … OWNER TO guvfx_app` (take-ownership). Also: `guvfx_migrate`
cannot become SUPERUSER; bootstrap `guvfx` remains SUPERUSER (controlled recovery path). The OLD
`demote_app_role.sql` **REFUSES** against the bootstrap/sole-superuser role.

## 9. Rollback design (preserves security during failure; never makes `guvfx_app` an owner/superuser)
| Failure | Class | Action |
|---|---|---|
| `separate_runtime_role.sql` errors mid-run | **transactionally reversible** | single BEGIN/COMMIT → auto-rollback; nothing changed |
| App cannot operate as `guvfx_app` after cutover | **configuration reversible** | re-point `DB_USER` back to bootstrap `guvfx` (redeploy prior env); fastest path; no DB change |
| Need to undo the DB separation | **DB privilege rollback** | `separate_runtime_role_rollback.sql` (explicit enumeration: ownership back to `guvfx`, revoke `guvfx_app` grants; refuses while erasure capability is provisioned; never blanket; keeps `guvfx` superuser) |
| Bad backend/worker image | **image reversible** | redeploy prior image |
| Data corruption | **recovery-point restore** | restore the certified point (RTO ≈ 68 s) |
Invariant: **always ≥1 controlled superuser (`guvfx`)**; emergency re-point to `guvfx` only if explicitly
authorised.

## 10. Production cutover sequence (DESIGN — not executed; each a Sponsor GO)
0. Deploy the hardened code (#552 + this PR) — DARK, flags OFF.
1. Fresh certified recovery point in-window (pg_dump major 15/16; verify + isolated restore-verify).
2. Pause broker_intelligence writers (minimal — no WV1 worker runs today).
3. **Mint credentials (secret store):** compute client-side SCRAM verifiers for `guvfx_app` and `guvfx_migrate`
   (cleartext never in SQL/argv/logs/Git); custody + rotation per the secret-store procedure.
4. **Run the separation via `ops/erasure/cutover.sh --execute`** (as bootstrap `guvfx`), NEVER by passing verifiers
   on the psql command line. The SCRAM verifiers are read from the environment (`GUVFX_APP_SCRAM`,
   `GUVFX_MIGRATE_SCRAM`) via `\getenv`, never argv/history/logs; export them from a 0600 file
   (`umask 077; set -a; . ./cutover_secrets.env; set +a`) then delete it. Example:
   `cutover.sh --execute --db guvfx --bootstrap-role guvfx --app-role guvfx_app --app-schema public
   --manifest <reviewed.psv> [--expect-manifest-sha256 <hex>]`. Transactional; refuses on unexpected topology,
   manifest mismatch, or missing/malformed verifiers (all fail-closed, nonzero exit). See
   `ops/erasure/README_runtime_role_cutover.md` and the 2026-10-23 CHANGES-REQUIRED return for the credential
   contract. (The earlier `-v app_scram=…/-v migrate_scram=…` form is SUPERSEDED — it would place verifiers on
   argv and in shell history.)
5. **Migrate (app-scoped, as `guvfx_migrate`)**: `migrate --plan` first (abort if any non-broker_intelligence
   app); `migrate broker_intelligence 0022`.
6. **Provision erasure** (app_role=`guvfx_app`) + `verify_erasure_privilege.sql`.
7. **Configuration cutover:** change backend + every worker env `DB_USER` from `guvfx` → `guvfx_app` (migration
   jobs use `guvfx_migrate`); rollout order backend then workers; **connection-drain** old `guvfx` sessions.
8. **Validate no process still connects as `guvfx`:** `pg_stat_activity` shows zero app/worker sessions as
   `guvfx` (only the admin/recovery identity may connect, manually).
9. Operator credential custody + readiness READY (via `guvfx_erx_*`), then synthetic canary, monitoring.
10. Arm `BROKER_INTELLIGENCE_ERASURE_ENABLED` — separate final GO, only when a real erasure is authorised.
STOP/GO + evidence + abort + rollback per stage as in the rollback table; T0 = stage 4.

## 11. Remaining risks
- **F-2** (HIGH, procedure): production migration must be app-scoped (`migrate broker_intelligence 0022`).
- **F-3** (MED): broker_public `MetricParticipationConsent.owner CASCADE` → ORM `User.delete()` fails on prod
  (table unmigrated). Blocks **only** User-deletion workflows (none scheduled → dormant), **not** this cutover,
  the WV1 migration, deployment, or erasure. Separate broker_public dependency.
- **Config-cutover complexity:** the runtime switch to `guvfx_app` must drain all `guvfx` app connections across
  backend + ~9 worker containers; a missed worker would keep connecting as the superuser. Validation (stage 8)
  is mandatory.
- Disk `/` at 90% (standing); single logical recovery point (re-take in-window); #552 + this PR not yet deployed.
- SCRAM-verifier custody: a SCRAM verifier is NOT cleartext, but it is still sensitive (it carries the salt +
  ServerKey). CORRECTION to an earlier claim here that "verifiers are safe in SQL/logs": `ALTER ROLE … PASSWORD
  '<verifier>'` WOULD be written to the server statement log under `log_statement`/`log_min_duration_statement`,
  so `separate_runtime_role.sql` now wraps the cutover transaction in `SET LOCAL log_statement='none'` +
  `log_min_duration_statement=-1`, validates verifier format in bash before psql, and reads them only via env
  `\getenv`. Residual: a separately-configured auditor (pgAudit) is NOT governed by those GUCs and must be handled
  by operational config for the cutover window. Cleartext never enters SQL/argv/logs/Git; it comes from the
  secret store and is destroyed after minting.

## 1 / 8 / 12 / 13
- **1. New PR + head SHA:** **PR #554**, head **`3401062a`** (branch `fix/runtime-role-separation`, base
  `feec863d`). HELD for Programme Director review — not self-merged (stopped at the governed review gate).
- **8. Adversarial review:** independent 3-lens review (PostgreSQL privilege-escalation + ownership + holistic)
  + synthesis — **no P0/P1**. Three P2/none items, two fixed before this head: (a) aggregates handled via
  `ALTER AGGREGATE` (was `ALTER FUNCTION`; fail-closed, no aggregate in prod); (b) a C4 re-run guard added to
  `separate_runtime_role.sql` (refuse if the governed proc already exists). The reviewer confirmed: enumeration
  stays within the approved non-system schema and excludes extension/system objects; owned/identity sequences
  ride their table; no blanket `REASSIGN OWNED`; the topology guard fails closed; `guvfx` is never demoted and a
  superuser is always retained; `guvfx_app`/`guvfx_migrate` cannot escalate directly or transitively; the SCRAM
  path has no cleartext/injection. Re-rehearsed after the fixes (happy path green; re-run guard REFUSES).
- **12. Production GO/NO-GO:** **NO-GO** — design + faithful disposable rehearsal complete and proven, exact-head
  CI green, adversarial review clean. Production awaits PD review of PR #554 and separate, per-stage window
  authorization.
- **13. Exact next Sponsor authorization:** review and (on GO) governed DARK merge of PR #554
  (`fix/runtime-role-separation`); **authorize no production change.** The subsequent first production-changing
  action would be stage 1 (fresh recovery point, read-only) then stage 4 (`separate_runtime_role.sql`), each a
  separate Sponsor GO.
