-- ADR-0054 / F-8 -- Evidence-destruction AUDIT CONTROLS (DARK; NOT applied to production here).
-- ============================================================================================================
-- Purpose: provide a verifiable, migrate-non-bypassable audit of TRUNCATE / DROP / DELETE affecting the two
-- immutable-evidence tables, as the pre-cutover audit control required by the interim TRUNCATE disposition when
-- pgAudit is unavailable (see docs/WV1_AUDIT_CONTROL_DESIGN_2026-10-23.md). Self-checking test harness:
-- ops/erasure/audit_evidence_controls_test.sql.
--
-- CAPACITY-SAFE BY DESIGN: this is an ALWAYS-ON, BOUNDED layer (a handful of audit rows per real destructive
-- event) with ZERO cluster-wide log volume -- it does NOT depend on log_statement. `log_statement='mod'` is a
-- SEPARATE, window-scoped bounded-canary defence-in-depth (enabled only during a migration window, with rotation
-- + off-host shipping + a free-space auto-stop) and is NOT part of this file.
--
-- KEY ISOLATION PROPERTY: everything lives in a dedicated schema `audit` OWNED BY THE BOOTSTRAP ROLE, not by
-- guvfx_schema_owner. The trusted migration identity (guvfx_migrate, a member of guvfx_schema_owner which owns
-- schema public) therefore CANNOT drop, disable, or modify any of it -- schema ownership of `public` does not
-- extend to another schema. The audit-writer is SECURITY DEFINER so writes always land regardless of the actor.
-- A tamper attempt that IS available to the evidence-table owner (ALTER TABLE ... DISABLE TRIGGER) is itself DDL
-- and is captured by the ddl_command_end event trigger (which the migration identity cannot remove).
--
-- HARDENING (reviewed 2026-10-23, F1/F2/F3):
--   * every privileged function is SECURITY DEFINER with a FIXED EMPTY search_path ('') and all non-catalog
--     objects fully schema-qualified -> the body cannot be redirected through an attacker-created object;
--   * PUBLIC has no EXECUTE on any function; no role other than the bootstrap owner is granted EXECUTE;
--   * NO dynamic SQL anywhere (no EXECUTE/format) -> no identifier-injection surface;
--   * actor attribution records session_user (authenticated login, NOT changed by SET ROLE or SECURITY DEFINER)
--     AND the effective role via current_setting('role') AND txid/backend-pid AND an optional approved-window id;
--   * the audit row carries BOUNDED METADATA ONLY -- never row contents, credentials, verifier values, tokens,
--     or arbitrary SQL text;
--   * the whole install runs in ONE transaction -> a partial failure rolls back atomically (nothing persists).
--
-- Run as the BOOTSTRAP superuser. Parameters:
--   -v bootstrap_role=<role>   (default resolved to $PGUSER)  -- owner of the audit schema/objects
-- Idempotent: safe to re-run; refuses (fail-closed) if a conflicting `audit` schema already exists that this
-- script did not create (version marker guvfx-evidence-audit/2).
\set ON_ERROR_STOP on
\if :{?bootstrap_role}
\else
  \set bootstrap_role `echo "$PGUSER"`
\endif

BEGIN;

-- fail-closed: do not clobber a pre-existing, differently-owned or differently-versioned `audit` schema.
DO $guard$
BEGIN
  IF to_regnamespace('audit') IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM pg_description d JOIN pg_namespace n ON n.oid=d.objoid
                     WHERE n.nspname='audit' AND d.description='guvfx-evidence-audit/2') THEN
    RAISE EXCEPTION 'refused (fail-closed): a schema "audit" already exists that this script did not create';
  END IF;
END $guard$;

CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION :"bootstrap_role";
COMMENT ON SCHEMA audit IS 'guvfx-evidence-audit/2';
REVOKE ALL ON SCHEMA audit FROM PUBLIC;

