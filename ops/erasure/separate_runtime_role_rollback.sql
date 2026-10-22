-- ADR-0054 / F-8 -- ROLLBACK of ops/erasure/separate_runtime_role.sql (bootstrap-SAFE).
-- ============================================================================================================
-- SUPERUSER-ONLY. SEPARATELY SPONSOR-AUTHORISED. Reverses the runtime-role separation by EXPLICIT enumeration.
-- It NEVER makes the app role an owner or superuser, NEVER uses a blanket REASSIGN OWNED, and NEVER demotes the
-- bootstrap role (which was never demoted) -- so the cluster always retains its controlled superuser.
--
-- Use ONLY to revert a separation that has NOT been followed by erasure provisioning (if the governed procedure
-- exists, run ops/erasure/production_rollback_erasure.sql FIRST, then this). The application-configuration
-- rollback (re-point DB_USER back to the bootstrap role) is a SEPARATE step (see the production packet).
--
-- Required: -v target_db -v allow_destructive_provisioning=1 -v bootstrap_role -v app_role -v app_schema
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo '*** REFUSED: pass -v target_db.'
  \quit
\endif
\if :{?allow_destructive_provisioning}
\else
  \echo '*** REFUSED: pass -v allow_destructive_provisioning=1.'
  \quit
\endif
\if :{?bootstrap_role}
\else
  \echo '*** REFUSED: pass -v bootstrap_role.'
  \quit
\endif
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role.'
  \quit
\endif
\if :{?app_schema}
\else
  \echo '*** REFUSED: pass -v app_schema.'
  \quit
\endif

BEGIN;
SELECT CASE WHEN current_database() = :'target_db' THEN 'true' ELSE 'false' END AS _dbok \gset
\if :_dbok
\else
  \echo '*** REFUSED: current_database() does not match -v target_db. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- Ordering guard: refuse while the erasure capability is still provisioned (reverse provisioning first).
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE p.proname='bi_governed_erase_owner' AND n.nspname='public') THEN 'true' ELSE 'false' END AS _prov \gset
\if :_prov
  \echo '*** REFUSED: erasure capability still provisioned (bi_governed_erase_owner present). Run'
  \echo '    production_rollback_erasure.sql FIRST, then re-run this separation rollback. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- Bootstrap role must still be a superuser (sanity: we never demoted it; a missing superuser is a red flag).
SELECT CASE WHEN (SELECT rolsuper FROM pg_roles WHERE rolname = :'bootstrap_role') THEN 'true' ELSE 'false' END AS _boot \gset
\if :_boot
\else
  \echo '*** REFUSED: bootstrap_role is not a superuser -- unexpected; manual review required. Rolling back.'
  ROLLBACK;
  \quit
\endif

SELECT set_config('x.schema', :'app_schema', false);
SELECT set_config('x.boot', :'bootstrap_role', false);
SELECT set_config('x.app_role', :'app_role', false);

-- 1) Revoke the runtime grants + default privileges granted to the app role.
SELECT format('REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I FROM %I', :'app_schema', :'app_role') AS _r1 \gset
SELECT format('REVOKE USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I FROM %I', :'app_schema', :'app_role') AS _r2 \gset
SELECT format('REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA %I FROM %I', :'app_schema', :'app_role') AS _r3 \gset
SELECT format('REVOKE USAGE ON SCHEMA %I FROM %I', :'app_schema', :'app_role') AS _r4 \gset
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM %I', :'app_schema', :'app_role') AS _r5 \gset
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM %I', :'app_schema', :'app_role') AS _r6 \gset
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I REVOKE EXECUTE ON FUNCTIONS FROM %I', :'app_schema', :'app_role') AS _r7 \gset
:_r1 ;
:_r2 ;
:_r3 ;
:_r5 ;
:_r6 ;
:_r7 ;
:_r4 ;

-- 2) Reverse ownership of application objects (explicit enumeration) from guvfx_schema_owner back to the
--    bootstrap role; restore the schema to its original owner (pg_database_owner).
DO $rel$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.oid, c.relname, c.relkind FROM pg_class c JOIN pg_namespace s ON s.oid=c.relnamespace
    WHERE s.nspname=current_setting('x.schema')
      AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname='guvfx_schema_owner')
      AND c.relkind IN ('r','S','v','m','f','p')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
      AND NOT (c.relkind='S' AND EXISTS (SELECT 1 FROM pg_depend d2 WHERE d2.classid='pg_class'::regclass
                      AND d2.objid=c.oid AND d2.refclassid='pg_class'::regclass AND d2.deptype IN ('a','i')))
  LOOP
    EXECUTE format('ALTER %s %I.%I OWNER TO %I',
      CASE WHEN r.relkind='S' THEN 'SEQUENCE' WHEN r.relkind='v' THEN 'VIEW'
           WHEN r.relkind='m' THEN 'MATERIALIZED VIEW' WHEN r.relkind='f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
      current_setting('x.schema'), r.relname, current_setting('x.boot'));
  END LOOP;
END
$rel$;
DO $fn$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS args, p.prokind
    FROM pg_proc p JOIN pg_namespace s ON s.oid=p.pronamespace
    WHERE s.nspname=current_setting('x.schema')
      AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname='guvfx_schema_owner')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')
  LOOP
    EXECUTE format('ALTER %s %I.%I(%s) OWNER TO %I',
      CASE WHEN r.prokind='p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
      current_setting('x.schema'), r.proname, r.args, current_setting('x.boot'));
  END LOOP;
END
$fn$;
SELECT format('ALTER SCHEMA %I OWNER TO pg_database_owner', :'app_schema') AS _sch \gset
:_sch ;

-- 3) Reset guvfx_migrate's acting-role default (leave the roles in place for the operator to drop if desired).
ALTER ROLE guvfx_migrate RESET role;

-- The bootstrap role was never demoted; the cluster still has its controlled superuser. The app role owns
-- nothing (we only revoked grants); it is never made an owner or superuser by this rollback.
COMMIT;
\echo 'separate_runtime_role_rollback.sql: COMMITTED. Runtime grants revoked; application-object ownership restored to the bootstrap role; bootstrap superuser intact.'
