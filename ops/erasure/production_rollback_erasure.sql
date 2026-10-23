-- ADR-0054 -- PRODUCTION capability rollback for controlled erasure. SUPERUSER-ONLY. SEPARATELY AUTHORISED.
-- ============================================================================================================
-- This is DISTINCT from ops/erasure/rollback_erasure_privilege.sql (the DISPOSABLE-REHEARSAL teardown, which
-- restores the database to its original pre-provision arrangement). A PRODUCTION rollback must NOT return the
-- evidence tables or schema `public` to the runtime application role, and must NOT re-grant the app role
-- DELETE/TRUNCATE/DDL -- doing so would re-open the very owner-level destruction vectors the provisioning closed.
--
-- This runbook DISABLES the erasure capability while PRESERVING the secure ownership + privilege boundary:
--   1. Operator (out of band, FIRST): set BROKER_INTELLIGENCE_ERASURE_ENABLED off and remove the
--      ERASURE_EXECUTOR_DB_* credentials from every environment (so no executor connection can be opened).
--   2. This script: drop the governed procedure + helpers (removes the ONLY path that deletes immutable
--      evidence), revoke + drop every executor login and the executor group (removes execute authority), and
--      revoke/clear outstanding execution grants.
--   3. LEAVE intact: schema `public` owned by guvfx_schema_owner; evidence tables + trigger functions +
--      sequences owned by guvfx_schema_owner; the app role's DELETE/TRUNCATE/CREATE revocations.
--
-- Guards (same as provisioning): -v target_db=<current db> + -v allow_destructive_provisioning=1.
--   psql "$DSN" -v ON_ERROR_STOP=1 -v target_db=guvfx -v allow_destructive_provisioning=1 \
--        -f ops/erasure/production_rollback_erasure.sql
\set ON_ERROR_STOP on
\if :{?target_db}
\else
  \echo '*** REFUSED: pass -v target_db=<current database name>'
  DO $refuse$ BEGIN RAISE EXCEPTION 'refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\if :{?allow_destructive_provisioning}
\else
  \echo '*** REFUSED: pass -v allow_destructive_provisioning=1 to acknowledge.'
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

-- 1) Remove the ONLY path that can delete immutable evidence.
DROP FUNCTION IF EXISTS public.bi_governed_erase_owner(bigint, text);
DROP FUNCTION IF EXISTS public.bi_governed_erase_owner(bigint, bigint, text);
DROP FUNCTION IF EXISTS public.bi_erasure_plan_facts(bigint);
DROP FUNCTION IF EXISTS public.bi_erasure_plan_matches(jsonb, jsonb);

-- 2) Revoke execute authority: drop every per-operator executor LOGIN, then the NOLOGIN group.
DO $drop$
DECLARE r text;
BEGIN
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE 'guvfx_erx_%' LOOP
    EXECUTE format('DROP OWNED BY %I CASCADE', r);
    EXECUTE format('DROP ROLE %I', r);
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'guvfx_erasure_exec') THEN
    EXECUTE 'DROP OWNED BY guvfx_erasure_exec CASCADE';   -- the EXECUTE grant is already gone with the function
    EXECUTE 'DROP ROLE guvfx_erasure_exec';
  END IF;
END
$drop$;

-- 3) Neutralise any outstanding execution grants (belt-and-suspenders; the procedure they authorise is gone).
UPDATE broker_intelligence_erasureexecutiongrant
   SET revoked = true, consumed_at = coalesce(consumed_at, now())
 WHERE revoked = false AND consumed_at IS NULL;

-- DELIBERATELY NOT DONE (the secure boundary is preserved):
--   * schema public + evidence tables stay owned by guvfx_schema_owner;
--   * the app role is NOT re-granted DELETE/TRUNCATE on the evidence tables, nor CREATE on public;
--   * the BEFORE-DELETE immutability triggers remain installed + enabled.
-- Immutability of evidence is therefore STRONGER after this rollback, not weaker: no governed delete path exists
-- and the ordinary role still cannot delete/truncate/DDL. Re-enabling erasure requires a fresh provisioning run.

COMMIT;
\echo 'production_rollback_erasure.sql: COMMITTED. Erasure capability disabled; secure ownership boundary preserved.'
