-- ADR-0054 / F-8 -- PRODUCTION runtime-role separation (bootstrap-SAFE, manifest-driven, fail-closed).
-- ============================================================================================================
-- SUPERUSER-ONLY. SEPARATELY SPONSOR-AUTHORISED. Driven by ops/erasure/cutover.sh, which (a) builds a TEMP table
-- `_cutover_manifest` from the reviewed, version-controlled cutover manifest and prepends it to this script, and
-- (b) supplies the SCRAM verifiers through the ENVIRONMENT (never argv/logs) read here via \getenv.
--
-- Target separation (PD disposition): bootstrap `guvfx` stays SUPERUSER (admin/recovery only; NEVER demoted);
-- new least-privilege `guvfx_app` (LOGIN) OWNS NOTHING + has MINIMAL runtime grants; `guvfx_schema_owner`
-- (NOLOGIN) owns the application objects + schema; `guvfx_migrate` (LOGIN) acts AS the schema owner for governed
-- migrations. No blanket REASSIGN OWNED; only MANIFEST-approved objects in the approved schema are transferred;
-- system/extension objects are never touched.
--
-- Non-secret psql vars (fail-closed if missing): target_db, allow_destructive_provisioning, bootstrap_role,
--   app_role, app_schema. SECRET verifiers come from env: GUVFX_APP_SCRAM, GUVFX_MIGRATE_SCRAM (via \getenv).
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo '*** REFUSED: pass -v target_db=<current database name>.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?allow_destructive_provisioning}
\else
  \echo '*** REFUSED: pass -v allow_destructive_provisioning=1.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?bootstrap_role}
\else
  \echo '*** REFUSED: pass -v bootstrap_role=<current bootstrap/admin superuser, e.g. guvfx>.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role=<new least-privilege runtime role, e.g. guvfx_app>.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?app_schema}
\else
  \echo '*** REFUSED: pass -v app_schema=<approved application schema, e.g. public>.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- F-4: SCRAM verifiers from the ENVIRONMENT (never on argv). Default-empty so an unset env fails closed.
\set app_scram ''
\set migrate_scram ''
\getenv app_scram GUVFX_APP_SCRAM
\getenv migrate_scram GUVFX_MIGRATE_SCRAM
SELECT CASE WHEN length(:'app_scram') > 20 AND :'app_scram' LIKE 'SCRAM-SHA-256$%'
            AND length(:'migrate_scram') > 20 AND :'migrate_scram' LIKE 'SCRAM-SHA-256$%'
       THEN 'true' ELSE 'false' END AS _creds_ok \gset
\if :_creds_ok
\else
  \echo '*** REFUSED: GUVFX_APP_SCRAM / GUVFX_MIGRATE_SCRAM env vars must each hold a SCRAM-SHA-256 verifier.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

BEGIN;

SELECT CASE WHEN current_database() = :'target_db' THEN 'true' ELSE 'false' END AS _dbok \gset
\if :_dbok
\else
  \echo '*** REFUSED: current_database() does not match -v target_db. Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- Topology (fail-closed): bootstrap_role = sole superuser + DB owner; app_schema real/non-system; app_role not privileged.
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
  \echo '*** REFUSED: unexpected topology (bootstrap must be sole superuser + DB owner; app_schema non-system;'
  \echo '    app_role not privileged). Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- Ordering guard (C4): refuse if erasure provisioning already ran (governed proc present).
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE p.proname='bi_governed_erase_owner' AND n.nspname='public') THEN 'true' ELSE 'false' END AS _provisioned \gset
\if :_provisioned
  \echo '*** REFUSED: erasure provisioning already ran (bi_governed_erase_owner present). Roll it back first.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

SELECT set_config('x.schema', :'app_schema', false);
SELECT set_config('x.boot', :'bootstrap_role', false);
SELECT set_config('x.app_role', :'app_role', false);

-- F-3: the reviewed manifest MUST be present (built by cutover.sh) and non-empty.
SELECT CASE WHEN to_regclass('pg_temp._cutover_manifest') IS NOT NULL THEN 'true' ELSE 'false' END AS _man \gset
\if :_man
\else
  \echo '*** REFUSED: _cutover_manifest TEMP table absent. Run via ops/erasure/cutover.sh (it builds + reviews it).'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- F-3: compare the LIVE bootstrap-owned app-schema inventory against the approved manifest; refuse on ANY
