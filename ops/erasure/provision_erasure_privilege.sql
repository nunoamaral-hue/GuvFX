-- ADR-0054 — Privileged controlled-erasure backend provisioning.
-- ============================================================================================
-- STATUS: DRAFT — PRODUCTION PROHIBITED. SUPERUSER-ONLY. SEPARATELY AUTHORIZED. NOT a Django migration.
-- Nothing here deletes data. Apply ONLY to a DISPOSABLE PostgreSQL 15 REHEARSAL database first. Do not run
-- against production, and do not grant the ordinary application role any erasure authority.
--
-- The destructive SECURITY DEFINER procedure body (§5) and the durable outbox/worker/2-person workflow are
-- BUILT + TESTED SEPARATELY (see docs/WV1_ERASURE_REWORK_PACKET.md) and must pass review before this script is
-- completed with the final body. §5 below is a reviewed TEMPLATE, intentionally not yet runnable.
--
-- ROLE / TRUST-BOUNDARY MODEL (Findings 2 + 8) — SEPARATE identities, least privilege:
--   guvfx_schema_owner   NOLOGIN  owns the application tables + the ordinary (block_delete) trigger functions.
--   guvfx_migrate        LOGIN    member of guvfx_schema_owner; the ONLY role that runs schema migrations.
--   guvfx_app            LOGIN    web/worker RUNTIME. DML only (SELECT/INSERT/UPDATE); NO DELETE/TRUNCATE on the
--                                 evidence tables; NO ownership; NO membership in any privileged role; NO EXECUTE
--                                 on the destructive procedure.
--   guvfx_erasure        NOLOGIN  OWNS the destructive SECURITY DEFINER procedure; this is the identity the
--                                 BEFORE-DELETE triggers key on (current_user = 'guvfx_erasure'). Granted DELETE
--                                 on the evidence tables. Not a login; reachable only via the procedure.
--   guvfx_erasure_exec   LOGIN    the ONLY role granted EXECUTE on the destructive procedure. A SEPARATE credential
--                                 that MUST NOT be present in the web/worker runtime environment — only the
--                                 controlled erasure management command (run by governance) connects as it.
--   guvfx_readonly       LOGIN    operational/audit read-only (SELECT only).
-- Why this is safe (Finding 2): a compromised web process holds only guvfx_app, which cannot DELETE/TRUNCATE,
-- cannot DDL the triggers (not the owner), is not a member of any privileged role (cannot SET ROLE), and has NO
-- EXECUTE on the procedure. The procedure cannot be invoked from the web runtime at all. `current_user` becomes
-- 'guvfx_erasure' ONLY inside the SECURITY DEFINER procedure (owned by it), invoked ONLY by guvfx_erasure_exec.

-- 1) Roles (NOLOGIN privileged identities; LOGIN roles get credentials from the secret store, NEVER here).
CREATE ROLE guvfx_schema_owner NOLOGIN;
CREATE ROLE guvfx_erasure       NOLOGIN;
-- LOGIN roles: set passwords out-of-band (\password or ALTER ROLE ... PASSWORD from a secret store), never in SQL.
CREATE ROLE guvfx_migrate      LOGIN;    GRANT guvfx_schema_owner TO guvfx_migrate;
CREATE ROLE guvfx_erasure_exec LOGIN;
CREATE ROLE guvfx_readonly     LOGIN;
-- <APPLICATION_ROLE> is the EXISTING runtime role (e.g. guvfx). It must NOT be a member of any role above.

-- 2) Ownership: move the evidence tables + the (DELETE) trigger functions + sequences to the schema owner, so the
--    runtime role can neither TRUNCATE nor DDL them. (Only the two block_delete functions exist — the BEFORE-TRUNCATE
--    design was removed; do NOT reference bi_*_block_truncate, which do not exist.)
ALTER TABLE broker_intelligence_evidenceblob OWNER TO guvfx_schema_owner;
ALTER TABLE broker_intelligence_brokerevent  OWNER TO guvfx_schema_owner;
ALTER FUNCTION bi_evidenceblob_block_delete() OWNER TO guvfx_schema_owner;
ALTER FUNCTION bi_brokerevent_block_delete()  OWNER TO guvfx_schema_owner;
ALTER SEQUENCE broker_intelligence_evidenceblob_id_seq OWNER TO guvfx_schema_owner;
ALTER SEQUENCE broker_intelligence_brokerevent_id_seq  OWNER TO guvfx_schema_owner;

