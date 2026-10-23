-- ADR-0054 -- ROLLBACK of ops/erasure/demote_app_role.sql (restore the PRIOR application-role posture).
-- ============================================================================================================
-- SUPERUSER-ONLY. SEPARATELY SPONSOR-AUTHORISED. Use ONLY to revert a demotion that has NOT yet been followed
-- by erasure provisioning (if provisioning has run, roll that back first with production_rollback_erasure.sql).
--
-- WARNING: this returns the application role to its prior ELEVATED posture (on the current production it was a
-- SUPERUSER) and returns object ownership to it. That re-opens the boundary the demotion closed; it exists only
-- so a demotion that broke the application can be reverted cleanly during the maintenance window. The prior
-- posture is passed EXPLICITLY so this script never guesses it.
--
-- Guards:
--   -v target_db=<current db> -v allow_destructive_provisioning=1 -v app_role=<role>
--   -v prior_posture=SUPERUSER            (the exact attribute to restore; production value was SUPERUSER)
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo '*** REFUSED: pass -v target_db.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?allow_destructive_provisioning}
\else
  \echo '*** REFUSED: pass -v allow_destructive_provisioning=1.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?prior_posture}
\else
  \echo '*** REFUSED: pass -v prior_posture=SUPERUSER (the exact prior attribute to restore). Never guessed.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

BEGIN;
SELECT CASE WHEN current_database() = :'target_db' THEN 'true' ELSE 'false' END AS _dbok \gset
\if :_dbok
\else
  \echo '*** REFUSED: current_database() does not match -v target_db. Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- Ordering guard (adversarial-review C5): refuse while the erasure capability is still provisioned (the
-- governed procedure exists). Reverting the demotion now would drag the evidence + execution-grant tables back
-- to the app role while the procedure + BEFORE-DELETE triggers are live -- an inconsistent, exploitable state.
-- Disable the capability FIRST with ops/erasure/production_rollback_erasure.sql, THEN run this rollback.
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE p.proname='bi_governed_erase_owner' AND n.nspname='public') THEN 'true' ELSE 'false' END AS _provisioned \gset
\if :_provisioned
  \echo '*** REFUSED: erasure capability still provisioned (bi_governed_erase_owner present). Run'
  \echo '    production_rollback_erasure.sql FIRST to disable it, THEN re-run this demotion rollback. Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- F-8 SAFETY GUARD (adopted with the runtime-role separation). This demotion rollback predates F-8: it does
-- REASSIGN OWNED BY guvfx_schema_owner TO <app_role> (L67) and restores an elevated posture (L72). In the pre-F-8
-- model guvfx_schema_owner owned only a handful of evidence objects and the app role WAS the elevated owner. In
-- the F-8 runtime-role-separation model guvfx_schema_owner owns the ENTIRE application schema and the runtime
-- guvfx_app must OWN NOTHING, so running this would hand the whole schema + the immutable-evidence tables to the
-- runtime role and re-elevate it -- collapsing the ADR-0054 boundary. Refuse fail-closed if that model is
-- detected (symmetric to rollback_erasure_privilege.sql's guard).
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
      AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'guvfx_schema_owner')
      AND c.relname NOT IN ('broker_intelligence_evidenceblob','broker_intelligence_brokerevent',
                            'broker_intelligence_erasureexecutiongrant')
  ) THEN 'true' ELSE 'false' END AS _f8model \gset
\if :_f8model
  \echo '*** REFUSED: guvfx_schema_owner owns general application tables -- this is the F-8 runtime-role'
  \echo '    separation model. This pre-F-8 demotion rollback would REASSIGN the whole schema + evidence tables'
  \echo '    to the app role and restore an elevated posture. Use ops/erasure/separate_runtime_role_rollback.sql'
  \echo '    (reverts ownership to the bootstrap role, never the app role). Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'rollback refused (fail-closed): F-8 model detected; see message above'; END $refuse$;
\endif

-- 1) Revoke the runtime grants + default privileges the demotion added.
ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_migrate IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_migrate IN SCHEMA public REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_migrate IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM :"app_role";
REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM :"app_role";
REVOKE USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public FROM :"app_role";
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM :"app_role";

-- 2) Return object ownership to the application role.
REASSIGN OWNED BY guvfx_schema_owner TO :"app_role";

-- 3) Restore the prior cluster posture EXACTLY as supplied (production prior value: SUPERUSER).
\if :{?prior_posture}
\endif
SELECT format('ALTER ROLE %I %s', :'app_role', :'prior_posture') AS _stmt \gset
\echo 'Restoring prior posture:'
\echo :_stmt
:_stmt ;

-- 4) The now-empty helper roles may be dropped by the operator afterwards (left in place here; dropping a role
--    that still owns nothing is safe): DROP ROLE guvfx_migrate; DROP ROLE guvfx_schema_owner;
COMMIT;
\echo 'demote_app_role_rollback.sql: COMMITTED. Prior application-role posture + ownership restored.'
