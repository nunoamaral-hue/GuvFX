-- ADR-0054 / F-8 -- Evidence-destruction AUDIT CONTROLS (DARK; NOT applied to production here).
-- ============================================================================================================
-- Purpose: provide a verifiable, migrate-non-bypassable audit of TRUNCATE / DROP / DELETE affecting the two
-- immutable-evidence tables, as the pre-cutover audit control required by the interim TRUNCATE disposition when
-- pgAudit is unavailable (see docs/WV1_AUDIT_CONTROL_DESIGN_2026-10-23.md).
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
-- A tamper attempt that IS available to the owner (ALTER TABLE ... DISABLE TRIGGER on an evidence table) is
-- itself DDL and is captured by the ddl_command_end event trigger (which the migration identity cannot remove).
--
-- Run as the BOOTSTRAP superuser. Parameters:
--   -v bootstrap_role=<role>   (default resolved to CURRENT_USER)  -- owner of the audit schema/objects
-- Idempotent-ish: refuses (fail-closed) if a conflicting `audit` schema already exists that it did not create.
\set ON_ERROR_STOP on
\if :{?bootstrap_role}
\else
  \set bootstrap_role `echo "$PGUSER"`
\endif

DO $guard$
BEGIN
  IF to_regnamespace('audit') IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM pg_description d JOIN pg_namespace n ON n.oid=d.objoid
                     WHERE n.nspname='audit' AND d.description='guvfx-evidence-audit/1') THEN
    RAISE EXCEPTION 'refused (fail-closed): a schema "audit" already exists that this script did not create';
  END IF;
END $guard$;

CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION :"bootstrap_role";
COMMENT ON SCHEMA audit IS 'guvfx-evidence-audit/1';
REVOKE ALL ON SCHEMA audit FROM PUBLIC;

CREATE TABLE IF NOT EXISTS audit.evidence_audit (
  id         bigserial primary key,
  at         timestamptz NOT NULL DEFAULT now(),
  actor      text NOT NULL,           -- session_user (the real identity, preserved through SET ROLE)
  action     text NOT NULL,           -- DELETE | TRUNCATE | DROP | <DDL command tag>
  obj        text NOT NULL,
  detail     text
);
-- append-only: no UPDATE/DELETE grants to anyone; only the SECURITY DEFINER writer inserts.
REVOKE ALL ON audit.evidence_audit FROM PUBLIC;

CREATE OR REPLACE FUNCTION audit._write(a text, ac text, o text, d text)
  RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = audit, pg_temp AS $$
BEGIN INSERT INTO audit.evidence_audit(actor,action,obj,detail) VALUES (a,ac,o,d); END $$;
REVOKE ALL ON FUNCTION audit._write(text,text,text,text) FROM PUBLIC;

-- statement-level DELETE / TRUNCATE auditing on the two evidence tables
CREATE OR REPLACE FUNCTION audit.on_evidence_dml()
  RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = audit, pg_temp AS $$
DECLARE n bigint;
BEGIN
  IF TG_OP = 'DELETE' THEN
    SELECT count(*) INTO n FROM deleted;
    PERFORM audit._write(session_user, 'DELETE', TG_TABLE_NAME, 'rows='||n);
  ELSE
    PERFORM audit._write(session_user, TG_OP, TG_TABLE_NAME, 'statement');
  END IF;
  RETURN NULL;
END $$;

-- event triggers: DROP (sql_drop) + any DDL touching evidence (ddl_command_end) -> captures DROP and tamper
-- (e.g. ALTER TABLE ... DISABLE TRIGGER) by an owner who tries to defeat the DML triggers.
CREATE OR REPLACE FUNCTION audit.on_sql_drop()
  RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = audit, pg_temp AS $$
DECLARE o record;
BEGIN
  FOR o IN SELECT * FROM pg_event_trigger_dropped_objects() LOOP
    IF o.object_identity LIKE '%broker_intelligence_evidenceblob%'
       OR o.object_identity LIKE '%broker_intelligence_brokerevent%'
       OR o.schema_name = 'audit' THEN
      PERFORM audit._write(session_user, 'DROP', o.object_identity, o.object_type);
    END IF;
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION audit.on_ddl_end()
  RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = audit, pg_temp AS $$
DECLARE o record;
BEGIN
  FOR o IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    IF o.object_identity LIKE '%broker_intelligence_evidenceblob%'
       OR o.object_identity LIKE '%broker_intelligence_brokerevent%' THEN
      PERFORM audit._write(session_user, o.command_tag, o.object_identity, 'DDL/tamper-watch');
    END IF;
  END LOOP;
END $$;

-- DML triggers are created by the BOOTSTRAP role (superuser) so no audit-schema USAGE is granted to
-- guvfx_schema_owner. (The evidence tables are owned by guvfx_schema_owner; a superuser may still add triggers.)
DROP TRIGGER IF EXISTS z_audit_del   ON broker_intelligence_evidenceblob;
DROP TRIGGER IF EXISTS z_audit_trunc ON broker_intelligence_evidenceblob;
DROP TRIGGER IF EXISTS z_audit_del2  ON broker_intelligence_brokerevent;
DROP TRIGGER IF EXISTS z_audit_trunc2 ON broker_intelligence_brokerevent;
CREATE TRIGGER z_audit_del   AFTER DELETE   ON broker_intelligence_evidenceblob REFERENCING OLD TABLE AS deleted FOR EACH STATEMENT EXECUTE FUNCTION audit.on_evidence_dml();
CREATE TRIGGER z_audit_trunc AFTER TRUNCATE ON broker_intelligence_evidenceblob FOR EACH STATEMENT EXECUTE FUNCTION audit.on_evidence_dml();
CREATE TRIGGER z_audit_del2  AFTER DELETE   ON broker_intelligence_brokerevent  REFERENCING OLD TABLE AS deleted FOR EACH STATEMENT EXECUTE FUNCTION audit.on_evidence_dml();
CREATE TRIGGER z_audit_trunc2 AFTER TRUNCATE ON broker_intelligence_brokerevent FOR EACH STATEMENT EXECUTE FUNCTION audit.on_evidence_dml();

DROP EVENT TRIGGER IF EXISTS et_evidence_sql_drop;
DROP EVENT TRIGGER IF EXISTS et_evidence_ddl_end;
CREATE EVENT TRIGGER et_evidence_sql_drop ON sql_drop        EXECUTE FUNCTION audit.on_sql_drop();
CREATE EVENT TRIGGER et_evidence_ddl_end  ON ddl_command_end EXECUTE FUNCTION audit.on_ddl_end();

-- Rollback (bootstrap):
--   DROP EVENT TRIGGER et_evidence_sql_drop; DROP EVENT TRIGGER et_evidence_ddl_end;
--   DROP SCHEMA audit CASCADE;  -- (also removes the DML triggers' function; drop the triggers first if desired)
