-- ADR-0054 — ROLLBACK of ops/erasure/demote_app_role.sql (restore the PRIOR application-role posture).
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
  \quit
\endif
\if :{?allow_destructive_provisioning}
\else
  \echo '*** REFUSED: pass -v allow_destructive_provisioning=1.'
  \quit
\endif
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role.'
  \quit
\endif
\if :{?prior_posture}
\else
  \echo '*** REFUSED: pass -v prior_posture=SUPERUSER (the exact prior attribute to restore). Never guessed.'
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
