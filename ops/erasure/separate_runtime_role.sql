-- ADR-0054 / F-8 -- PRODUCTION runtime-role separation (bootstrap-superuser-SAFE cutover).
-- ============================================================================================================
-- SUPERUSER-ONLY. SEPARATELY SPONSOR-AUTHORISED. Replaces the UNSAFE in-place demotion (demote_app_role.sql) for
-- production, where the application runtime login is the PostgreSQL BOOTSTRAP role (OID 10, sole superuser, DB
-- owner, owner of the system catalog). We do NOT demote that role and we NEVER run an unrestricted
-- `REASSIGN OWNED BY <bootstrap>` (which would target pinned pg_catalog/information_schema objects).
--
-- Target separation (PD disposition):
--   <bootstrap_role>      stays SUPERUSER (admin/recovery identity only; no app/worker/migration use after cutover)
--   guvfx_schema_owner    NOLOGIN, least-privilege; OWNS the application objects + the application schema
--   guvfx_migrate         LOGIN, least-privilege, member of guvfx_schema_owner; its sessions act AS the schema
--                         owner (SET role) so governed migrations create schema-owner-owned objects
--   <app_role> (guvfx_app) LOGIN, least-privilege; OWNS NOTHING; only runtime DML/USAGE/EXECUTE + default privs;
--                         not a member of / unable to assume schema-owner, migrate, erasure or admin roles
--
-- Passwords are NEVER cleartext in SQL: LOGIN roles are created with client-computed SCRAM-SHA-256 VERIFIERS
-- passed as :app_scram / :migrate_scram (a salted hash, not the password).
--
-- Required psql vars (fail-closed if any missing):
--   -v target_db=<current db>  -v allow_destructive_provisioning=1
--   -v bootstrap_role=guvfx    -v app_role=guvfx_app    -v app_schema=public
--   -v app_scram='SCRAM-SHA-256$...'   -v migrate_scram='SCRAM-SHA-256$...'
-- Reverse with ops/erasure/separate_runtime_role_rollback.sql (explicit enumeration; never blanket).
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo '*** REFUSED: pass -v target_db=<current database name>.'
  \quit
\endif
\if :{?allow_destructive_provisioning}
\else
  \echo '*** REFUSED: pass -v allow_destructive_provisioning=1 to acknowledge.'
  \quit
\endif
\if :{?bootstrap_role}
\else
  \echo '*** REFUSED: pass -v bootstrap_role=<current bootstrap/admin superuser, e.g. guvfx>.'
  \quit
\endif
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role=<new least-privilege runtime role, e.g. guvfx_app>.'
  \quit
\endif
\if :{?app_schema}
\else
  \echo '*** REFUSED: pass -v app_schema=<approved application schema, e.g. public>.'
  \quit
\endif
\if :{?app_scram}
\else
  \echo '*** REFUSED: pass -v app_scram=<client-computed SCRAM-SHA-256 verifier for the app role>.'
  \quit
\endif
\if :{?migrate_scram}
\else
  \echo '*** REFUSED: pass -v migrate_scram=<client-computed SCRAM-SHA-256 verifier for guvfx_migrate>.'
  \quit
\endif

BEGIN;

-- DB-name guard (interpolated OUTSIDE any dollar-quoted block).
SELECT CASE WHEN current_database() = :'target_db' THEN 'true' ELSE 'false' END AS _dbok \gset
\if :_dbok
\else
  \echo '*** REFUSED: current_database() does not match -v target_db. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- Positive topology identification (fail-closed): the bootstrap_role MUST be the sole superuser, the database
-- owner, and present; the app_schema MUST be a real, non-system schema; the app_role MUST NOT already carry an
-- elevated attribute (we create/verify it least-privilege). Refuse on anything unexpected.
SELECT CASE WHEN
      (SELECT rolsuper FROM pg_roles WHERE rolname = :'bootstrap_role')
  AND (SELECT count(*) = 1 FROM pg_roles WHERE rolsuper)
  AND (SELECT pg_get_userbyid(datdba) = :'bootstrap_role' FROM pg_database WHERE datname = current_database())
  AND (SELECT count(*) = 1 FROM pg_namespace WHERE nspname = :'app_schema'
         AND nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND nspname !~ '^pg_')
  AND NOT COALESCE((SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls
                      FROM pg_roles WHERE rolname = :'app_role'), false)
  THEN 'true' ELSE 'false' END AS _topo_ok \gset
\if :_topo_ok
\else
  \echo '*** REFUSED: unexpected topology -- bootstrap_role must be the sole superuser + DB owner, app_schema must'
  \echo '    be a non-system schema, and app_role must not be privileged. Nothing changed. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- Ordering guard (adversarial-review C4): refuse if erasure provisioning has ALREADY run (the governed
-- procedure exists). Re-running the separation then re-GRANTs the app role broad DELETE on ALL TABLES (incl.
-- the evidence tables) and EXECUTE on ALL FUNCTIONS (incl. the governed proc) that provisioning revoked. Roll
-- provisioning back (production_rollback_erasure.sql) before any re-run.
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE p.proname='bi_governed_erase_owner' AND n.nspname='public') THEN 'true' ELSE 'false' END AS _provisioned \gset
\if :_provisioned
  \echo '*** REFUSED: erasure provisioning has already run (bi_governed_erase_owner present). Re-running the'
  \echo '    separation would re-open broad grants provisioning revoked. Roll provisioning back first. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- pgcrypto (used by the erasure procedure later; harmless here, idempotent).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---- 1) Roles (idempotent; all least-privilege) -----------------------------------------------------------
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_schema_owner') THEN
    CREATE ROLE guvfx_schema_owner NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_migrate') THEN
    CREATE ROLE guvfx_migrate LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS; END IF;
