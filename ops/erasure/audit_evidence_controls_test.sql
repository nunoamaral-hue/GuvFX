-- ADR-0054 / F-8 -- Self-checking TEST HARNESS for the evidence-destruction audit controls.
-- ============================================================================================================
-- Catalog assertions (Section A) + negative-control denials (Section B) + positive/tamper controls (Section C).
-- Each assertion RAISEs EXCEPTION on failure; with ON_ERROR_STOP the first failure aborts with a clear message.
--
-- PREREQUISITES (prepared by the disposable-rehearsal harness, NOT by this file):
--   * the F-8 roles exist: guvfx_schema_owner, guvfx_migrate, guvfx_app, guvfx_erasure_exec;
--   * the two evidence tables exist and are owned by guvfx_schema_owner;
--   * audit_evidence_controls.sql has been installed as the bootstrap superuser.
-- Run AS THE BOOTSTRAP SUPERUSER:  psql -U <bootstrap> -d <db> -v ON_ERROR_STOP=1 -f audit_evidence_controls_test.sql
--
-- SAFETY: Sections A and B never mutate (B's attempts are all expected-denied). Section C MUTATES evidence
-- (DELETE/TRUNCATE/disable-trigger) and is therefore GATED behind -v allow_mutating=1. NEVER pass that flag
-- against production or the certified restored copy -- disposable clusters only.
\set ON_ERROR_STOP on

\echo '== Section A -- catalog assertions (non-mutating) =='

-- A1: audit schema owned by a SUPERUSER bootstrap role, and NOT by any app/migration/erasure/schema-owner role.
DO $$
DECLARE owner_super boolean; owner_name text;
BEGIN
  SELECT r.rolname, r.rolsuper INTO owner_name, owner_super
  FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname='audit';
  IF owner_name IS NULL THEN RAISE EXCEPTION 'ASSERT FAILED: audit schema missing'; END IF;
  IF NOT owner_super THEN RAISE EXCEPTION 'ASSERT FAILED: audit schema owner % is not a superuser', owner_name; END IF;
  IF owner_name IN ('guvfx_schema_owner','guvfx_migrate','guvfx_app','guvfx_erasure_exec') THEN
    RAISE EXCEPTION 'ASSERT FAILED: audit schema owned by a non-bootstrap role %', owner_name; END IF;
  RAISE NOTICE 'PASS A1: audit schema owned by superuser bootstrap role %', owner_name;
END $$;

-- A2: evidence_audit owned by the same bootstrap role; NO privileges granted to any app/migration/erasure role
--     (append-only -- only the SECURITY DEFINER writer inserts).
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(DISTINCT grantee::regrole::text||':'||privilege_type, ',')
  INTO bad
  FROM pg_class c, aclexplode(c.relacl) a
  WHERE c.oid='audit.evidence_audit'::regclass
    AND a.grantee <> c.relowner
    AND a.grantee::regrole::text IN ('guvfx_schema_owner','guvfx_migrate','guvfx_app','guvfx_erasure_exec','public');
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'ASSERT FAILED: evidence_audit grants to non-owner: %', bad; END IF;
  RAISE NOTICE 'PASS A2: evidence_audit append-only (no non-owner grants)';
END $$;

-- A3: every privileged function is SECURITY DEFINER with a FIXED EMPTY search_path.
DO $$
DECLARE r record; cnt int := 0;
BEGIN
  FOR r IN
    SELECT p.oid, p.proname, p.prosecdef, p.proconfig
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='audit' AND p.proname IN ('_write','on_evidence_dml','on_sql_drop','on_ddl_end')
  LOOP
    cnt := cnt + 1;
    IF NOT r.prosecdef THEN RAISE EXCEPTION 'ASSERT FAILED: audit.% not SECURITY DEFINER', r.proname; END IF;
    -- an empty search_path is stored by PostgreSQL as the proconfig element  search_path=""  (quoted empty).
    IF r.proconfig IS NULL
       OR NOT EXISTS (SELECT 1 FROM unnest(r.proconfig) e WHERE e IN ('search_path=""','search_path=')) THEN
      RAISE EXCEPTION 'ASSERT FAILED: audit.% search_path not fixed-empty (proconfig=%)', r.proname, r.proconfig; END IF;
  END LOOP;
  IF cnt <> 4 THEN RAISE EXCEPTION 'ASSERT FAILED: expected 4 privileged functions, found %', cnt; END IF;
  RAISE NOTICE 'PASS A3: 4 functions SECURITY DEFINER + fixed empty search_path';
END $$;

-- A4: PUBLIC and all app/migration/erasure/schema-owner roles have NO EXECUTE on any privileged function.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(p.proname||'->'||a.grantee::regrole::text, ',')
  INTO bad
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace, aclexplode(p.proacl) a
  WHERE n.nspname='audit' AND p.proname IN ('_write','on_evidence_dml','on_sql_drop','on_ddl_end')
    AND a.privilege_type='EXECUTE'
    AND (a.grantee=0  -- PUBLIC
         OR a.grantee::regrole::text IN ('guvfx_schema_owner','guvfx_migrate','guvfx_app','guvfx_erasure_exec'));
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'ASSERT FAILED: unexpected EXECUTE grant(s): %', bad; END IF;
  RAISE NOTICE 'PASS A4: no EXECUTE to PUBLIC or any non-bootstrap role';
END $$;

-- A5: NO dynamic SQL in the privileged function bodies (no EXECUTE / format()).
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(p.proname, ',') INTO bad
  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname='audit' AND p.proname IN ('_write','on_evidence_dml','on_sql_drop','on_ddl_end')
    AND (p.prosrc ~* '\mEXECUTE\M' OR p.prosrc ~* '\mformat\s*\(');
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'ASSERT FAILED: dynamic SQL present in: %', bad; END IF;
  RAISE NOTICE 'PASS A5: no dynamic SQL in privileged bodies';
END $$;

-- A6: both event triggers exist, are ENABLED, and owned by a superuser bootstrap role.
DO $$
DECLARE n int; badowner text;
BEGIN
  SELECT count(*) INTO n FROM pg_event_trigger
    WHERE evtname IN ('et_evidence_sql_drop','et_evidence_ddl_end') AND evtenabled <> 'D';
  IF n <> 2 THEN RAISE EXCEPTION 'ASSERT FAILED: expected 2 enabled event triggers, found %', n; END IF;
  SELECT string_agg(evtname, ',') INTO badowner FROM pg_event_trigger e JOIN pg_roles r ON r.oid=e.evtowner
    WHERE e.evtname IN ('et_evidence_sql_drop','et_evidence_ddl_end')
      AND (NOT r.rolsuper OR r.rolname IN ('guvfx_schema_owner','guvfx_migrate','guvfx_app','guvfx_erasure_exec'));
  IF badowner IS NOT NULL THEN RAISE EXCEPTION 'ASSERT FAILED: event trigger(s) not bootstrap-owned: %', badowner; END IF;
  RAISE NOTICE 'PASS A6: 2 enabled event triggers, bootstrap-owned';
END $$;

-- A7: the four DML triggers exist and are enabled on the two evidence tables.
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM pg_trigger
    WHERE tgname IN ('z_audit_del','z_audit_trunc','z_audit_del2','z_audit_trunc2') AND tgenabled <> 'D';
  IF n <> 4 THEN RAISE EXCEPTION 'ASSERT FAILED: expected 4 enabled DML triggers, found %', n; END IF;
  RAISE NOTICE 'PASS A7: 4 enabled DML triggers on evidence tables';
END $$;

-- A8: the stable-OID registry exists, is bootstrap-owned + append-only, and pins exactly the 2 evidence tables
--     and 4 DML triggers (detection keys on these OIDs, not on mutable names).
DO $$
DECLARE owner_name text; bad text; nt int; ng int;
BEGIN
  SELECT pg_get_userbyid(relowner) INTO owner_name FROM pg_class WHERE oid='audit.protected_objid'::regclass;
  IF owner_name IN ('guvfx_schema_owner','guvfx_migrate','guvfx_app','guvfx_erasure_exec') THEN
    RAISE EXCEPTION 'ASSERT FAILED: protected_objid owned by non-bootstrap role %', owner_name; END IF;
  SELECT string_agg(DISTINCT grantee::regrole::text, ',') INTO bad
    FROM pg_class c, aclexplode(c.relacl) a
   WHERE c.oid='audit.protected_objid'::regclass AND a.grantee <> c.relowner
     AND a.grantee::regrole::text IN ('guvfx_schema_owner','guvfx_migrate','guvfx_app','guvfx_erasure_exec','public');
  IF bad IS NOT NULL THEN RAISE EXCEPTION 'ASSERT FAILED: protected_objid grants to non-owner: %', bad; END IF;
  SELECT count(*) FILTER (WHERE note='evidence-table'), count(*) FILTER (WHERE note='audit-dml-trigger')
    INTO nt, ng FROM audit.protected_objid;
  IF nt <> 2 OR ng <> 4 THEN RAISE EXCEPTION 'ASSERT FAILED: registry pins % tables + % triggers (expected 2+4)', nt, ng; END IF;
  RAISE NOTICE 'PASS A8: OID registry bootstrap-owned, append-only, pins 2 tables + 4 triggers';
END $$;

\echo '== Section B -- negative controls: the trusted migration identity cannot defeat the audit (non-mutating) =='

-- helper pattern: attempt as a restricted role; any error = denial = PASS; silent success = ASSERT FAILED.
-- B1: migrate (via SET ROLE schema_owner) cannot DROP an event trigger.
DO $$ DECLARE ok boolean:=false; BEGIN
  BEGIN EXECUTE 'SET ROLE guvfx_schema_owner'; EXECUTE 'DROP EVENT TRIGGER et_evidence_sql_drop';
  EXCEPTION WHEN OTHERS THEN ok:=true; END;
  RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: schema_owner DROPPED an event trigger'; END IF;
  RAISE NOTICE 'PASS B1: migration identity cannot DROP event trigger';
END $$;

-- B2: cannot DISABLE an event trigger.
DO $$ DECLARE ok boolean:=false; BEGIN
  BEGIN EXECUTE 'SET ROLE guvfx_schema_owner'; EXECUTE 'ALTER EVENT TRIGGER et_evidence_ddl_end DISABLE';
  EXCEPTION WHEN OTHERS THEN ok:=true; END;
  RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: schema_owner DISABLED an event trigger'; END IF;
  RAISE NOTICE 'PASS B2: migration identity cannot DISABLE event trigger';
END $$;

-- B3: cannot CREATE OR REPLACE the privileged writer (change the function).
DO $$ DECLARE ok boolean:=false; BEGIN
  BEGIN EXECUTE 'SET ROLE guvfx_schema_owner';
        EXECUTE 'CREATE OR REPLACE FUNCTION audit._write(text,text,text) RETURNS void LANGUAGE sql AS $x$ SELECT $x$';
  EXCEPTION WHEN OTHERS THEN ok:=true; END;
  RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: schema_owner replaced audit._write'; END IF;
  RAISE NOTICE 'PASS B3: migration identity cannot change the privileged writer';
END $$;

-- B4: cannot DROP or alter the audit schema.
DO $$ DECLARE ok boolean:=false; BEGIN
  BEGIN EXECUTE 'SET ROLE guvfx_schema_owner'; EXECUTE 'DROP SCHEMA audit CASCADE';
  EXCEPTION WHEN OTHERS THEN ok:=true; END;
  RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: schema_owner DROPPED the audit schema'; END IF;
  RAISE NOTICE 'PASS B4: migration identity cannot DROP the audit schema';
END $$;

-- B5: cannot forge / update / delete / truncate audit rows (no USAGE on the schema at all).
DO $$ DECLARE ok boolean:=false; BEGIN
  BEGIN EXECUTE 'SET ROLE guvfx_schema_owner'; EXECUTE 'INSERT INTO audit.evidence_audit(actor,action,obj) VALUES (''x'',''x'',''x'')';
  EXCEPTION WHEN OTHERS THEN ok:=true; END; RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: schema_owner forged an audit row'; END IF;
  ok:=false;
  BEGIN EXECUTE 'SET ROLE guvfx_schema_owner'; EXECUTE 'DELETE FROM audit.evidence_audit';
  EXCEPTION WHEN OTHERS THEN ok:=true; END; RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: schema_owner DELETED audit rows'; END IF;
  ok:=false;
  BEGIN EXECUTE 'SET ROLE guvfx_schema_owner'; EXECUTE 'TRUNCATE audit.evidence_audit';
  EXCEPTION WHEN OTHERS THEN ok:=true; END; RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: schema_owner TRUNCATED audit rows'; END IF;
  RAISE NOTICE 'PASS B5: migration identity cannot forge/update/delete/truncate audit rows';
END $$;

-- B6: app and erasure roles cannot even read the audit schema, nor call the writer directly.
DO $$ DECLARE ok boolean:=false; BEGIN
  BEGIN EXECUTE 'SET ROLE guvfx_app'; PERFORM 1 FROM audit.evidence_audit;
  EXCEPTION WHEN OTHERS THEN ok:=true; END; RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: guvfx_app read the audit table'; END IF;
  ok:=false;
  BEGIN EXECUTE 'SET ROLE guvfx_erasure_exec'; EXECUTE 'SELECT audit._write(''x'',''x'',''x'')';
  EXCEPTION WHEN OTHERS THEN ok:=true; END; RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: guvfx_erasure_exec called audit._write'; END IF;
  RAISE NOTICE 'PASS B6: app/erasure roles have no access to the audit schema or writer';
END $$;

-- B7: the trusted migration identity cannot suppress statement logging (log_statement is SUSET).
DO $$ DECLARE ok boolean:=false; BEGIN
  BEGIN EXECUTE 'SET ROLE guvfx_migrate'; EXECUTE 'SET log_statement=''none''';
  EXCEPTION WHEN OTHERS THEN ok:=true; END; RESET ROLE;
  IF NOT ok THEN RAISE EXCEPTION 'ASSERT FAILED: guvfx_migrate changed log_statement'; END IF;
  RAISE NOTICE 'PASS B7: migration identity cannot change log_statement (SUSET)';
END $$;

\if :{?allow_mutating}
\echo '== Section C -- positive / tamper controls (MUTATING; disposable clusters only) =='

-- C1: DELETE on evidence is captured with correct actor (login) + effective role (SET ROLE) + bounded detail.
DO $$
DECLARE before bigint; r record;
BEGIN
  SELECT count(*) INTO before FROM audit.evidence_audit;
  -- act as the trusted migration identity assuming the table owner, inside an approved window
  SET SESSION AUTHORIZATION guvfx_migrate;  -- sets session_user = guvfx_migrate (bootstrap/superuser only)
  SET ROLE guvfx_schema_owner;
  SET audit.window_id = 'W-TEST-C1';
  DELETE FROM broker_intelligence_evidenceblob WHERE id = (SELECT min(id) FROM broker_intelligence_evidenceblob);
  RESET ROLE; RESET SESSION AUTHORIZATION; RESET audit.window_id;
  SELECT * INTO r FROM audit.evidence_audit ORDER BY id DESC LIMIT 1;
  IF (SELECT count(*) FROM audit.evidence_audit) <> before+1 THEN RAISE EXCEPTION 'ASSERT FAILED: DELETE not audited'; END IF;
  IF r.actor <> 'guvfx_migrate' THEN RAISE EXCEPTION 'ASSERT FAILED: actor=% (expected guvfx_migrate)', r.actor; END IF;
  IF r.effective_role <> 'guvfx_schema_owner' THEN RAISE EXCEPTION 'ASSERT FAILED: effective_role=%', r.effective_role; END IF;
  IF r.action <> 'DELETE' OR r.detail NOT LIKE 'rows=%' THEN RAISE EXCEPTION 'ASSERT FAILED: action/detail=%/%', r.action, r.detail; END IF;
  IF r.window_id <> 'W-TEST-C1' THEN RAISE EXCEPTION 'ASSERT FAILED: window_id=%', r.window_id; END IF;
  IF r.txid IS NULL OR r.session_pid IS NULL THEN RAISE EXCEPTION 'ASSERT FAILED: txid/pid not captured'; END IF;
  RAISE NOTICE 'PASS C1: DELETE audited (actor=% eff=% win=% detail=%)', r.actor, r.effective_role, r.window_id, r.detail;
END $$;

-- C2: TRUNCATE on evidence is captured (single statement -> single row; no recursion).
DO $$
DECLARE before bigint; added int; r record;
BEGIN
  SELECT count(*) INTO before FROM audit.evidence_audit;
  SET ROLE guvfx_schema_owner; TRUNCATE broker_intelligence_brokerevent; RESET ROLE;
  added := (SELECT count(*) FROM audit.evidence_audit) - before;
  IF added <> 1 THEN RAISE EXCEPTION 'ASSERT FAILED: TRUNCATE produced % audit rows (expected 1 -> recursion?)', added; END IF;
  SELECT * INTO r FROM audit.evidence_audit ORDER BY id DESC LIMIT 1;
  IF r.action <> 'TRUNCATE' THEN RAISE EXCEPTION 'ASSERT FAILED: action=%', r.action; END IF;
  RAISE NOTICE 'PASS C2: TRUNCATE audited as a single row (no recursion)';
END $$;

-- C3: tamper -- disabling a DML trigger (owner-available) is itself captured by the ddl_command_end trigger.
DO $$
DECLARE before bigint; added int;
BEGIN
  SELECT count(*) INTO before FROM audit.evidence_audit;
  SET ROLE guvfx_schema_owner; ALTER TABLE broker_intelligence_evidenceblob DISABLE TRIGGER z_audit_del; RESET ROLE;
  added := (SELECT count(*) FROM audit.evidence_audit) - before;
  IF added < 1 THEN RAISE EXCEPTION 'ASSERT FAILED: trigger-disable tamper was NOT captured'; END IF;
  -- restore
  ALTER TABLE broker_intelligence_evidenceblob ENABLE TRIGGER z_audit_del;
  RAISE NOTICE 'PASS C3: trigger-disable tamper captured by ddl_command_end event trigger';
END $$;

-- C4: ordinary non-evidence DDL is NOT blocked and NOT audited (Django migrations unaffected).
DO $$
DECLARE before bigint; added int;
BEGIN
  SELECT count(*) INTO before FROM audit.evidence_audit;
  SET ROLE guvfx_schema_owner;
  CREATE TABLE public.unrelated_migration_probe (id int);
  INSERT INTO public.unrelated_migration_probe VALUES (1);
  ALTER TABLE public.unrelated_migration_probe ADD COLUMN v text;
  DROP TABLE public.unrelated_migration_probe;
  RESET ROLE;
  added := (SELECT count(*) FROM audit.evidence_audit) - before;
  IF added <> 0 THEN RAISE EXCEPTION 'ASSERT FAILED: unrelated DDL produced % audit rows', added; END IF;
  RAISE NOTICE 'PASS C4: unrelated DDL neither blocked nor audited';
END $$;

-- C5: RENAME-THEN-DROP of an evidence table is captured (OID-based; the attack that defeated name matching).
--     Both the ALTER (rename) and the DROP must leave a trail. (Destroys broker_intelligence_evidenceblob.)
DO $$
DECLARE before bigint; alt int; drp int;
BEGIN
  SELECT count(*) INTO before FROM audit.evidence_audit;
  SET ROLE guvfx_schema_owner;
  ALTER TABLE broker_intelligence_evidenceblob RENAME TO z_evblob;
  ALTER INDEX  broker_intelligence_evidenceblob_pkey   RENAME TO z_evpk;
  ALTER SEQUENCE broker_intelligence_evidenceblob_id_seq RENAME TO z_evseq;
  DROP TABLE z_evblob;
  RESET ROLE;
  alt := (SELECT count(*) FROM audit.evidence_audit WHERE id > before AND action='ALTER TABLE');
  drp := (SELECT count(*) FROM audit.evidence_audit WHERE id > before AND action='DROP');
  IF alt < 1 THEN RAISE EXCEPTION 'ASSERT FAILED: rename of evidence table NOT captured'; END IF;
  IF drp < 1 THEN RAISE EXCEPTION 'ASSERT FAILED: drop of renamed evidence table NOT captured'; END IF;
  RAISE NOTICE 'PASS C5: rename-then-drop captured (ALTER rows=%, DROP rows=%)', alt, drp;
END $$;

-- C6: DROP of a DML audit trigger on an evidence table is itself captured (via the pinned trigger OID),
--     so neutralising the DML audit before a silent DELETE cannot be done without a trace.
DO $$
DECLARE before bigint; added int;
BEGIN
  SELECT count(*) INTO before FROM audit.evidence_audit;
  SET ROLE guvfx_schema_owner; DROP TRIGGER z_audit_del2 ON broker_intelligence_brokerevent; RESET ROLE;
  added := (SELECT count(*) FROM audit.evidence_audit) - before;
  IF added < 1 THEN RAISE EXCEPTION 'ASSERT FAILED: DROP TRIGGER on evidence table NOT captured'; END IF;
  RAISE NOTICE 'PASS C6: dropping a DML audit trigger is captured (added=%)', added;
END $$;

-- C7: pollution/DoS control -- a lookalike-named object (name contains the evidence substring) created and
--     dropped by the owner is NOT audited, because detection keys on OID, not name.
DO $$
DECLARE before bigint; added int;
BEGIN
  SELECT count(*) INTO before FROM audit.evidence_audit;
  SET ROLE guvfx_schema_owner;
  CREATE TABLE public.broker_intelligence_evidenceblob_lookalike (id int);
  DROP TABLE public.broker_intelligence_evidenceblob_lookalike;
  RESET ROLE;
  added := (SELECT count(*) FROM audit.evidence_audit) - before;
  IF added <> 0 THEN RAISE EXCEPTION 'ASSERT FAILED: lookalike-named object produced % audit rows (pollution)', added; END IF;
  RAISE NOTICE 'PASS C7: lookalike-named object not audited (OID-based detection, no pollution)';
END $$;
\else
\echo '(Section C skipped -- pass -v allow_mutating=1 on a DISPOSABLE cluster to run positive/tamper controls)'
\endif

\echo '== ALL ASSERTIONS PASSED =='
