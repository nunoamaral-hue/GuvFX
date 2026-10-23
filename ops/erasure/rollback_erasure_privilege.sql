-- ADR-0054 -- Reverse the controlled-erasure privilege provisioning (REHEARSAL rollback).
-- Restores the original arrangement: evidence tables + trigger functions + sequences owned by the application
-- role again, the procedure + helpers dropped, and the privileged roles removed. SUPERUSER-ONLY. Transactional.
--   psql "$DSN" -v ON_ERROR_STOP=1 -v target_db=guvfx_rehearsal -v app_role=guvfx_app \
--        -f ops/erasure/rollback_erasure_privilege.sql
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo '*** REFUSED: pass -v target_db=<current database name>'
  \quit
\endif
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role=<application runtime role>'
  \quit
\endif

BEGIN;

-- DB-name guard (interpolated OUTSIDE dollar-quoting; psql does not substitute :vars inside $tag$...$tag$).
SELECT CASE WHEN current_database() = :'target_db' THEN 'true' ELSE 'false' END AS _dbok \gset
\if :_dbok
\else
  \echo '*** REFUSED: current_database() does not match -v target_db. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- F-8 SAFETY GUARD (adopted with the runtime-role separation). This REHEARSAL teardown predates F-8 and assumes
-- the app role owns the evidence objects and guvfx_schema_owner owns ONLY the handful of erasure objects. In the
-- F-8 runtime-role-separation model guvfx_schema_owner owns the ENTIRE application schema, so this script would
-- (a) hand schema + evidence ownership to the runtime app role and (b) `DROP OWNED BY guvfx_schema_owner`,
-- DESTROYING every application table. Refuse fail-closed if that model is detected.
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
      AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'guvfx_schema_owner')
      AND c.relname NOT IN ('broker_intelligence_evidenceblob','broker_intelligence_brokerevent',
                            'broker_intelligence_erasureexecutiongrant')
  ) THEN 'true' ELSE 'false' END AS _f8model \gset
\if :_f8model
  \echo '*** REFUSED: guvfx_schema_owner owns general application tables -- this is the F-8 runtime-role'
  \echo '    separation model. This DISPOSABLE-REHEARSAL teardown would hand schema/evidence ownership to the'
  \echo '    app role and DROP OWNED BY guvfx_schema_owner (destroying the application schema). For the F-8'
  \echo '    model use production_rollback_erasure.sql, THEN ops/erasure/separate_runtime_role_rollback.sql.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'rollback refused (fail-closed): F-8 model detected; see message above'; END $refuse$;
\endif

-- 1) Drop the governed procedure + helpers (owned by guvfx_erasure). Both signatures, for any prior version.
DROP FUNCTION IF EXISTS public.bi_governed_erase_owner(bigint, text);
DROP FUNCTION IF EXISTS public.bi_governed_erase_owner(bigint, bigint, text);
DROP FUNCTION IF EXISTS public.bi_erasure_plan_facts(bigint);
DROP FUNCTION IF EXISTS public.bi_erasure_plan_matches(jsonb, jsonb);

-- 2) Restore ownership of the evidence objects to the application role.
ALTER TABLE broker_intelligence_evidenceblob OWNER TO :"app_role";
ALTER TABLE broker_intelligence_brokerevent  OWNER TO :"app_role";
ALTER FUNCTION bi_evidenceblob_block_delete() OWNER TO :"app_role";
ALTER FUNCTION bi_brokerevent_block_delete()  OWNER TO :"app_role";
ALTER SEQUENCE broker_intelligence_evidenceblob_id_seq OWNER TO :"app_role";
ALTER SEQUENCE broker_intelligence_brokerevent_id_seq  OWNER TO :"app_role";

-- 3) Restore the app role's full table rights (owner implicitly has them, but make the grant explicit too).
GRANT ALL ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent TO :"app_role";

-- 3b) Restore ownership of the execution-grant table (a migrated Django table) to the app role, so the DROP
-- OWNED below does NOT drop it. The grant ROWS are left as-is (harmless metadata; the procedure is gone).
ALTER TABLE broker_intelligence_erasureexecutiongrant OWNER TO :"app_role";
ALTER SEQUENCE broker_intelligence_erasureexecutiongrant_id_seq OWNER TO :"app_role";

-- 3c) Restore schema ownership + CREATE to the application role (reverses the HIGH-finding hardening).
ALTER SCHEMA public OWNER TO :"app_role";
GRANT CREATE ON SCHEMA public TO :"app_role";

-- 4) Remove privileged-role grants, then drop the roles. DROP OWNED cleans up any remaining grants/defaults.
-- Per-operator executor LOGINs (members of guvfx_erasure_exec, created by the grant process, e.g. guvfx_erx_*)
-- are dropped first so the group drop has no dependent members.
DO $drop$
DECLARE r text;
BEGIN
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE 'guvfx_erx_%' LOOP
    EXECUTE format('DROP OWNED BY %I CASCADE', r);
    EXECUTE format('DROP ROLE %I', r);
  END LOOP;
  FOREACH r IN ARRAY ARRAY['guvfx_erasure','guvfx_erasure_exec','guvfx_schema_owner','guvfx_migrate','guvfx_readonly'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('DROP OWNED BY %I CASCADE', r);
      EXECUTE format('DROP ROLE %I', r);
    END IF;
  END LOOP;
END
$drop$;

COMMIT;
\echo 'rollback_erasure_privilege.sql: COMMITTED. The original privilege arrangement is restored.'