-- 3) Runtime role privileges: DML for ingestion, never DELETE/TRUNCATE on the evidence tables.
REVOKE DELETE, TRUNCATE ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent FROM <APPLICATION_ROLE>;
GRANT  SELECT, INSERT, UPDATE ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent TO <APPLICATION_ROLE>;
GRANT  USAGE, SELECT ON SEQUENCE broker_intelligence_evidenceblob_id_seq, broker_intelligence_brokerevent_id_seq
       TO <APPLICATION_ROLE>;

-- 4) Erasure identity privileges: guvfx_erasure may DELETE (the trigger also requires current_user = it) + write
--    the durable outbox. It is NOT a login and is reachable only through the procedure.
GRANT DELETE ON broker_intelligence_withdrawal, broker_intelligence_brokermessageobservation,
                broker_intelligence_brokerevent, broker_intelligence_evidenceblob,
                broker_intelligence_connectedmailbox, broker_intelligence_brokeremailidentity
      TO guvfx_erasure;
GRANT SELECT, INSERT, UPDATE ON broker_intelligence_deletiontask TO guvfx_erasure;
GRANT SELECT, UPDATE ON broker_intelligence_erasurerequest TO guvfx_erasure;

-- 5) The destructive SECURITY DEFINER procedure (TEMPLATE — final body built + tested separately; see packet).
--    It takes ONLY an erasure-request id + a non-reusable confirmation value; derives owner/plan/approvals/
--    separation-of-duties/hold state from DB rows (NEVER caller-supplied owner/requester/verifier/approver/caller);
--    recomputes + compares the full plan digest; performs the owner-scoped deletion; anonymises audit PII; writes
--    durable DeletionTask rows (files removed post-commit by the worker); records a non-PII result; and moves the
--    request to its terminal state — all in one transaction. Then:
--      ALTER FUNCTION bi_governed_erase_owner(bigint, bytea) OWNER TO guvfx_erasure;
--      SET search_path (pinned inside the function): SET search_path = pg_catalog, public;
--      REVOKE ALL ON FUNCTION bi_governed_erase_owner(bigint, bytea) FROM PUBLIC, <APPLICATION_ROLE>;
--      GRANT EXECUTE ON FUNCTION bi_governed_erase_owner(bigint, bytea) TO guvfx_erasure_exec;   -- executor ONLY

-- 6) Read-only operational role.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO guvfx_readonly;

-- 7) Invariants the readiness check (erasure.privileged_backend_ready) re-verifies before any execute:
--    SELECT NOT pg_has_role('<APPLICATION_ROLE>', 'guvfx_erasure',      'USAGE');   -- must be true (not a member)
--    SELECT NOT pg_has_role('<APPLICATION_ROLE>', 'guvfx_erasure_exec', 'USAGE');   -- must be true
--    SELECT NOT pg_has_role('<APPLICATION_ROLE>', 'guvfx_schema_owner', 'USAGE');   -- must be true
--    -- plus: proc exists with the expected signature, SECURITY DEFINER, pinned search_path, owned by guvfx_erasure,
--    --       EXECUTE granted to guvfx_erasure_exec only (no PUBLIC/app), tables owned by guvfx_schema_owner, app
--    --       DELETE/TRUNCATE revoked, and the expected version/checksum marker — any mismatch => NOT READY.
--
-- ROLLBACK (rehearsal only): reverse ownership (ALTER ... OWNER TO <APPLICATION_ROLE>), re-GRANT, DROP the
-- procedure, and DROP the created roles (REASSIGN OWNED / DROP OWNED first). Document the exact reverse in the
-- deploy runbook before any production consideration (which remains PROHIBITED here).