END
$roles$;
-- app_role (guvfx_app): created if absent, attributes forced least-privilege either way. Its NAME is passed
-- into the DO block via a GUC (psql does not interpolate :vars inside dollar-quoted blocks).
SELECT set_config('x.app_role', :'app_role', false);
DO $approle$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('x.app_role')) THEN
    EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS',
                   current_setting('x.app_role'));
  ELSE
    EXECUTE format('ALTER ROLE %I NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS',
                   current_setting('x.app_role'));
  END IF;
END
$approle$;

-- LOGIN credentials from client-computed SCRAM verifiers (never cleartext).
ALTER ROLE guvfx_migrate LOGIN PASSWORD :'migrate_scram';
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_scram') AS _setpw \gset
:_setpw ;

-- guvfx_migrate is a member of the schema owner AND its sessions ACT AS the schema owner, so governed
-- migrations create objects owned by guvfx_schema_owner (not by guvfx_migrate).
GRANT guvfx_schema_owner TO guvfx_migrate;
ALTER ROLE guvfx_migrate SET role = 'guvfx_schema_owner';

-- The app role must NOT be a member of any privileged role.
REVOKE guvfx_schema_owner, guvfx_migrate FROM :"app_role";

-- ---- 2) Transfer ONLY application-object ownership in the approved schema to guvfx_schema_owner ------------
-- Explicit enumeration. EXCLUDES extension-owned objects (pg_depend deptype 'e') and anything outside the
-- approved application schema. NEVER an unrestricted REASSIGN OWNED. Refuses (via the per-object ALTER) only
-- app objects currently owned by the bootstrap role.
SELECT set_config('x.schema', :'app_schema', false);
SELECT set_config('x.boot', :'bootstrap_role', false);

-- 2a) the schema itself -> schema owner
SELECT format('ALTER SCHEMA %I OWNER TO guvfx_schema_owner', :'app_schema') AS _altsch \gset
:_altsch ;

-- 2b) tables, sequences, views, matviews, foreign tables in the approved schema, owned by the bootstrap role,
--     NOT extension-owned.
DO $own_rel$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT c.oid, c.relname, c.relkind
    FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
    WHERE s.nspname = current_setting('x.schema')
      AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_setting('x.boot'))
      AND c.relkind IN ('r','S','v','m','f','p')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
      -- Skip OWNED/identity sequences (linked to a table column): their owner follows the table's ALTER OWNER,
      -- and an explicit ALTER SEQUENCE OWNER on them errors ("linked to table"). Standalone sequences still move.
      AND NOT (c.relkind='S' AND EXISTS (SELECT 1 FROM pg_depend d2 WHERE d2.classid='pg_class'::regclass
                      AND d2.objid=c.oid AND d2.refclassid='pg_class'::regclass AND d2.deptype IN ('a','i')))
  LOOP
    EXECUTE format('ALTER %s %I.%I OWNER TO guvfx_schema_owner',
                   CASE WHEN r.relkind='S' THEN 'SEQUENCE'
                        WHEN r.relkind IN ('v') THEN 'VIEW'
                        WHEN r.relkind='m' THEN 'MATERIALIZED VIEW'
                        WHEN r.relkind='f' THEN 'FOREIGN TABLE'
                        ELSE 'TABLE' END,
                   current_setting('x.schema'), r.relname);
    n := n + 1;
  END LOOP;
  RAISE NOTICE 'separate_runtime_role: reassigned % relation(s) in schema % to guvfx_schema_owner', n, current_setting('x.schema');
END
$own_rel$;

-- 2c) functions/procedures in the approved schema, owned by the bootstrap role, NOT extension-owned.
DO $own_fn$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS args, p.prokind
    FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace
    WHERE s.nspname = current_setting('x.schema')
      AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = current_setting('x.boot'))
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')
  LOOP
    EXECUTE format('ALTER %s %I.%I(%s) OWNER TO guvfx_schema_owner',
                   CASE WHEN r.prokind='p' THEN 'PROCEDURE' WHEN r.prokind='a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
                   current_setting('x.schema'), r.proname, r.args);
    n := n + 1;
  END LOOP;
  RAISE NOTICE 'separate_runtime_role: reassigned % function(s)/procedure(s) in schema % to guvfx_schema_owner', n, current_setting('x.schema');
END
$own_fn$;

-- ---- 3) Runtime privileges for the app role (it OWNS NOTHING; operates via these grants) -------------------
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', :'app_schema', :'app_role') AS _g1 \gset
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I', :'app_schema', :'app_role') AS _g2 \gset
SELECT format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I TO %I', :'app_schema', :'app_role') AS _g3 \gset
SELECT format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I', :'app_schema', :'app_role') AS _g4 \gset
:_g1 ;
:_g2 ;
:_g3 ;
:_g4 ;
-- Future objects created by governed migrations (which now run AS guvfx_schema_owner) must be usable by the app.
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', :'app_schema', :'app_role') AS _d1 \gset
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I', :'app_schema', :'app_role') AS _d2 \gset
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I GRANT EXECUTE ON FUNCTIONS TO %I', :'app_schema', :'app_role') AS _d3 \gset
:_d1 ;
:_d2 ;
:_d3 ;

-- The bootstrap role is NOT demoted here (it remains the controlled superuser/recovery identity). Application
-- processes stop using it via the separate configuration cutover (re-point DB_USER to the app role).

COMMIT;
\echo 'separate_runtime_role.sql: COMMITTED. App objects owned by guvfx_schema_owner; app role holds runtime grants only; bootstrap role left intact as the controlled superuser.'