-- Bounded-metadata, append-only audit record. NO row contents / credentials / verifiers / tokens / SQL text.
CREATE TABLE IF NOT EXISTS audit.evidence_audit (
  id              bigserial PRIMARY KEY,
  at              timestamptz NOT NULL DEFAULT now(),  -- timestamp
  actor           text        NOT NULL,                -- session_user: authenticated login (non-forgeable)
  effective_role  text,                                -- current_setting('role'): effective role via SET ROLE
  action          text        NOT NULL,                -- operation class: DELETE | TRUNCATE | DROP | <DDL tag>
  obj             text        NOT NULL,                -- protected object identity
  txid            bigint,                              -- transaction id
  session_pid     integer,                             -- backend pid (session identifier)
  window_id       text,                                -- approved maintenance-window id, where supplied (else NULL)
  detail          text                                 -- bounded: 'rows=<n>' | 'statement' | object type/tag
);
-- append-only: no UPDATE/DELETE/TRUNCATE grants to anyone; only the SECURITY DEFINER writer inserts.
REVOKE ALL ON audit.evidence_audit FROM PUBLIC;
REVOKE ALL ON SEQUENCE audit.evidence_audit_id_seq FROM PUBLIC;

-- Stable-OID registry of the protected objects (the two evidence tables AND the four DML triggers). Detection
-- keys on these OIDs, NOT on object names -- an OID survives ALTER ... RENAME and is still reported at DROP
-- time, so a rename-then-drop (or rename-then-drop-trigger) by the evidence-table owner cannot slip past the
-- event triggers, and a lookalike-named object created by any role has a different OID and is ignored.
-- Bootstrap-owned, re-synced on every (idempotent) install; unreachable by any non-bootstrap role.
CREATE TABLE IF NOT EXISTS audit.protected_objid (
  objid oid PRIMARY KEY,
  note  text NOT NULL   -- 'evidence-table' | 'audit-dml-trigger'
);
REVOKE ALL ON audit.protected_objid FROM PUBLIC;

-- Single writer. Captures actor/effective-role/txn/session/window itself so callers pass only class/obj/detail.
-- session_user and current_setting('role') are SESSION properties, unchanged by nested SECURITY DEFINER entry,
-- so they reflect the real caller even though this function executes as its bootstrap owner.
CREATE OR REPLACE FUNCTION audit._write(ac text, o text, d text)
  RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
  INSERT INTO audit.evidence_audit(actor, effective_role, action, obj, txid, session_pid, window_id, detail)
  VALUES (session_user,
          -- effective role via SET ROLE; when none was set the GUC reads 'none' -> record the login instead
          CASE WHEN current_setting('role', true) IN ('', 'none') THEN session_user
               ELSE current_setting('role', true) END,
          ac, o,
          txid_current(),
          pg_backend_pid(),
          current_setting('audit.window_id', true),
          d);
END $$;
REVOKE ALL ON FUNCTION audit._write(text, text, text) FROM PUBLIC;

-- statement-level DELETE / TRUNCATE auditing on the two evidence tables
CREATE OR REPLACE FUNCTION audit.on_evidence_dml()
  RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n bigint;
BEGIN
  IF TG_OP = 'DELETE' THEN
    SELECT count(*) INTO n FROM deleted;                 -- transition table: row COUNT only, never contents
    PERFORM audit._write('DELETE', TG_TABLE_NAME, 'rows='||n);
  ELSE
    PERFORM audit._write(TG_OP, TG_TABLE_NAME, 'statement');
  END IF;
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION audit.on_evidence_dml() FROM PUBLIC;

-- event triggers: DROP (sql_drop) + any DDL touching evidence (ddl_command_end) -> captures DROP and tamper
-- (e.g. ALTER TABLE ... DISABLE TRIGGER) by an owner who tries to defeat the DML triggers.
CREATE OR REPLACE FUNCTION audit.on_sql_drop()
  RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE o record;
BEGIN
  FOR o IN SELECT * FROM pg_event_trigger_dropped_objects() LOOP
    -- match on STABLE OID (survives RENAME, still reported at drop time), never on the mutable name.
    IF o.objid IN (SELECT objid FROM audit.protected_objid)
       OR o.schema_name = 'audit' THEN
      PERFORM audit._write('DROP', o.object_identity, o.object_type);
    END IF;
  END LOOP;
END $$;
REVOKE ALL ON FUNCTION audit.on_sql_drop() FROM PUBLIC;

CREATE OR REPLACE FUNCTION audit.on_ddl_end()
  RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE o record;
