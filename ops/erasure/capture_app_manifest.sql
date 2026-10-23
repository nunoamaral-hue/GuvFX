-- ADR-0054 / F-8 / F-3 -- READ-ONLY capture of the expected application-object manifest for a schema.
-- ============================================================================================================
-- Emits one pipe-separated line per application object in the approved schema that is owned by the (current)
-- application/bootstrap role and is NOT extension-owned:
--     obj_kind|obj_schema|obj_identity|current_owner|classification
-- classification is a conservative default (the two immutable-evidence tables -> 'protected'; everything else
-- -> 'application'); the operator REVIEWS/edits it, then it becomes the version-controlled cutover manifest
-- consumed by ops/erasure/separate_runtime_role.sql. Read-only: no mutation. Deterministic ORDER for a stable
-- digest.
--   psql ... -v app_schema=public -f ops/erasure/capture_app_manifest.sql > cutover_manifest.psv
\set ON_ERROR_STOP on
\if :{?app_schema}
\else
  \echo '*** pass -v app_schema=<approved application schema, e.g. public>'
  DO $refuse$ BEGIN RAISE EXCEPTION 'cutover refused (fail-closed): see the REFUSED message above'; END $refuse$;
\endif
\pset tuples_only on
\pset format unaligned
SET default_transaction_read_only = on;

SELECT line FROM (
  -- relations: tables (r,p), sequences (S), views (v), matviews (m), foreign tables (f)
  SELECT (CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview'
                         WHEN 'f' THEN 'foreign_table' ELSE 'table' END)
         ||'|'|| n.nspname ||'|'|| c.relname ||'|'|| pg_get_userbyid(c.relowner) ||'|'||
         (CASE WHEN c.relname IN ('broker_intelligence_evidenceblob','broker_intelligence_brokerevent')
               THEN 'protected' ELSE 'application' END) AS line
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = :'app_schema' AND c.relkind IN ('r','p','S','v','m','f')
     AND NOT EXISTS (SELECT 1 FROM pg_depend d
                     WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
  UNION ALL
  -- routines: functions, procedures, aggregates (identity includes the argument signature)
  SELECT (CASE p.prokind WHEN 'a' THEN 'aggregate' WHEN 'p' THEN 'procedure' ELSE 'function' END)
         ||'|'|| n.nspname ||'|'|| p.proname ||'('|| pg_get_function_identity_arguments(p.oid) ||')'
         ||'|'|| pg_get_userbyid(p.proowner) ||'|application' AS line
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = :'app_schema'
     AND NOT EXISTS (SELECT 1 FROM pg_depend d
                     WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')
) s
ORDER BY line;
