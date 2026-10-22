-- ADR-0054 — Reverse the controlled-erasure privilege provisioning (REHEARSAL rollback).
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

-- 1) Drop the governed procedure + helpers (owned by guvfx_erasure).
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

-- 3b) Restore schema ownership + CREATE to the application role (reverses the HIGH-finding hardening).
ALTER SCHEMA public OWNER TO :"app_role";
GRANT CREATE ON SCHEMA public TO :"app_role";

-- 4) Remove privileged-role grants, then drop the roles. DROP OWNED cleans up any remaining grants/defaults.
DO $drop$
DECLARE r text;
BEGIN
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