BEGIN
  FOR o IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    -- match on STABLE OID so ALTER ... RENAME of a protected table/trigger is itself recorded (tamper-evident).
    IF o.objid IN (SELECT objid FROM audit.protected_objid) THEN
      PERFORM audit._write(o.command_tag, o.object_identity, 'DDL/tamper-watch');
    END IF;
  END LOOP;
END $$;
REVOKE ALL ON FUNCTION audit.on_ddl_end() FROM PUBLIC;

-- DML triggers are created by the BOOTSTRAP role (superuser) so no audit-schema USAGE is granted to
-- guvfx_schema_owner. (The evidence tables are owned by guvfx_schema_owner; a superuser may still add triggers.)
DROP TRIGGER IF EXISTS z_audit_del    ON broker_intelligence_evidenceblob;
DROP TRIGGER IF EXISTS z_audit_trunc  ON broker_intelligence_evidenceblob;
DROP TRIGGER IF EXISTS z_audit_del2   ON broker_intelligence_brokerevent;
DROP TRIGGER IF EXISTS z_audit_trunc2 ON broker_intelligence_brokerevent;
CREATE TRIGGER z_audit_del    AFTER DELETE   ON broker_intelligence_evidenceblob REFERENCING OLD TABLE AS deleted FOR EACH STATEMENT EXECUTE FUNCTION audit.on_evidence_dml();
CREATE TRIGGER z_audit_trunc  AFTER TRUNCATE ON broker_intelligence_evidenceblob FOR EACH STATEMENT EXECUTE FUNCTION audit.on_evidence_dml();
CREATE TRIGGER z_audit_del2   AFTER DELETE   ON broker_intelligence_brokerevent  REFERENCING OLD TABLE AS deleted FOR EACH STATEMENT EXECUTE FUNCTION audit.on_evidence_dml();
CREATE TRIGGER z_audit_trunc2 AFTER TRUNCATE ON broker_intelligence_brokerevent  FOR EACH STATEMENT EXECUTE FUNCTION audit.on_evidence_dml();

-- (re)populate the stable-OID registry now that both tables and all four DML triggers exist. Re-synced each
-- install (DELETE+INSERT) so reinstall picks up the current OIDs. to_regclass() yields NULL for a missing table,
-- which simply fails the IN-match (no NULL row is inserted).
DELETE FROM audit.protected_objid;
INSERT INTO audit.protected_objid(objid, note)
  SELECT c.oid, 'evidence-table'
    FROM pg_class c
   WHERE c.oid IN (to_regclass('public.broker_intelligence_evidenceblob'),
                   to_regclass('public.broker_intelligence_brokerevent'))
  UNION ALL
  SELECT t.oid, 'audit-dml-trigger'
    FROM pg_trigger t
   WHERE t.tgname IN ('z_audit_del','z_audit_trunc','z_audit_del2','z_audit_trunc2')
     AND t.tgrelid IN (to_regclass('public.broker_intelligence_evidenceblob'),
                       to_regclass('public.broker_intelligence_brokerevent'));

DROP EVENT TRIGGER IF EXISTS et_evidence_sql_drop;
DROP EVENT TRIGGER IF EXISTS et_evidence_ddl_end;
CREATE EVENT TRIGGER et_evidence_sql_drop ON sql_drop        EXECUTE FUNCTION audit.on_sql_drop();
CREATE EVENT TRIGGER et_evidence_ddl_end  ON ddl_command_end EXECUTE FUNCTION audit.on_ddl_end();

COMMIT;

-- Rollback (bootstrap) -- drop EVENT TRIGGERS FIRST so no DDL can transiently fail while the audit schema is
-- being removed, then the schema (which cascades to the writer + trigger functions + DML triggers' dependency):
--   BEGIN;
--   DROP EVENT TRIGGER IF EXISTS et_evidence_sql_drop;
--   DROP EVENT TRIGGER IF EXISTS et_evidence_ddl_end;
--   DROP TRIGGER IF EXISTS z_audit_del    ON broker_intelligence_evidenceblob;
--   DROP TRIGGER IF EXISTS z_audit_trunc  ON broker_intelligence_evidenceblob;
--   DROP TRIGGER IF EXISTS z_audit_del2   ON broker_intelligence_brokerevent;
--   DROP TRIGGER IF EXISTS z_audit_trunc2 ON broker_intelligence_brokerevent;
--   DROP SCHEMA audit CASCADE;
--   COMMIT;
