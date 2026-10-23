-- ADR-0054 / F-8 -- ROLLBACK of ops/erasure/separate_runtime_role.sql (bootstrap-SAFE, manifest-driven).
-- ============================================================================================================
-- SUPERUSER-ONLY. SEPARATELY SPONSOR-AUTHORISED. Reverses the runtime-role separation using the SAME reviewed
-- cutover manifest (TEMP table `_cutover_manifest`, prepended by ops/erasure/cutover.sh --rollback). It NEVER
-- makes the app role an owner or superuser, NEVER uses a blanket REASSIGN OWNED, and NEVER demotes the bootstrap
-- role (which was never demoted) -- so the cluster always retains its controlled superuser.
--
-- ADDITIONAL ROLLBACK SAFETY (F, "rollback safety"): the DB-privilege rollback revokes the runtime grants that
-- the application depends on. It therefore REFUSES while any session is connected as the app role, unless the
-- operator passes -v emergency_override=1 (documented, audited). The intended order is: (1) configuration
-- rollback -- re-point DB_USER back to the bootstrap role + redeploy, draining every app/worker connection as the
-- app role; (2) confirm pg_stat_activity shows zero app-role sessions; (3) THEN run this.
--
-- Use ONLY to revert a separation that has NOT been followed by erasure provisioning (if the governed procedure
-- exists, run ops/erasure/production_rollback_erasure.sql FIRST, then this).
--
-- Required: -v target_db -v allow_destructive_provisioning=1 -v bootstrap_role -v app_role -v app_schema
-- Optional: -v emergency_override=1 (skip the active-session guard; records WHY in the output).
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo '*** REFUSED: pass -v target_db.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?allow_destructive_provisioning}
\else
  \echo '*** REFUSED: pass -v allow_destructive_provisioning=1.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?bootstrap_role}
\else
  \echo '*** REFUSED: pass -v bootstrap_role.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?app_schema}
\else
  \echo '*** REFUSED: pass -v app_schema.'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?emergency_override}
\else
  \set emergency_override 0
\endif

BEGIN;
SELECT CASE WHEN current_database() = :'target_db' THEN 'true' ELSE 'false' END AS _dbok \gset
\if :_dbok
\else
  \echo '*** REFUSED: current_database() does not match -v target_db. Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- Ordering guard: refuse while the erasure capability is still provisioned (reverse provisioning first).
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE p.proname='bi_governed_erase_owner' AND n.nspname='public') THEN 'true' ELSE 'false' END AS _prov \gset
\if :_prov
  \echo '*** REFUSED: erasure capability still provisioned (bi_governed_erase_owner present). Run'
  \echo '    production_rollback_erasure.sql FIRST, then re-run this separation rollback. Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- Bootstrap role must still be a superuser (sanity: we never demoted it; a missing superuser is a red flag).
SELECT CASE WHEN (SELECT rolsuper FROM pg_roles WHERE rolname = :'bootstrap_role') THEN 'true' ELSE 'false' END AS _boot \gset
\if :_boot
\else
  \echo '*** REFUSED: bootstrap_role is not a superuser -- unexpected; manual review required. Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

-- ADDITIONAL ROLLBACK SAFETY: refuse while any session is connected as the app role (drain + config-rollback
-- first), unless an explicit, documented emergency override is passed.
SELECT count(*) AS _appsessions FROM pg_stat_activity
  WHERE usename = :'app_role' AND pid <> pg_backend_pid() \gset
\if :emergency_override
  \echo '*** WARNING: emergency_override=1 -- bypassing the active-app-session guard. Active app-role sessions:'
  \echo '    (count below). This is only safe if the application is already stopped or re-pointed.'
  \echo :_appsessions
\else
  SELECT CASE WHEN :_appsessions > 0 THEN 'true' ELSE 'false' END AS _busy \gset
  \if :_busy
    \echo '*** REFUSED: app-role sessions are still connected. Re-point DB_USER back to the bootstrap role and'
    \echo '    drain all app/worker connections FIRST (then pg_stat_activity shows zero), or pass'
    \echo '    -v emergency_override=1 with documented justification. Rolling back.'
    ROLLBACK;
    DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
  \endif
\endif

-- The reviewed manifest MUST be present (prepended by cutover.sh --rollback): reversal is manifest-driven.
SELECT CASE WHEN to_regclass('pg_temp._cutover_manifest') IS NOT NULL THEN 'true' ELSE 'false' END AS _man \gset
\if :_man
\else
  \echo '*** REFUSED: _cutover_manifest TEMP table absent. Run via ops/erasure/cutover.sh --rollback. Rolling back.'
  ROLLBACK;
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif

SELECT set_config('x.schema', :'app_schema', false);
SELECT set_config('x.boot', :'bootstrap_role', false);
SELECT set_config('x.app_role', :'app_role', false);

