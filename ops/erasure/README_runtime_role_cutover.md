# Runtime-role separation cutover (ADR-0054 / Finding F-8)

DARK tooling. SUPERUSER-ONLY. Each step is a SEPARATE Sponsor-authorised production action. Nothing here runs
against production without that authorisation; the forward cutover does not change application behaviour (the
erasure capability stays behind `BROKER_INTELLIGENCE_ERASURE_ENABLED`, default OFF).

## Why

Production `guvfx` is the bootstrap role (OID 10): the sole superuser and database owner. The runtime, migrations
and the erasure capability must not all run as that superuser. This separates them WITHOUT demoting `guvfx`:

| Role | Login | Owns | Role |
|---|---|---|---|
| `guvfx` (bootstrap) | yes | system catalog (unchanged) | admin / recovery only; **never demoted**; a controlled superuser is always retained |
| `guvfx_schema_owner` | NOLOGIN | **all** application objects + schema `public` | ownership identity migrations act as |
| `guvfx_migrate` | LOGIN | nothing (creates schema-owner-owned objects via `SET role`) | governed schema migrations |
| `guvfx_app` | LOGIN | **nothing** | web/worker runtime; minimal runtime grants only |

## Scripts

- `capture_app_manifest.sql` — READ-ONLY. Emits `obj_kind|obj_schema|obj_identity|current_owner|classification`
  for every application object in the approved schema (extension-owned excluded). The two evidence tables are
  classified `protected`; everything else `application`.
- `cutover.sh` — wrapper with three modes:
  - `--capture` : run the read-only capture into a reviewable `.psv` file (+ prints `MANIFEST_SHA256`).
  - `--execute` : build the reviewed manifest into a TEMP table `_cutover_manifest` (via `\copy FROM STDIN`, so
    object names never hit argv), then run `separate_runtime_role.sql`. SCRAM verifiers are read from the
    environment (`GUVFX_APP_SCRAM`, `GUVFX_MIGRATE_SCRAM`) by the script's `\getenv` — never on the command line.
  - `--rollback` : same manifest preamble + `separate_runtime_role_rollback.sql`.
- `separate_runtime_role.sql` — the governed separation (transactional, fail-closed).
- `separate_runtime_role_rollback.sql` — DB-privilege reversal (manifest-driven + active-session guard).
- `cutover_manifest.rehearsal.psv` — a REFERENCE manifest captured from a faithful bootstrap-`guvfx` PG15
  reproduction of the schema (312 rows: 155 tables, 149 sequences, 8 routines; 2 protected). SHA-256
  `af7c63fed7449e7633f293bbbff34d4a3dfb06f642864174fc01157ace25cb4f`. **It is a rehearsal reference, not the
  production manifest** — production is currently behind on migrations, so the operator re-captures in-window
  with `cutover.sh --capture` against the restored snapshot and reviews the diff against this reference before
  `--execute`.

## How the CHANGES-REQUIRED findings are met

- **F1 (membership closure, fail-closed):** `separate_runtime_role.sql` computes the direct+transitive membership
  closure of `guvfx_app`/`guvfx_schema_owner`/`guvfx_migrate` (`WITH RECURSIVE`) BEFORE any mutation and again
  before COMMIT, and RAISEs on any unexpected membership. It never silently revokes an unexpected membership.
- **F2 (minimal grants):** `guvfx_app` gets `SELECT/INSERT/UPDATE/DELETE` on `application` tables, `SELECT/INSERT/
  UPDATE` (NO DELETE/TRUNCATE) on `protected` evidence tables, `USAGE/SELECT/UPDATE` on sequences, and **no
  function EXECUTE**. Default privileges grant **no DELETE and no EXECUTE**, so future migration-created objects
  are DELETE-safe and never auto-executable. `verify_erasure_privilege.sql` and `privileged_backend_readiness()`
  additionally assert PUBLIC and the runtime role can execute **no** SECURITY DEFINER routine.
- **F3 (explicit reviewed manifest):** the script consumes a reviewed manifest and compares it against the live
  inventory BEFORE mutating; it RAISEs on any missing / unexpected / owner-mismatch / unclassified object,
  transfers ONLY manifest-approved objects, and emits a before (`MANIFEST_DIGEST`) and after
  (`POST_OWNER_DIGEST`) digest. The rollback reversal is manifest-driven too.
- **F4 (credentials off argv):** verifiers flow only through the environment into psql via `\getenv`; the wrapper
  passes `docker exec -e NAME` (name only, no value), writes no secret to the combined SQL, runs with
  `set +o history` and `umask 077`, and removes the combined file on exit/error/interrupt. Rehearsal proved zero
  verifier/password occurrences in any process argv (positive control confirmed the psql process was observed).

## Reversal ordering (IMPORTANT)

For a production teardown, reverse in the opposite order of application and use the **F-8-aware** scripts:
1. `production_rollback_erasure.sql` (drops the governed proc + executor roles; **preserves** the ownership
   boundary — it does NOT return evidence/schema to the app role and does NOT drop `guvfx_schema_owner`).
2. Configuration rollback: re-point `DB_USER` back to `guvfx`, redeploy, and DRAIN every app/worker connection.
3. `cutover.sh --rollback` (refuses while any `guvfx_app` session is still connected, unless
   `--emergency-override`).

Do **not** use `rollback_erasure_privilege.sql` in the F-8 model: it is the pre-F-8 DISPOSABLE-REHEARSAL teardown
and would hand ownership to the app role and `DROP OWNED BY guvfx_schema_owner` (destroying the schema). It now
refuses fail-closed when it detects the F-8 model.
