# WV1 / ADR-0054 F-8 — Production runtime-role separation cutover plan (PROPOSAL ONLY, 2026-10-23)

**Status:** PROPOSAL. **Nothing in this plan has been executed.** Every mutating stage is a SEPARATE
Sponsor-authorised production action with its own GO. Production disposition: **NO-GO** until the PD authorises,
per stage. **No MT5 / dashboard / Copy-Trader change. No OAuth change.** The forward cutover does **not** change
application behaviour; the erasure capability stays behind `BROKER_INTELLIGENCE_ERASURE_ENABLED` (**OFF**).
**Base:** canonical main `3586b540` (F-8 `75e5469a` is in main's history).

## Fresh production observations (READ-ONLY, 2026-10-23, inside a `READ ONLY` transaction on `guvfx-postgres`)
- PostgreSQL **15.15** (Alpine/musl, x86_64). DB `guvfx` owned by `guvfx`.
- `guvfx` is the **sole superuser** (count = 1); inherit/createrole/createdb/login/bypassrls all on.
- `guvfx` owns **137 tables, 130 sequences, 689 indexes, 5 routines** in `public`. All 5 routines are
  **trigger functions** (plpgsql, SECURITY INVOKER); **0 SECURITY DEFINER** routines.
- Schema `public` owner = **`pg_database_owner`** (PG15 default — *not* yet `guvfx`).
- `broker_intelligence` migrations applied in prod: leaf **`0010`**; repo leaf **`0022`** → **12 migrations
  pending**. `broker_public` has **no** migrations applied in prod (repo has `0001`,`0002`).
- **No** `guvfx_schema_owner` / `guvfx_migrate` / `guvfx_app` / `guvfx_evidence_owner` roles exist. `pg_default_acl`
  empty. No non-system role memberships.
- App/worker containers currently connecting as `guvfx`: `guvfx-backend`, `guvfx-frontend` (static), and workers
  `mt5-trade-ingest`, `wayond-listener`, `customer-notification`, `tp-protection-watcher`, `mt5-node2-order`,
  `mt5-shadow`, `mt5-validate`, `beta-provisioner` (≈9 worker connections to drain).

> Implication: the cutover must additionally `ALTER SCHEMA public OWNER TO guvfx_schema_owner` (prod's `public`
> is still `pg_database_owner`), and the in-window manifest **must be re-captured** (prod is behind the rehearsal
> reference at `0010`, not `0022`).

---

## Stage-gated sequence (T0 = Stage 9, the first DB-privilege mutation)

| # | Stage | Mutates prod? | Verification | Commit / rollback boundary |
|---|---|---|---|---|
| 1 | **Fresh encrypted recovery point** via `ops/recovery/` — streamed `pg_dump | gpg` of `guvfx` to the approved **NAS** (Tailscale `100.73.130.98`), off-host. | Read-only on DB (dump) | dump size > prior; gpg recipient correct; checksum recorded | n/a (read-only); artifact retained |
| 2 | **Integrity verify + disposable restore** — restore the stream into an **ephemeral** PG15 cert cluster (initdb), run `pg_restore`/`psql -c '\dt'` counts; compare table/sequence counts to §obs (137/130). | No (disposable) | restore exit 0; counts match 137/130/5; row-count spot-check | discard cert cluster |
| 3 | **Production object-manifest capture** — `cutover.sh --capture --db guvfx --app-schema public` against prod (read-only; `SET default_transaction_read_only`). | **No** (read-only) | manifest line-count = live object count; exactly 2 `protected` rows; well-formed (regex) | n/a |
| 4 | **Human review + pinned SHA-256** — 2 reviewers diff the manifest vs §obs and vs the rehearsal reference; classify every object `application`/`protected`; commit the reviewed `.psv`; record `MANIFEST_SHA256`. | No | both reviewers sign; digest recorded in packet | n/a — **GO gate**: no `--execute` without the pinned digest |
| 5 | **Maintenance window + user-impact statement** — brief outage of broker_intelligence writes (minimal: no WV1 worker runs today); publish window; no impact to MT5/trading. | No | window announced; on-call named | n/a |
| 6 | **Writer drain + active-session verification** — stop/pause broker_intelligence-touching writers; confirm `pg_stat_activity` shows no app writers mid-cutover. | No (ops) | `pg_stat_activity` reviewed | re-enable writers on abort |
| 7 | **Current role/ownership/privilege inventory** — snapshot (the §obs query) committed as the pre-cutover baseline. | No (read-only) | baseline attached to packet | n/a |
| 8 | **Credential generation + custody** — mint SCRAM-SHA-256 **verifiers** for `guvfx_app` and `guvfx_migrate` in the secret store; write to a `0600` env file; **cleartext never in SQL/argv/logs/Git**; two-person custody. | No (secret store) | verifiers format-valid; file `0600`; cleartext destroyed after | n/a |
| 9 | **T0 — forward separation** — `cutover.sh --execute --db guvfx --bootstrap-role guvfx --app-role guvfx_app --app-schema public --manifest <reviewed.psv> --expect-manifest-sha256 <pinned>` with `GUVFX_APP_SCRAM`/`GUVFX_MIGRATE_SCRAM` in env. Single transaction; fail-closed. | **YES** (one txn) | script post-checks (app owns nothing; no protected DELETE/TRUNCATE; schema_owner owns objects; `public` owner = schema_owner; membership closure; `POST_OWNER_DIGEST`) | **COMMIT boundary = the script's own BEGIN/COMMIT**; any error → auto-ROLLBACK, nothing changed |
| 10 | **DB configuration** — none at the server level required by F-8 (no `postgresql.conf`/`pg_hba` change). *If* Option-C pgAudit is adopted, enable TRUNCATE/DROP/DELETE auditing on evidence tables here. | YES (only if pgAudit) | `SHOW shared_preload_libraries` includes pgaudit (if adopted) | reload revert |
| 11 | **Migration order** — as `guvfx_migrate`: `migrate --plan` (abort if any app other than broker_intelligence/broker_public is pending); `migrate broker_intelligence 0022`; `migrate broker_public 0002`. | **YES** | `showmigrations` leaf = 0022 / 0002; new objects owned by `guvfx_schema_owner` | per-migration; on failure, restore (Stage 1) |
| 12 | **Erasure-role provisioning order** — `provision_erasure_privilege.sql` (app_role=`guvfx_app`) creating `guvfx_erasure*`/executor roles; **flag stays OFF**. | **YES** | `verify_erasure_privilege.sql` → VERIFY OK (incl. block 9 SECDEF containment) | `production_rollback_erasure.sql` (F-8-aware; drops proc+executor, preserves ownership boundary) |
| 13 | **Per-stage verification commands** — after 9/11/12 run the inventory query + `verify_erasure_privilege.sql`; attach outputs as evidence. | No (read-only) | all green on the exact post-state | n/a |
| 14 | **Explicit commit/rollback boundary per mutation** — 9 = transactional (auto-rollback); 11 = per-migration + recovery-point restore; 12 = `production_rollback_erasure.sql`; config (10) = reload revert. | — | each boundary rehearsed (Phase 4) | — |
| 15 | **Application configuration change + controlled restart** — change `DB_USER` `guvfx`→`guvfx_app` for backend + all ≈9 workers (migration jobs use `guvfx_migrate`); roll backend first, then workers; **drain** old `guvfx` sessions. | YES (app config) | each container healthy post-restart | re-point `DB_USER`→`guvfx`, redeploy (fastest rollback) |
| 16 | **Read-only smoke tests** — `GET /health/` 200; email-JWT login 200; `/api/auth/me/` 200; ORM reads (users/accounts/trades); `pg_stat_activity` shows **zero** app/worker sessions still connecting as `guvfx`. | No (read traffic) | all 200; zero `guvfx` app sessions | abort → Stage 15 rollback |
| 17 | **Erasure flag remains OFF** — confirm `BROKER_INTELLIGENCE_ERASURE_ENABLED` unset/false everywhere. | No | grep env of backend+workers | n/a |
| 18 | **Rollback-vs-proceed criteria** — proceed only if every stage's verification is green and smoke passes; otherwise roll back at the nearest boundary (config re-point is the fast path; recovery-point restore is the last resort). | — | documented decision per stage | — |
| 19 | **Estimated outage** — broker_intelligence writer pause ≈ the Stage 9 transaction + Stage 11 migrations (~minutes; no large table rewrites in 0011→0022 — to be confirmed by `migrate --plan`). Trading/MT5 unaffected. | — | time each stage in a final dry-run | — |
| 20 | **Named operator + two-person approvals** — Operator: **Nuno (PD/Sponsor)** or delegate; two-person GO required at: Stage 4 (manifest sign-off), Stage 8 (credential custody), Stage 9 (T0), Stage 12 (erasure provisioning), and any later Stage-17→arming step. | — | sign-offs recorded in Notion (PM-owned lifecycle) | — |

## Reversal ordering (if teardown needed)
Reverse in opposite order with the **F-8-aware** scripts: (1) `production_rollback_erasure.sql`; (2) config
rollback `DB_USER`→`guvfx` + redeploy + **drain**; (3) `cutover.sh --rollback` (refuses while any `guvfx_app`
session is connected, unless `--emergency-override` with justification). **Do not** use
`rollback_erasure_privilege.sql` in prod (pre-F-8 disposable-rehearsal teardown; refuses fail-closed on F-8).

## Hard stops (this plan does not do any of these without a separate, explicit Sponsor GO)
Create a production recovery point; capture a production manifest if it required mutation (it does not — read-only);
create/modify production roles; change production ownership/privileges; generate/install production credentials;
run production migrations; deploy/restart production services; provision erasure; enable an erasure flag; execute
an erasure; alter OAuth; touch MT5 / dashboard / Copy-Trader.
