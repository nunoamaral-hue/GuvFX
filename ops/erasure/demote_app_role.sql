-- ADR-0054 -- PRODUCTION application-role DEMOTION to least privilege (mandatory precondition).
-- ============================================================================================================
-- SUPERUSER-ONLY. SEPARATELY SPONSOR-AUTHORISED. Run BEFORE ops/erasure/provision_erasure_privilege.sql.
-- This is the separate, explicit demotion the provisioning script deliberately refuses to perform itself.
--
-- It moves ALL object ownership off the (currently privileged) application role to a dedicated schema owner,
-- demotes the application role to a least-privilege cluster posture, and re-grants ONLY the runtime DML the
-- backend needs (plus default privileges so objects a later migration creates remain usable by the app role).
-- After this, `erasure.privileged_backend_readiness()` no longer rejects the app role on posture grounds, and
-- schema migrations run under guvfx_migrate (a member of the schema owner).
--
-- Guards (fail before any change):
--   -v target_db=<exact current database name>   (verified against current_database())
--   -v allow_destructive_provisioning=1           (explicit acknowledgement)
--   -v app_role=<application runtime role>         (the role to demote, e.g. guvfx)
--   -v migrate_pw=<disposable/managed password for guvfx_migrate>
-- Reverse with ops/erasure/demote_app_role_rollback.sql (restores the PRIOR posture, incl. its ownership).
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
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role=<application runtime role to demote>.'
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

-- F-8 topology guard: this IN-PLACE demotion is UNSAFE for a bootstrap / sole-superuser application role --
-- demoting it would leave the cluster with no superuser, and a blanket REASSIGN OWNED would hit pinned system
-- objects. If app_role is the bootstrap role (OID 10) or the only superuser, REFUSE and direct the operator to
-- the bootstrap-safe ops/erasure/separate_runtime_role.sql (new least-privilege runtime role). This script
-- remains valid ONLY for a NON-bootstrap, non-sole-superuser app role (e.g. a disposable test role).
SELECT CASE WHEN
      COALESCE((SELECT oid = 10 FROM pg_roles WHERE rolname = :'app_role'), false)
   OR COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname = :'app_role'), false)
        AND (SELECT count(*) FROM pg_roles WHERE rolsuper) = 1
   THEN 'true' ELSE 'false' END AS _app_is_bootstrap \gset
\if :_app_is_bootstrap
  \echo '*** REFUSED: app_role is the bootstrap role (OID 10) and/or the sole superuser. In-place demotion would'
  \echo '    leave the cluster without a superuser and target pinned system objects. Use'
  \echo '    ops/erasure/separate_runtime_role.sql (new least-privilege runtime role) instead. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- Ordering guard (adversarial-review C4): refuse if erasure provisioning has ALREADY run (the governed
-- procedure exists). Re-running the demotion after provisioning would re-GRANT the app role the broad
-- DELETE/EXECUTE that provisioning revoked. Roll provisioning back (production_rollback_erasure.sql) first.
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE p.proname='bi_governed_erase_owner' AND n.nspname='public') THEN 'true' ELSE 'false' END AS _provisioned \gset
\if :_provisioned
  \echo '*** REFUSED: erasure provisioning has already run (bi_governed_erase_owner present). Re-running demotion'
  \echo '    would re-open broad grants provisioning revoked. Roll provisioning back first. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- 1) Roles (idempotent). guvfx_schema_owner owns objects; guvfx_migrate (its member) runs migrations.
DO $r$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_schema_owner') THEN
    CREATE ROLE guvfx_schema_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_migrate') THEN
    CREATE ROLE guvfx_migrate LOGIN; END IF;
END
$r$;
\if :{?migrate_pw}
  ALTER ROLE guvfx_migrate LOGIN PASSWORD :'migrate_pw';
\endif
GRANT guvfx_schema_owner TO guvfx_migrate;

-- 2) Move ALL object ownership off the application role to the schema owner (the app role then owns nothing,
--    including schema public -- closing the owner-level DROP/ALTER vectors). REASSIGN OWNED is scoped to the
--    current database + shared objects the role owns.
REASSIGN OWNED BY :"app_role" TO guvfx_schema_owner;

-- 3) Demote the application role at the CLUSTER level (the posture the readiness probe now requires).
ALTER ROLE :"app_role" NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;

-- 4) Re-grant ONLY the runtime privileges the backend needs. The app role now owns nothing, so it operates via
--    these explicit grants. (provision_erasure_privilege.sql then REVOKEs DELETE/TRUNCATE on the evidence tables
--    and removes all access to the execution-grant table -- the erasure-specific tightening on top of this.)
GRANT USAGE ON SCHEMA public TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_role";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO :"app_role";
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO :"app_role";
-- Objects a FUTURE migration (run as guvfx_migrate) creates must remain usable by the app role.
ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_migrate IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_migrate IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_migrate IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO :"app_role";

COMMIT;
\echo 'demote_app_role.sql: COMMITTED. Application role demoted to least privilege; ownership moved to guvfx_schema_owner.'