-- missing / unexpected / owner-mismatch / unclassified object BEFORE any mutation.
DO $manifest$
DECLARE missing text; unexpected text; badclass text;
BEGIN
  IF (SELECT count(*) FROM _cutover_manifest) = 0 THEN RAISE EXCEPTION 'cutover manifest is empty'; END IF;
  IF EXISTS (SELECT 1 FROM _cutover_manifest WHERE classification NOT IN ('application','protected')) THEN
    RAISE EXCEPTION 'manifest contains an unclassified/forbidden classification (only application|protected allowed)';
  END IF;
  CREATE TEMP TABLE _live_inv ON COMMIT DROP AS
    SELECT (CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview'
                           WHEN 'f' THEN 'foreign_table' ELSE 'table' END) AS obj_kind,
           n.nspname AS obj_schema, c.relname AS obj_identity, pg_get_userbyid(c.relowner) AS owner
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname = current_setting('x.schema') AND c.relkind IN ('r','p','S','v','m','f')
       AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
    UNION ALL
    SELECT (CASE p.prokind WHEN 'a' THEN 'aggregate' WHEN 'p' THEN 'procedure' ELSE 'function' END),
           n.nspname, p.proname||'('||pg_get_function_identity_arguments(p.oid)||')', pg_get_userbyid(p.proowner)
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
     WHERE n.nspname = current_setting('x.schema')
       AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e');
  -- live objects not in the manifest (unexpected) -- e.g. a manually-added function/table
  SELECT string_agg(obj_kind||' '||obj_schema||'.'||obj_identity, '; ') INTO unexpected FROM (
    SELECT obj_kind,obj_schema,obj_identity FROM _live_inv
    EXCEPT SELECT obj_kind,obj_schema,obj_identity FROM _cutover_manifest) u;
  IF unexpected IS NOT NULL THEN RAISE EXCEPTION 'live object(s) NOT in the approved manifest (refusing): %', unexpected; END IF;
  -- manifest objects not present live (missing)
  SELECT string_agg(obj_kind||' '||obj_schema||'.'||obj_identity, '; ') INTO missing FROM (
    SELECT obj_kind,obj_schema,obj_identity FROM _cutover_manifest
    EXCEPT SELECT obj_kind,obj_schema,obj_identity FROM _live_inv) m;
  IF missing IS NOT NULL THEN RAISE EXCEPTION 'manifest object(s) missing live (refusing): %', missing; END IF;
  -- owner mismatch: every live object must currently be owned by the bootstrap role (the manifest's expected owner)
  SELECT string_agg(l.obj_identity||' owned by '||l.owner, '; ') INTO badclass FROM _live_inv l
    JOIN _cutover_manifest m USING (obj_kind,obj_schema,obj_identity)
   WHERE l.owner <> current_setting('x.boot') OR l.owner <> m.expected_owner;
  IF badclass IS NOT NULL THEN RAISE EXCEPTION 'owner mismatch vs manifest/bootstrap (refusing): %', badclass; END IF;
  RAISE NOTICE 'manifest OK: % object(s) match live exactly', (SELECT count(*) FROM _cutover_manifest);
END
$manifest$;

-- F-1: membership-closure guard (pre-mutation). Permitted matrix: app_role {}, schema_owner {} + NOLOGIN,
-- migrate {guvfx_schema_owner}. Refuse (do NOT silently revoke) on any unexpected direct/transitive membership.
DO $closure_pre$
DECLARE bad text;
BEGIN
  SELECT string_agg(rolname, ',') INTO bad FROM pg_roles WHERE oid IN (
    WITH RECURSIVE cl(roleid) AS (
      SELECT oid FROM pg_roles WHERE rolname = current_setting('x.app_role')
      UNION SELECT m.roleid FROM pg_auth_members m JOIN cl ON m.member = cl.roleid)
    SELECT roleid FROM cl) AND rolname <> current_setting('x.app_role');
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'app_role has unexpected role membership(s) [refuse, review manually]: %', bad; END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_schema_owner') THEN
    IF (SELECT rolcanlogin FROM pg_roles WHERE rolname='guvfx_schema_owner') THEN RAISE EXCEPTION 'guvfx_schema_owner must be NOLOGIN'; END IF;
    SELECT string_agg(rolname, ',') INTO bad FROM pg_roles WHERE oid IN (
      WITH RECURSIVE cl(roleid) AS (SELECT oid FROM pg_roles WHERE rolname='guvfx_schema_owner'
        UNION SELECT m.roleid FROM pg_auth_members m JOIN cl ON m.member=cl.roleid)
      SELECT roleid FROM cl) AND rolname <> 'guvfx_schema_owner';
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'guvfx_schema_owner has unexpected membership(s): %', bad; END IF;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_migrate') THEN
    SELECT string_agg(rolname, ',') INTO bad FROM pg_roles WHERE oid IN (
      WITH RECURSIVE cl(roleid) AS (SELECT oid FROM pg_roles WHERE rolname='guvfx_migrate'
        UNION SELECT m.roleid FROM pg_auth_members m JOIN cl ON m.member=cl.roleid)
      SELECT roleid FROM cl) AND rolname NOT IN ('guvfx_migrate','guvfx_schema_owner');
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'guvfx_migrate has unexpected membership(s) [only guvfx_schema_owner allowed]: %', bad; END IF;
  END IF;
END
$closure_pre$;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---- Roles (idempotent, least-privilege) ------------------------------------------------------------------
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_schema_owner') THEN
    CREATE ROLE guvfx_schema_owner NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_migrate') THEN
    CREATE ROLE guvfx_migrate LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_setting('x.app_role')) THEN
    EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS', current_setting('x.app_role'));
  ELSE
    EXECUTE format('ALTER ROLE %I NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS', current_setting('x.app_role'));
  END IF;
