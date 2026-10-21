-- ADR-0054 — Privileged controlled-erasure backend provisioning.
-- ============================================================================================
-- STATUS: DRAFT. SUPERUSER-ONLY. SEPARATELY AUTHORIZED. NOT a Django migration and NOT auto-run.
-- Apply ONLY in a rehearsal environment first, then (Sponsor-authorized) in production. Nothing in this
-- file deletes data. It establishes the database-enforced privilege boundary that makes governed erasure
-- possible; until it is applied, evidence/event deletion is IMPOSSIBLE for every ordinary path (the
-- migration-0017 triggers require current_user = 'guvfx_erasure', a role that does not yet exist).
--
-- THREAT MODEL (Finding 2 — why the ordinary application role cannot reproduce the bypass):
--   * The BEFORE-DELETE triggers on EvidenceBlob/BrokerEvent permit a delete ONLY when the deleting
--     session's current_user is 'guvfx_erasure'.
--   * The ordinary application role (e.g. 'guvfx') is NOT granted membership in 'guvfx_erasure' and is not a
--     superuser, so it CANNOT `SET ROLE guvfx_erasure`. A custom GUC is irrelevant — the triggers ignore it.
--   * The ONLY way current_user becomes 'guvfx_erasure' is executing the SECURITY DEFINER procedure below,
--     which is OWNED BY 'guvfx_erasure' (so it runs as that role regardless of caller) and whose body is
--     fixed, owner-scoped, ref-checked, fully-qualified, with a pinned search_path — the caller cannot make
--     it target an arbitrary owner/table.
--   * Therefore arbitrary SQL / `SET` / raw ORM on the ordinary role can delete nothing; only the governed
--     procedure can, and only for the owner it is asked to erase, recorded against the caller + request id.
-- ============================================================================================

-- 1) Dedicated, NON-LOGIN privileged role. No password; cannot be connected to directly.
CREATE ROLE guvfx_erasure NOLOGIN;

-- 2) Least-privilege grants: the privileged role may DELETE the owner-scoped tables (the trigger ALSO
--    requires current_user = it) and INSERT durable file-deletion tasks. Nothing else.
GRANT DELETE ON
    broker_intelligence_withdrawal,
    broker_intelligence_brokermessageobservation,
    broker_intelligence_brokerevent,
    broker_intelligence_evidenceblob,
    broker_intelligence_connectedmailbox,
    broker_intelligence_brokeremailidentity
  TO guvfx_erasure;
-- GRANT INSERT ON broker_intelligence_deletiontask TO guvfx_erasure;   -- (after the outbox table ships)

-- 2b) CLOSE THE OWNER-LEVEL TRUNCATE/DDL VECTORS (adversarial-review HIGH). The migration-only state blocks all
--     ordinary DML DELETE, but the application role still OWNS the two evidence tables + the trigger functions, so
--     a FULLY-COMPROMISED app role could TRUNCATE, DISABLE/DROP the triggers, or CREATE OR REPLACE the functions.
--     Moving OWNERSHIP off the app role removes that power; the app role keeps only the DML it needs for ingestion
--     (SELECT/INSERT/UPDATE), never DELETE/TRUNCATE. Replace <APPLICATION_ROLE> with the real app role.
ALTER TABLE broker_intelligence_evidenceblob OWNER TO guvfx_erasure;
ALTER TABLE broker_intelligence_brokerevent  OWNER TO guvfx_erasure;
ALTER FUNCTION bi_evidenceblob_block_delete()   OWNER TO guvfx_erasure;
ALTER FUNCTION bi_brokerevent_block_delete()    OWNER TO guvfx_erasure;
ALTER FUNCTION bi_evidenceblob_block_truncate() OWNER TO guvfx_erasure;
ALTER FUNCTION bi_brokerevent_block_truncate()  OWNER TO guvfx_erasure;
REVOKE DELETE, TRUNCATE ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent FROM <APPLICATION_ROLE>;
GRANT SELECT, INSERT, UPDATE ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent TO <APPLICATION_ROLE>;
GRANT USAGE, SELECT ON SEQUENCE broker_intelligence_evidenceblob_id_seq, broker_intelligence_brokerevent_id_seq
      TO <APPLICATION_ROLE>;
-- OPERATIONAL CAVEAT: after ownership moves, future Django migrations that ALTER these two tables must run as a
-- role that owns them (guvfx_erasure or a superuser), not the ordinary app role. Factor this into the deploy runbook.

-- 3) The governed SECURITY DEFINER procedure (findings 3/4/5/8). OWNED BY guvfx_erasure; the application
--    role is granted EXECUTE only. Body is authored with the durable deletion-outbox increment and must be
--    reviewed + rehearsed before use. Outline (DO NOT run until the outbox table + body are finalized):
--
--    CREATE FUNCTION bi_governed_erase_owner(p_owner_id bigint, p_request_id bigint, p_caller text)
--      RETURNS jsonb
--      LANGUAGE plpgsql
--      SECURITY DEFINER
--      SET search_path = pg_catalog, public
--    AS $fn$
--    DECLARE ... BEGIN
--      -- owner-scoped deletes (fully qualified): withdrawals, observations, events;
--      -- candidate blobs = UNION of owner event + observation evidence refs (finding 5);
--      -- delete a blob row ONLY when no event/observation reference remains; INSERT a DeletionTask
--      --   (EVIDENCE_FILE, storage_key, PENDING) for each (finding 3/4 — files removed post-commit by the worker);
--      -- INSERT DeletionTask (CREDENTIAL_FILE, credential_ref, PENDING) per mailbox, then delete mailboxes;
--      -- ANONYMISE the subject's audit rows (email/subject_user/ip/user_agent/identifying JSON) — finding 8;
--      -- record p_caller + p_request_id; RETURN counts (no content).
--    END; $fn$;
--    ALTER FUNCTION bi_governed_erase_owner(bigint, bigint, text) OWNER TO guvfx_erasure;
--    REVOKE ALL ON FUNCTION bi_governed_erase_owner(bigint, bigint, text) FROM PUBLIC;
--    GRANT EXECUTE ON FUNCTION bi_governed_erase_owner(bigint, bigint, text) TO <APPLICATION_ROLE>;
--
-- 4) Confirm the ordinary application role is NOT a member of guvfx_erasure (it must not be):
--    SELECT pg_has_role('<APPLICATION_ROLE>', 'guvfx_erasure', 'MEMBER');  -- must be false