-- Precondition (fail-closed): this rollback only operates on the exact post-separation state. Refuse if a prior
-- step has inverted ownership -- guvfx_schema_owner absent, the app role owning any object, or a manifest object
-- not currently owned by guvfx_schema_owner. (This is what catches a careless pre-F-8 erasure teardown having
-- already handed ownership to the app role: fix that forward, do not run this blindly.)
DO $precond$
DECLARE bad text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_schema_owner') THEN
    RAISE EXCEPTION 'rollback refused: guvfx_schema_owner absent -- the post-separation owner is gone; restore it before reversing';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname=current_setting('x.schema') AND c.relkind IN ('r','p','S','v','m','f')
               AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_setting('x.app_role')))
     OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname=current_setting('x.schema')
                  AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_setting('x.app_role'))) THEN
    RAISE EXCEPTION 'rollback refused: the app role owns object(s) in the schema -- unexpected inverted state; operator review required';
  END IF;
  SELECT string_agg(l.obj_identity, '; ') INTO bad FROM (
    SELECT c.relname AS obj_identity, pg_get_userbyid(c.relowner) AS owner,
           (CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview'
                           WHEN 'f' THEN 'foreign_table' ELSE 'table' END) AS obj_kind, n.nspname AS obj_schema
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname=current_setting('x.schema') AND c.relkind IN ('r','p','S','v','m','f')
       AND NOT (c.relkind='S' AND EXISTS (SELECT 1 FROM pg_depend d2 WHERE d2.classid='pg_class'::regclass
                      AND d2.objid=c.oid AND d2.refclassid='pg_class'::regclass AND d2.deptype IN ('a','i')))
  ) l JOIN _cutover_manifest m USING (obj_kind,obj_schema,obj_identity)
    WHERE l.owner <> 'guvfx_schema_owner';
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'rollback refused: manifest object(s) not owned by guvfx_schema_owner (state is not the expected post-separation one): %', bad;
  END IF;
END
$precond$;

-- 1) Revoke the runtime grants + default privileges granted to the app role (match separate_runtime_role.sql:
--    tables S/I/U[/D], sequences U/S/U, schema USAGE, default privileges S/I/U; NO EXECUTE was ever granted).
SELECT format('REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I FROM %I', :'app_schema', :'app_role') AS _r1 \gset
SELECT format('REVOKE USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I FROM %I', :'app_schema', :'app_role') AS _r2 \gset
SELECT format('REVOKE USAGE ON SCHEMA %I FROM %I', :'app_schema', :'app_role') AS _r3 \gset
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I REVOKE SELECT, INSERT, UPDATE ON TABLES FROM %I', :'app_schema', :'app_role') AS _r4 \gset
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE guvfx_schema_owner IN SCHEMA %I REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM %I', :'app_schema', :'app_role') AS _r5 \gset
:_r1 ;
:_r2 ;
:_r4 ;
:_r5 ;
:_r3 ;

-- 2) Reverse ownership of the MANIFEST-approved application objects (not a blanket sweep) from
--    guvfx_schema_owner back to the bootstrap role; restore the schema to its original owner.
DO $rel$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT c.oid, c.relname, c.relkind FROM pg_class c JOIN pg_namespace s ON s.oid=c.relnamespace
    JOIN _cutover_manifest m
      ON m.obj_schema = s.nspname AND m.obj_identity = c.relname
     AND m.obj_kind = (CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview'
                                      WHEN 'f' THEN 'foreign_table' ELSE 'table' END)
    WHERE s.nspname=current_setting('x.schema')
      AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname='guvfx_schema_owner')
      AND c.relkind IN ('r','S','v','m','f','p')
      AND NOT (c.relkind='S' AND EXISTS (SELECT 1 FROM pg_depend d2 WHERE d2.classid='pg_class'::regclass
                      AND d2.objid=c.oid AND d2.refclassid='pg_class'::regclass AND d2.deptype IN ('a','i')))
  LOOP
    EXECUTE format('ALTER %s %I.%I OWNER TO %I',
      CASE WHEN r.relkind='S' THEN 'SEQUENCE' WHEN r.relkind='v' THEN 'VIEW'
           WHEN r.relkind='m' THEN 'MATERIALIZED VIEW' WHEN r.relkind='f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
      current_setting('x.schema'), r.relname, current_setting('x.boot'));
    n := n + 1;
  END LOOP;
  RAISE NOTICE 'reverted % manifest relation(s) to the bootstrap role', n;
END
$rel$;
DO $fn$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS args, p.prokind
    FROM pg_proc p JOIN pg_namespace s ON s.oid=p.pronamespace
    JOIN _cutover_manifest m
      ON m.obj_schema = s.nspname AND m.obj_identity = p.proname||'('||pg_get_function_identity_arguments(p.oid)||')'
     AND m.obj_kind = (CASE p.prokind WHEN 'a' THEN 'aggregate' WHEN 'p' THEN 'procedure' ELSE 'function' END)
    WHERE s.nspname=current_setting('x.schema')
      AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname='guvfx_schema_owner')
  LOOP
    EXECUTE format('ALTER %s %I.%I(%s) OWNER TO %I',
      CASE WHEN r.prokind='p' THEN 'PROCEDURE' WHEN r.prokind='a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
      current_setting('x.schema'), r.proname, r.args, current_setting('x.boot'));
    n := n + 1;
  END LOOP;
  RAISE NOTICE 'reverted % manifest routine(s) to the bootstrap role', n;
END
$fn$;
SELECT format('ALTER SCHEMA %I OWNER TO pg_database_owner', :'app_schema') AS _sch \gset
:_sch ;

-- 3) Reset guvfx_migrate's acting-role default (leave the roles in place for the operator to drop if desired).
ALTER ROLE guvfx_migrate RESET role;

-- The bootstrap role was never demoted; the cluster still has its controlled superuser. The app role owns
-- nothing (we only revoked grants); it is never made an owner or superuser by this rollback.
COMMIT;
\echo 'separate_runtime_role_rollback.sql: COMMITTED. Runtime grants revoked; manifest objects restored to the bootstrap role; bootstrap superuser intact.'
