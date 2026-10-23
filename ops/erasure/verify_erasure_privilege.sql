-- ADR-0054 -- Post-provision verification of the controlled-erasure privilege boundary.
-- Run after provision_erasure_privilege.sql. Read-only. Exits NONZERO (RAISE) if ANY invariant is unmet, so it
-- can gate a rehearsal/deploy. Mirrors erasure.privileged_backend_readiness().
--   psql "$DSN" -v ON_ERROR_STOP=1 -v app_role=guvfx_app -f ops/erasure/verify_erasure_privilege.sql
\set ON_ERROR_STOP on
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role=<application runtime role>'
  DO $refuse$ BEGIN RAISE EXCEPTION 'verify refused (fail-closed): missing -v app_role; no invariant was checked'; END $refuse$;
\endif

-- Pass app_role into the DO block via a GUC (psql does not interpolate :vars inside dollar-quoted blocks).
SELECT set_config('erasure.app_role', :'app_role', false);

DO $verify$
DECLARE
  problems text[] := ARRAY[]::text[];
  v_oid oid;
  v_count int;
  v_secdef boolean;
  v_owner text;
  v_cfg text[];
  v_args text;
  v_desc text;
  v_sp text;
  v_app text := current_setting('erasure.app_role');
  v_grantees text[];
  r text;