END
$roles$;

-- Credentials from client-computed SCRAM verifiers (from env; never cleartext, never argv).
ALTER ROLE guvfx_migrate LOGIN PASSWORD :'migrate_scram';
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_scram') AS _setpw \gset
:_setpw ;

GRANT guvfx_schema_owner TO guvfx_migrate;
ALTER ROLE guvfx_migrate SET role = 'guvfx_schema_owner';
-- NOTE (F-1): the app role's memberships are NOT touched here. The membership-closure guard above has already
-- REFUSED if the app role carried ANY membership; we never silently revoke an unexpected membership.

-- ---- Ownership transfer: ONLY manifest-approved objects, to guvfx_schema_owner --------------------------
-- Schema first.
SELECT format('ALTER SCHEMA %I OWNER TO guvfx_schema_owner', :'app_schema') AS _altsch \gset
:_altsch ;
-- Relations (skip owned/identity sequences; they follow their table). Driven by the manifest.
DO $own_rel$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT c.oid, c.relname, c.relkind
    FROM pg_class c JOIN pg_namespace s ON s.oid=c.relnamespace
    JOIN _cutover_manifest m
      ON m.obj_schema = s.nspname AND m.obj_identity = c.relname
     AND m.obj_kind = (CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview'
                                      WHEN 'f' THEN 'foreign_table' ELSE 'table' END)
    WHERE s.nspname = current_setting('x.schema') AND c.relkind IN ('r','p','S','v','m','f')
      AND NOT (c.relkind='S' AND EXISTS (SELECT 1 FROM pg_depend d2 WHERE d2.classid='pg_class'::regclass
                      AND d2.objid=c.oid AND d2.refclassid='pg_class'::regclass AND d2.deptype IN ('a','i')))
  LOOP
    EXECUTE format('ALTER %s %I.%I OWNER TO guvfx_schema_owner',
      CASE WHEN r.relkind='S' THEN 'SEQUENCE' WHEN r.relkind='v' THEN 'VIEW' WHEN r.relkind='m' THEN 'MATERIALIZED VIEW'
           WHEN r.relkind='f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
      current_setting('x.schema'), r.relname);
    n := n + 1;
  END LOOP;
  RAISE NOTICE 'transferred % manifest relation(s) to guvfx_schema_owner', n;
END
$own_rel$;
-- Routines (functions/procedures/aggregates) driven by the manifest.
DO $own_fn$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS args, p.prokind
    FROM pg_proc p JOIN pg_namespace s ON s.oid=p.pronamespace
    JOIN _cutover_manifest m
      ON m.obj_schema = s.nspname AND m.obj_identity = p.proname||'('||pg_get_function_identity_arguments(p.oid)||')'
     AND m.obj_kind = (CASE p.prokind WHEN 'a' THEN 'aggregate' WHEN 'p' THEN 'procedure' ELSE 'function' END)
    WHERE s.nspname = current_setting('x.schema')
  LOOP
    EXECUTE format('ALTER %s %I.%I(%s) OWNER TO guvfx_schema_owner',
      CASE WHEN r.prokind='p' THEN 'PROCEDURE' WHEN r.prokind='a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
      current_setting('x.schema'), r.proname, r.args);
    n := n + 1;
  END LOOP;
  RAISE NOTICE 'transferred % manifest routine(s) to guvfx_schema_owner', n;
END
$own_fn$;