BEGIN
  -- (1) roles
  FOREACH r IN ARRAY ARRAY['guvfx_schema_owner','guvfx_erasure','guvfx_erasure_exec','guvfx_migrate'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      problems := problems || format('role %s absent', r); END IF;
  END LOOP;

  -- (2) exactly one procedure in public
  SELECT count(*) INTO v_count FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE p.proname = 'bi_governed_erase_owner' AND n.nspname = 'public';
  IF v_count = 0 THEN problems := problems || 'procedure absent'::text;
  ELSIF v_count > 1 THEN problems := problems || format('%s procedures named bi_governed_erase_owner (ambiguous)', v_count);
  ELSE
    SELECT p.oid, p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig,
           (SELECT string_agg(format_type(t, NULL), ', ') FROM unnest(p.proargtypes) AS t),
           obj_description(p.oid,'pg_proc')
      INTO v_oid, v_secdef, v_owner, v_cfg, v_args, v_desc
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE p.proname = 'bi_governed_erase_owner' AND n.nspname = 'public';
    IF replace(v_args,' ','') <> 'bigint,text' THEN problems := problems || format('bad signature: %s', v_args); END IF;
    IF NOT v_secdef THEN problems := problems || 'not SECURITY DEFINER'::text; END IF;
    IF v_owner <> 'guvfx_erasure' THEN problems := problems || format('owner is %s not guvfx_erasure', v_owner); END IF;
    SELECT substring(c FROM 'search_path=(.*)') INTO v_sp FROM unnest(coalesce(v_cfg,ARRAY[]::text[])) c
      WHERE c LIKE 'search_path=%' LIMIT 1;
    IF v_sp IS NULL THEN problems := problems || 'search_path not pinned'::text;
    ELSIF btrim(v_sp) <> 'pg_catalog, public' THEN problems := problems || format('search_path not exact: %s', v_sp); END IF;
    IF v_desc IS NULL OR position('ADR-0054/erasure-proc/v2' IN v_desc) = 0 THEN
      problems := problems || 'version marker missing/mismatched'::text; END IF;

    -- (3) EXECUTE ACL
    IF has_function_privilege('public', v_oid, 'EXECUTE') THEN problems := problems || 'PUBLIC has EXECUTE'::text; END IF;
    IF has_function_privilege(v_app, v_oid, 'EXECUTE') THEN problems := problems || 'app role has EXECUTE'::text; END IF;
    IF NOT has_function_privilege('guvfx_erasure_exec', v_oid, 'EXECUTE') THEN
      problems := problems || 'executor lacks EXECUTE'::text; END IF;
    IF (SELECT proacl IS NULL FROM pg_proc WHERE oid = v_oid) THEN
      problems := problems || 'procedure ACL is default (PUBLIC retains EXECUTE)'::text;
    ELSE
      SELECT coalesce(array_agg(grantee::regrole::text),ARRAY[]::text[]) INTO v_grantees
        FROM aclexplode((SELECT proacl FROM pg_proc WHERE oid = v_oid))
       WHERE privilege_type='EXECUTE' AND grantee <> 0
         AND grantee <> (SELECT proowner FROM pg_proc WHERE oid = v_oid);
      IF EXISTS (SELECT 1 FROM unnest(v_grantees) g WHERE g <> 'guvfx_erasure_exec') THEN
        problems := problems || format('unexpected EXECUTE grantees: %s', v_grantees); END IF;
    END IF;
  END IF;

  -- (4) evidence table + sequence ownership
  FOREACH r IN ARRAY ARRAY['broker_intelligence_evidenceblob','broker_intelligence_brokerevent'] LOOP
    IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = r) <> 'guvfx_schema_owner' THEN
      problems := problems || format('table %s not owned by guvfx_schema_owner', r); END IF;
  END LOOP;
  FOREACH r IN ARRAY ARRAY['broker_intelligence_evidenceblob_id_seq','broker_intelligence_brokerevent_id_seq'] LOOP
    IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = r AND relkind='S') <> 'guvfx_schema_owner' THEN
      problems := problems || format('sequence %s not owned by guvfx_schema_owner', r); END IF;
  END LOOP;

  -- (5) trigger function ownership
  FOREACH r IN ARRAY ARRAY['bi_evidenceblob_block_delete','bi_brokerevent_block_delete'] LOOP
    IF (SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = r) <> 'guvfx_schema_owner' THEN
      problems := problems || format('trigger function %s not owned by guvfx_schema_owner', r); END IF;
  END LOOP;

  -- (6) delete triggers installed + firing in an origin session (tgenabled O/A)
  FOREACH r IN ARRAY ARRAY['bi_evidenceblob_no_delete','bi_brokerevent_no_delete'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = r AND NOT tgisinternal) THEN
      problems := problems || format('trigger %s not installed', r);
    ELSIF (SELECT tgenabled FROM pg_trigger WHERE tgname = r AND NOT tgisinternal) NOT IN ('O','A') THEN
      problems := problems || format('trigger %s does not fire in an origin session', r); END IF;
  END LOOP;

  -- (6b) schema public owned by the schema owner; app role has no CREATE on it (closes DROP SCHEMA CASCADE)
  IF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public') IS DISTINCT FROM 'guvfx_schema_owner' THEN
    problems := problems || 'schema public not owned by guvfx_schema_owner'::text; END IF;
  IF has_schema_privilege(v_app, 'public', 'CREATE') THEN
    problems := problems || 'app role has CREATE on schema public'::text; END IF;

  -- (6c) execution-grant table (P0 executor trust anchor): off the app role; app has no privilege; PUBLIC none;
  -- guvfx_erasure consume-only (no INSERT).
  IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='broker_intelligence_erasureexecutiongrant')
       IS DISTINCT FROM 'guvfx_schema_owner' THEN
    problems := problems || 'execution-grant table not owned by guvfx_schema_owner'::text; END IF;
  FOREACH r IN ARRAY ARRAY['SELECT','INSERT','UPDATE','DELETE'] LOOP
    IF has_table_privilege(v_app, 'broker_intelligence_erasureexecutiongrant', r) THEN
      problems := problems || format('app has %s on the execution-grant table', r); END IF;
  END LOOP;
  IF has_table_privilege('public', 'broker_intelligence_erasureexecutiongrant', 'SELECT') THEN
    problems := problems || 'PUBLIC has SELECT on the execution-grant table'::text; END IF;
  IF has_table_privilege('guvfx_erasure', 'broker_intelligence_erasureexecutiongrant', 'INSERT') THEN
    problems := problems || 'guvfx_erasure has INSERT on the execution-grant table'::text; END IF;

  -- (7) app role: LEAST PRIVILEGE -- no elevated cluster attribute; not a member of / able to assume any
  -- privileged role (incl. guvfx_migrate). A SUPERUSER/BYPASSRLS/CREATEROLE/CREATEDB app role would bypass the
  -- REVOKEs and the immutability triggers (P0 production superuser blocker). Mirrors privileged_backend_readiness.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=v_app AND rolsuper) THEN
    problems := problems || 'app role is SUPERUSER (bypasses the REVOKEs and the immutability triggers)'::text; END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=v_app AND rolcreaterole) THEN
    problems := problems || 'app role has CREATEROLE'::text; END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=v_app AND rolcreatedb) THEN
    problems := problems || 'app role has CREATEDB'::text; END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=v_app AND rolbypassrls) THEN
    problems := problems || 'app role has BYPASSRLS'::text; END IF;
  FOREACH r IN ARRAY ARRAY['guvfx_schema_owner','guvfx_erasure','guvfx_erasure_exec','guvfx_migrate'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=r)
       AND (pg_has_role(v_app, r, 'USAGE') OR pg_has_role(v_app, r, 'MEMBER')) THEN
      problems := problems || format('app role is a member of or can assume %s', r); END IF;
  END LOOP;
  -- Transitive escalation: no role the app role can SET ROLE to may itself carry an elevated attribute.
  IF EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname <> v_app AND pg_has_role(v_app, r.oid, 'MEMBER')
             AND (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolbypassrls)) THEN
    problems := problems || 'app role can assume a role with an elevated attribute (SUPERUSER/CREATEROLE/CREATEDB/BYPASSRLS) -- transitive escalation'::text; END IF;

  -- (7d) F-8: the runtime app role must OWN NOTHING in the application schema (ownership lives on the schema
  -- owner; the app role operates via grants only). Catches a role that is still an object owner.
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname=v_app)
               AND c.relkind IN ('r','S','v','m','f','p')) THEN
    problems := problems || 'app role owns relation(s) in public -- runtime role must own nothing'::text; END IF;
  IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
             WHERE n.nspname='public' AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=v_app)) THEN
    problems := problems || 'app role owns function(s) in public -- runtime role must own nothing'::text; END IF;

  -- (8) app role: no DELETE/TRUNCATE on evidence tables
  FOREACH r IN ARRAY ARRAY['broker_intelligence_evidenceblob','broker_intelligence_brokerevent'] LOOP
    IF has_table_privilege(v_app, r, 'DELETE') THEN problems := problems || format('app has DELETE on %s', r); END IF;
    IF has_table_privilege(v_app, r, 'TRUNCATE') THEN problems := problems || format('app has TRUNCATE on %s', r); END IF;
  END LOOP;

  -- (9) F-2.8: generalised privileged-routine containment. No SECURITY DEFINER routine in the application schema
  -- may be EXECUTE-able by PUBLIC or by the runtime app role. The erasure procedure is one such routine (covered
  -- specifically above); this catches ANY SECURITY DEFINER function a migration might add. Trigger functions
  -- need no invoker EXECUTE and the app calls no custom DB routine, so the runtime role should execute none.
  IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
             WHERE n.nspname='public' AND p.prosecdef AND has_function_privilege('public', p.oid, 'EXECUTE')) THEN
    problems := problems || 'PUBLIC can EXECUTE a SECURITY DEFINER routine in public'::text; END IF;
  SELECT coalesce(string_agg(n.nspname||'.'||p.proname, ', '), '') INTO v_args
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname='public' AND p.prosecdef AND has_function_privilege(v_app, p.oid, 'EXECUTE');
  IF v_args <> '' THEN
    problems := problems || format('app role can EXECUTE SECURITY DEFINER routine(s) [privileged-routine escalation]: %s', v_args); END IF;

  IF array_length(problems,1) > 0 THEN
    RAISE EXCEPTION 'VERIFY FAILED (% problems): %', array_length(problems,1), array_to_string(problems, ' | ');
  END IF;
  RAISE NOTICE 'VERIFY OK: controlled-erasure privilege boundary satisfies every invariant.';
END
$verify$;