-- ---- F-2: MINIMAL runtime grants for the app role (owns nothing). ---------------------------------------
--   application tables: SELECT/INSERT/UPDATE/DELETE ; protected tables: SELECT/INSERT/UPDATE (NO DELETE/TRUNCATE);
--   sequences: USAGE/SELECT/UPDATE ; routines: NONE (trigger functions need no invoker EXECUTE; the app calls no
--   custom DB routine). Default privileges grant NO DELETE and NO EXECUTE, so FUTURE objects are DELETE-safe and
--   never auto-executable.
GRANT USAGE ON SCHEMA :"app_schema" TO :"app_role";
DO $grants$
DECLARE r record;
BEGIN
  FOR r IN SELECT obj_identity, classification FROM _cutover_manifest WHERE obj_kind='table' LOOP
    IF r.classification='protected' THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I.%I TO %I', current_setting('x.schema'), r.obj_identity, current_setting('x.app_role'));
    ELSE
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I.%I TO %I', current_setting('x.schema'), r.obj_identity, current_setting('x.app_role'));
    END IF;
  END LOOP;
  FOR r IN SELECT obj_identity FROM _cutover_manifest WHERE obj_kind='sequence' LOOP
    EXECUTE format('GRANT USAGE, SELECT, UPDATE ON SEQUENCE %I.%I TO %I', current_setting('x.schema'), r.obj_identity, current_setting('x.app_role'));
  END LOOP;
END
$grants$;
-- Default privileges for FUTURE migration-created objects (schema_owner is the creator): NO DELETE, NO EXECUTE.
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I GRANT SELECT, INSERT, UPDATE ON TABLES TO %I', :'app_schema', :'app_role') AS _d1 \gset
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I', :'app_schema', :'app_role') AS _d2 \gset
:_d1 ;
:_d2 ;

-- ---- Pre-COMMIT re-checks (fail-closed) -----------------------------------------------------------------
DO $post$
DECLARE bad text; v_app text := current_setting('x.app_role'); v_sch text := current_setting('x.schema');
BEGIN
  -- app role owns nothing in the approved schema
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname=v_sch AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname=v_app)
               AND c.relkind IN ('r','p','S','v','m','f')) THEN RAISE EXCEPTION 'post-check: app role still owns relation(s)'; END IF;
  IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
             WHERE n.nspname=v_sch AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=v_app)) THEN RAISE EXCEPTION 'post-check: app role still owns routine(s)'; END IF;
  -- app role must NOT have DELETE/TRUNCATE on protected evidence tables
  FOR bad IN SELECT obj_identity FROM _cutover_manifest WHERE obj_kind='table' AND classification='protected' LOOP
    IF has_table_privilege(v_app, format('%I.%I', v_sch, bad), 'DELETE')
       OR has_table_privilege(v_app, format('%I.%I', v_sch, bad), 'TRUNCATE') THEN
      RAISE EXCEPTION 'post-check: app role has DELETE/TRUNCATE on protected table %', bad; END IF;
  END LOOP;
  -- closure recheck: app_role no memberships; migrate only schema_owner
  SELECT string_agg(rolname, ',') INTO bad FROM pg_roles WHERE oid IN (
    WITH RECURSIVE cl(roleid) AS (SELECT oid FROM pg_roles WHERE rolname=v_app
      UNION SELECT m.roleid FROM pg_auth_members m JOIN cl ON m.member=cl.roleid) SELECT roleid FROM cl)
    AND rolname <> v_app;
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'post-check: app role gained membership(s): %', bad; END IF;
  -- app role carries no elevated attribute
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=v_app AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls)) THEN
    RAISE EXCEPTION 'post-check: app role carries an elevated attribute'; END IF;
  RAISE NOTICE 'post-checks OK: app role owns nothing, no protected DELETE/TRUNCATE, no membership, least-privilege';
END
$post$;

-- F-3: before/after digest (manifest digest + resulting owner map), emitted for the evidence record.
SELECT 'MANIFEST_DIGEST=' || md5(string_agg(obj_kind||'|'||obj_schema||'|'||obj_identity||'|'||classification, E'\n' ORDER BY obj_kind,obj_identity)) AS mdig
  FROM _cutover_manifest \gset
\echo :mdig
SELECT 'POST_OWNER_DIGEST=' || md5(string_agg(obj_kind||'|'||obj_identity||'|'||owner, E'\n' ORDER BY obj_kind,obj_identity)) AS odig FROM (
  SELECT (CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' WHEN 'f' THEN 'foreign_table' ELSE 'table' END) obj_kind,
         c.relname obj_identity, pg_get_userbyid(c.relowner) owner
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname=current_setting('x.schema') AND c.relkind IN ('r','p','S','v','m','f')
     AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
) s \gset
\echo :odig

COMMIT;
\echo 'separate_runtime_role.sql: COMMITTED. Manifest-approved app objects owned by guvfx_schema_owner; app role holds minimal grants + owns nothing; bootstrap superuser intact.'
