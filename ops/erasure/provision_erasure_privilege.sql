-- ADR-0054 — Privileged controlled-erasure backend provisioning (RUNNABLE).
-- ============================================================================================================
-- SUPERUSER-ONLY. SEPARATELY AUTHORISED. NOT a Django migration. Run ONLY against a database you have been
-- explicitly authorised to provision. PRODUCTION use requires separate Sponsor authorisation for that step.
--
-- Safety: the script refuses to run unless ALL of these psql variables are passed, and it verifies the target
-- database name, so it cannot be applied to the wrong database by accident:
--   -v target_db=<exact current database name>         (verified against current_database())
--   -v allow_destructive_provisioning=1                 (explicit acknowledgement)
--   -v synthetic_confirmed=1                             (operator confirms synthetic/authorised data)
--   -v app_role=<application runtime role, e.g. guvfx_app>
--   -v erasure_exec_pw=<disposable executor password>  (NOT echoed; keep out of shell history in prod)
--   -v migrate_pw=<password>  -v readonly_pw=<password>
-- Example (rehearsal):
--   psql "$DSN" -v ON_ERROR_STOP=1 -v target_db=guvfx_rehearsal -v allow_destructive_provisioning=1 \
--        -v synthetic_confirmed=1 -v app_role=guvfx_app -v erasure_exec_pw=... -v migrate_pw=... -v readonly_pw=... \
--        -f ops/erasure/provision_erasure_privilege.sql
--
-- Idempotent: re-running is deterministic (roles created only if absent; ownership/grants/function are
-- CREATE-OR-REPLACE / idempotent). Transactional: the whole script runs in one transaction (ON_ERROR_STOP + the
-- single BEGIN/COMMIT), so a failure rolls everything back. Verify with verify_erasure_privilege.sql; reverse
-- with rollback_erasure_privilege.sql.
--
-- ROLE / TRUST-BOUNDARY MODEL (Findings 2 + 8):
--   guvfx_schema_owner  NOLOGIN  owns the evidence tables + their block_delete trigger functions + sequences.
--   guvfx_migrate       LOGIN    member of guvfx_schema_owner; runs schema migrations.
--   <app_role>          LOGIN    runtime: DML only on evidence tables (no DELETE/TRUNCATE), no ownership, no
--                                membership in any privileged role, NO EXECUTE on the procedure.
--   guvfx_erasure       NOLOGIN  owns the SECURITY DEFINER procedure; the identity the triggers key on.
--   guvfx_erasure_exec  NOLOGIN  GROUP holding the ONLY EXECUTE grant on the procedure; short-lived per-operator
--                                LOGIN roles join it for one operation (credential off the web runtime). The
--                                procedure authenticates the human operator via session_user + a single-use grant.
--   guvfx_readonly      LOGIN    operational/audit read-only.

\set ON_ERROR_STOP on

-- ---- Guards (fail before any change) -----------------------------------------------------------------------
\if :{?target_db}
\else
  \echo '*** REFUSED: pass -v target_db=<current database name> (safety guard). Nothing was changed.'
  \quit
\endif
\if :{?allow_destructive_provisioning}
\else
  \echo '*** REFUSED: pass -v allow_destructive_provisioning=1 to acknowledge. Nothing was changed.'
  \quit
\endif
\if :{?synthetic_confirmed}
\else
  \echo '*** REFUSED: pass -v synthetic_confirmed=1 to confirm the data is synthetic/authorised. Nothing changed.'
  \quit
\endif
\if :{?app_role}
\else
  \echo '*** REFUSED: pass -v app_role=<application runtime role>. Nothing was changed.'
  \quit
\endif

BEGIN;

-- DB-name guard (interpolated OUTSIDE any dollar-quoted block — psql does not substitute :vars inside $tag$...$tag$).
SELECT CASE WHEN current_database() = :'target_db' THEN 'true' ELSE 'false' END AS _dbok \gset
\if :_dbok
\else
  \echo '*** REFUSED: current_database() does not match -v target_db. Rolling back.'
  ROLLBACK;
  \quit
\endif

-- Precondition (P0 -- production superuser blocker): the application runtime role MUST already be least
-- privilege. If it carries ANY elevated cluster attribute (SUPERUSER / CREATEROLE / CREATEDB / BYPASSRLS), a
-- session on it would bypass the REVOKEs AND the BEFORE-DELETE immutability triggers, so this provisioning
-- would install a COSMETIC boundary. Refuse BEFORE any ownership change or procedure creation. Demotion to the
-- least-privilege posture is a SEPARATE, explicit production operation with its own rollback -- this script
-- NEVER demotes a role automatically.
SELECT CASE WHEN EXISTS (
  SELECT 1 FROM pg_roles
  WHERE rolname = :'app_role' AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls)
) THEN 'true' ELSE 'false' END AS _approlepriv \gset
\if :_approlepriv
  \echo '*** REFUSED: application role is privileged (SUPERUSER/CREATEROLE/CREATEDB/BYPASSRLS). Demote it to the'
  \echo '    least-privilege runtime posture FIRST (a separate, explicit governed operation with its own'
  \echo '    rollback). Provisioning a privileged app role would be a cosmetic boundary. Nothing changed; rolling back.'
  ROLLBACK;
  \quit
\endif

-- pgcrypto: the procedure recomputes credential-ref digests (sha256) to detect plan drift.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---- 1) Roles (idempotent) ---------------------------------------------------------------------------------
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_schema_owner') THEN
    CREATE ROLE guvfx_schema_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_erasure') THEN
    CREATE ROLE guvfx_erasure NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_migrate') THEN
    CREATE ROLE guvfx_migrate LOGIN; END IF;
  -- guvfx_erasure_exec is a NOLOGIN GROUP (P0 — no single shared executor login). Each authorised operator gets
  -- a distinct short-lived LOGIN role that is a MEMBER of this group (created by the independent grant process,
  -- NOT here), inheriting EXECUTE on the procedure. The procedure resolves the human operator from session_user
  -- via ErasureExecutionGrant — the executor identity is never a caller argument.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_erasure_exec') THEN
    CREATE ROLE guvfx_erasure_exec NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_readonly') THEN
    CREATE ROLE guvfx_readonly LOGIN; END IF;
END
$roles$;

-- LOGIN credentials from psql vars (never literals in this file). guvfx_erasure_exec is a NOLOGIN group (no pw).
\if :{?migrate_pw}
  ALTER ROLE guvfx_migrate LOGIN PASSWORD :'migrate_pw';
\endif
\if :{?readonly_pw}
  ALTER ROLE guvfx_readonly LOGIN PASSWORD :'readonly_pw';
\endif

GRANT guvfx_schema_owner TO guvfx_migrate;
-- The application role must NOT be a member of any privileged role.
REVOKE guvfx_schema_owner, guvfx_erasure, guvfx_erasure_exec FROM :"app_role";

-- ---- 2) Ownership: evidence tables + their block_delete trigger functions + sequences -> schema owner ------
ALTER TABLE broker_intelligence_evidenceblob OWNER TO guvfx_schema_owner;
ALTER TABLE broker_intelligence_brokerevent  OWNER TO guvfx_schema_owner;
ALTER FUNCTION bi_evidenceblob_block_delete() OWNER TO guvfx_schema_owner;
ALTER FUNCTION bi_brokerevent_block_delete()  OWNER TO guvfx_schema_owner;
ALTER SEQUENCE broker_intelligence_evidenceblob_id_seq OWNER TO guvfx_schema_owner;
ALTER SEQUENCE broker_intelligence_brokerevent_id_seq  OWNER TO guvfx_schema_owner;

-- Adversarial-review HIGH (round 4): a non-superuser that OWNS schema public can `DROP SCHEMA public CASCADE`,
-- destroying the evidence tables regardless of per-table ownership. Move schema ownership off the application
-- role and revoke CREATE, closing that owner-level destruction vector. Migrations run as guvfx_migrate (a member
-- of guvfx_schema_owner) so they can still create objects; the runtime app role keeps USAGE (granted to PUBLIC
-- by default) but can no longer create or drop the schema.
ALTER SCHEMA public OWNER TO guvfx_schema_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM :"app_role";

-- ---- 3) Application runtime role: DML only on the evidence tables; never DELETE/TRUNCATE -------------------
REVOKE DELETE, TRUNCATE ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent FROM :"app_role";
GRANT  SELECT, INSERT, UPDATE ON broker_intelligence_evidenceblob, broker_intelligence_brokerevent TO :"app_role";
GRANT  USAGE, SELECT ON SEQUENCE broker_intelligence_evidenceblob_id_seq, broker_intelligence_brokerevent_id_seq
       TO :"app_role";

-- ---- 4) Erasure identity: the privileges the procedure needs (it runs AS guvfx_erasure) -------------------
GRANT SELECT, DELETE ON broker_intelligence_withdrawal, broker_intelligence_brokermessageobservation,
                        broker_intelligence_brokerevent, broker_intelligence_evidenceblob,
                        broker_intelligence_connectedmailbox, broker_intelligence_brokeremailidentity,
                        broker_intelligence_tradingaccountbrokercontact
      TO guvfx_erasure;
GRANT SELECT, UPDATE ON broker_intelligence_erasurerequest TO guvfx_erasure;
GRANT SELECT ON broker_intelligence_erasurehold TO guvfx_erasure;
GRANT SELECT, INSERT ON broker_intelligence_deletiontask TO guvfx_erasure;
GRANT USAGE, SELECT ON SEQUENCE broker_intelligence_deletiontask_id_seq TO guvfx_erasure;
-- Audit anonymisation (UPDATE only; rows are preserved):
GRANT SELECT, UPDATE ON broker_intelligence_brokeremailidentityaudit,
                        broker_intelligence_tradingaccountbrokercontactaudit,
                        core_auditevent TO guvfx_erasure;
-- The procedure reads + CONSUMES execution grants (sets consumed_at). It never INSERTs them.
GRANT SELECT, UPDATE ON broker_intelligence_erasureexecutiongrant TO guvfx_erasure;

-- ---- 4b) Execution-grant table: OFF the application role (P0 — a forged grant would defeat non-forgeability).
-- Owned by the schema owner; the app role gets NOTHING; the independent grant-admin process (a privileged role,
-- never the app role or an executor) INSERTs rows out of band. The procedure (guvfx_erasure) SELECT/UPDATEs it.
ALTER TABLE broker_intelligence_erasureexecutiongrant OWNER TO guvfx_schema_owner;
ALTER SEQUENCE broker_intelligence_erasureexecutiongrant_id_seq OWNER TO guvfx_schema_owner;
REVOKE ALL ON broker_intelligence_erasureexecutiongrant FROM PUBLIC;
REVOKE ALL ON broker_intelligence_erasureexecutiongrant FROM :"app_role";

-- The executor connection runs the readiness probe, which reads django_migrations (a regular, non-PII table).
-- Members of the guvfx_erasure_exec group inherit this SELECT.
GRANT SELECT ON django_migrations TO guvfx_erasure_exec;

-- ---- 5) Read-only operational role -------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA public TO guvfx_readonly;

-- ---- 6) Helper: live plan facts (read-only; same shape as the Python dry-run plan, minus token) -----------
CREATE OR REPLACE FUNCTION public.bi_erasure_plan_facts(p_owner bigint)
RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $facts$
WITH ev AS (SELECT id, evidence_id FROM broker_intelligence_brokerevent WHERE owner_id = p_owner),
     ob AS (SELECT id, evidence_id FROM broker_intelligence_brokermessageobservation WHERE owner_id = p_owner),
     mbx AS (SELECT id, credential_ref FROM broker_intelligence_connectedmailbox WHERE user_id = p_owner),
     cand AS (SELECT evidence_id AS bid FROM ev WHERE evidence_id IS NOT NULL
              UNION SELECT evidence_id FROM ob WHERE evidence_id IS NOT NULL),
     classified AS (
       -- NULL-owner references count as "other" (retain), matching the Python plan (owner IS DISTINCT FROM).
       SELECT c.bid,
         ((SELECT count(*) FROM broker_intelligence_brokerevent e
             WHERE e.evidence_id = c.bid AND e.owner_id IS DISTINCT FROM p_owner)
        + (SELECT count(*) FROM broker_intelligence_brokermessageobservation o
             WHERE o.evidence_id = c.bid AND o.owner_id IS DISTINCT FROM p_owner)) AS other_refs
       FROM cand c)
SELECT jsonb_build_object(
  'subject_user_id', p_owner,
  'withdrawals', (SELECT coalesce(jsonb_agg(id ORDER BY id), '[]'::jsonb)
                  FROM broker_intelligence_withdrawal WHERE owner_id = p_owner),
  'broker_events', (SELECT coalesce(jsonb_agg(id ORDER BY id), '[]'::jsonb) FROM ev),
  'observations', (SELECT coalesce(jsonb_agg(id ORDER BY id), '[]'::jsonb) FROM ob),
  'connected_mailboxes', (SELECT coalesce(jsonb_agg(id ORDER BY id), '[]'::jsonb) FROM mbx),
  'identities', (SELECT coalesce(jsonb_agg(id ORDER BY id), '[]'::jsonb)
                 FROM broker_intelligence_brokeremailidentity WHERE user_id = p_owner),
  'contacts', (SELECT coalesce(jsonb_agg(id ORDER BY id), '[]'::jsonb)
               FROM broker_intelligence_tradingaccountbrokercontact
               WHERE broker_email_identity_id IN
                     (SELECT id FROM broker_intelligence_brokeremailidentity WHERE user_id = p_owner)),
  'evidence_blobs_delete', (SELECT coalesce(jsonb_agg(bid ORDER BY bid), '[]'::jsonb)
                            FROM classified WHERE other_refs = 0),
  'evidence_blobs_retain_shared', (SELECT coalesce(jsonb_agg(bid ORDER BY bid), '[]'::jsonb)
                                   FROM classified WHERE other_refs > 0),
  'credential_ref_digests', (SELECT coalesce(jsonb_agg(d ORDER BY d), '[]'::jsonb) FROM (
       SELECT encode(public.digest(credential_ref, 'sha256'), 'hex') AS d FROM mbx
       WHERE coalesce(credential_ref, '') <> '') s)
);
$facts$;
ALTER FUNCTION public.bi_erasure_plan_facts(bigint) OWNER TO guvfx_erasure;
REVOKE ALL ON FUNCTION public.bi_erasure_plan_facts(bigint) FROM PUBLIC;

-- ---- 7) Helper: order-independent plan-fact comparison (TOCTOU guard) -------------------------------------
CREATE OR REPLACE FUNCTION public.bi_erasure_plan_matches(p_stored jsonb, p_live jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public AS $match$
  SELECT (p_stored->>'subject_user_id') IS NOT DISTINCT FROM (p_live->>'subject_user_id')
     AND NOT EXISTS (
       SELECT 1 FROM (VALUES ('withdrawals'),('broker_events'),('observations'),('connected_mailboxes'),
                             ('identities'),('contacts'),('evidence_blobs_delete'),('evidence_blobs_retain_shared'),
                             ('credential_ref_digests')) AS k(key)
       WHERE (SELECT coalesce(array_agg(e ORDER BY e), ARRAY[]::text[])
                FROM jsonb_array_elements_text(coalesce(p_stored->k.key, '[]'::jsonb)) e)
          IS DISTINCT FROM
             (SELECT coalesce(array_agg(e ORDER BY e), ARRAY[]::text[])
                FROM jsonb_array_elements_text(coalesce(p_live->k.key, '[]'::jsonb)) e));
$match$;
ALTER FUNCTION public.bi_erasure_plan_matches(jsonb, jsonb) OWNER TO guvfx_erasure;
REVOKE ALL ON FUNCTION public.bi_erasure_plan_matches(jsonb, jsonb) FROM PUBLIC;

-- ---- 8) The governed SECURITY DEFINER procedure (the ONLY path to current_user = guvfx_erasure) -----------
CREATE OR REPLACE FUNCTION public.bi_governed_erase_owner(p_request_id bigint, p_confirmation text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $proc$
DECLARE
  r record;
  v_owner bigint;
  v_live jsonb;
  v_del_blob_ids bigint[];
  v_session text;
  v_grant_id bigint;
  v_executor bigint;
  v_wd int := 0; v_obs int := 0; v_ev int := 0; v_blobs int := 0; v_mbx int := 0; v_idn int := 0;
  v_contacts int := 0; v_cred_tasks int := 0; v_total_tasks int := 0;
BEGIN
  -- Lock the request row; derive EVERYTHING from DB state (never trust caller-supplied owner/approver/plan).
  SELECT * INTO r FROM broker_intelligence_erasurerequest WHERE id = p_request_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'erasure request % not found', p_request_id; END IF;

  -- Lifecycle + complete verification record + two-person separation of duties (DB-authoritative).
  IF r.status <> 'APPROVED' THEN RAISE EXCEPTION 'request % not APPROVED (status=%)', p_request_id, r.status; END IF;
  IF NOT r.identity_verified OR r.verified_by_id IS NULL OR r.verified_at IS NULL
     OR btrim(coalesce(r.verification_method, '')) = '' THEN
     RAISE EXCEPTION 'incomplete identity verification record'; END IF;
  IF r.requested_by_id IS NULL THEN RAISE EXCEPTION 'no requester recorded (two-person cannot be enforced)'; END IF;
  IF r.approved_by_id IS NULL OR r.approved_at IS NULL THEN RAISE EXCEPTION 'no recorded approval'; END IF;
  IF r.approval_revoked THEN RAISE EXCEPTION 'approval revoked'; END IF;
  IF r.approval_expires_at IS NOT NULL AND r.approval_expires_at <= now() THEN RAISE EXCEPTION 'approval expired'; END IF;
  -- Separation-of-duties policy (see ADR-0054): requester, verifier, approver pairwise-distinct; executor != approver.
  IF r.verified_by_id = r.requested_by_id THEN RAISE EXCEPTION 'two-person: verifier = requester'; END IF;
  IF r.approved_by_id = r.requested_by_id THEN RAISE EXCEPTION 'two-person: approver = requester'; END IF;
  IF r.approved_by_id = r.verified_by_id THEN RAISE EXCEPTION 'two-person: approver = verifier'; END IF;
  IF r.assigned_executor_id IS NULL THEN RAISE EXCEPTION 'no assigned executor on the request'; END IF;
  IF r.assigned_executor_id = r.approved_by_id THEN RAISE EXCEPTION 'two-person: assigned executor = approver'; END IF;

  v_owner := r.subject_user_id_snapshot;
  IF v_owner IS NULL THEN RAISE EXCEPTION 'no subject owner id'; END IF;

  PERFORM pg_advisory_xact_lock(26485, v_owner::int);   -- one active execution per subject (two-int4 form)

  IF EXISTS (SELECT 1 FROM broker_intelligence_erasurehold
             WHERE subject_user_id_snapshot = v_owner AND active) THEN
     RAISE EXCEPTION 'active legal/security hold blocks erasure'; END IF;

  -- Confirmation possession + TOCTOU: token must match the planned token, and the live facts must match the plan.
  IF r.plan IS NULL THEN RAISE EXCEPTION 'no stored plan'; END IF;
  IF p_confirmation IS DISTINCT FROM r.confirm_token THEN
     RAISE EXCEPTION 'confirmation token does not match the planned token'; END IF;
  v_live := public.bi_erasure_plan_facts(v_owner);
  IF NOT public.bi_erasure_plan_matches(r.plan, v_live) THEN
     RAISE EXCEPTION 'owner state changed since the plan was produced (TOCTOU)'; END IF;

  -- P0 — resolve the EXECUTOR from the DB-authenticated login, NOT a caller argument. session_user is the role
  -- that connected; SECURITY DEFINER changes current_user to the procedure owner but NEVER session_user. A
  -- single-use ErasureExecutionGrant (created out of band by the independent grant process; the app role has no
  -- access to that table) maps this exact session to an operator for THIS request. A missing/revoked/expired/
  -- already-consumed grant, or one mapping a different operator, fails closed. The grant is then consumed.
  v_session := session_user;
  SELECT g.id, g.operator_id INTO v_grant_id, v_executor
  FROM broker_intelligence_erasureexecutiongrant g
  WHERE g.request_id = p_request_id AND g.db_session_user = v_session
        AND NOT g.revoked AND g.consumed_at IS NULL AND g.expires_at > now()
  FOR UPDATE;
  IF v_grant_id IS NULL THEN
     RAISE EXCEPTION 'no valid execution grant for session % on request %', v_session, p_request_id; END IF;
  IF v_executor IS NULL THEN RAISE EXCEPTION 'execution grant has no operator'; END IF;
  IF v_executor <> r.assigned_executor_id THEN
     RAISE EXCEPTION 'grant operator (%) does not match the request assigned executor (%)',
        v_executor, r.assigned_executor_id; END IF;
  IF v_executor = r.approved_by_id THEN RAISE EXCEPTION 'two-person: executor = approver'; END IF;
  UPDATE broker_intelligence_erasureexecutiongrant SET consumed_at = now() WHERE id = v_grant_id;  -- single-use

  -- Enqueue credential-file deletions for the owner's mailboxes (all are about to be deleted), then delete them.
  INSERT INTO broker_intelligence_deletiontask(request_id, kind, target_ref, status, attempts, last_error, created_at)
  SELECT p_request_id, 'CREDENTIAL_FILE', credential_ref, 'PENDING', 0, '', now()
  FROM broker_intelligence_connectedmailbox
  WHERE user_id = v_owner AND coalesce(credential_ref, '') <> ''
  ON CONFLICT (request_id, kind, target_ref) DO NOTHING;
  GET DIAGNOSTICS v_cred_tasks = ROW_COUNT;

  -- Delete owner-scoped rows in FK-safe order. Withdrawals first (they PROTECT events).
  DELETE FROM broker_intelligence_withdrawal WHERE owner_id = v_owner;
  GET DIAGNOSTICS v_wd = ROW_COUNT;

  -- Delete observations + events TOGETHER and capture the evidence ids of the rows ACTUALLY removed. This closes
  -- the plan->delete window (adversarial-review MED): the deletable-blob set is derived from live rows at delete
  -- time, so a row ingested for the owner before this statement is deleted here and its blob captured, instead of
  -- relying on the earlier plan snapshot. (Residual: a row ingested AFTER this statement is new data; full
  -- closure requires ingestion to take pg_advisory_xact_lock(26485, owner) or a mailbox-disconnect precondition —
  -- see ADR-0054, a production-arming follow-up.)
  WITH del_ob AS (DELETE FROM broker_intelligence_brokermessageobservation WHERE owner_id = v_owner RETURNING evidence_id),
       del_ev AS (DELETE FROM broker_intelligence_brokerevent WHERE owner_id = v_owner RETURNING evidence_id)
  SELECT (SELECT count(*) FROM del_ob),
         (SELECT count(*) FROM del_ev),
         (SELECT coalesce(array_agg(DISTINCT e), ARRAY[]::bigint[]) FROM (
            SELECT evidence_id AS e FROM del_ob WHERE evidence_id IS NOT NULL
            UNION ALL SELECT evidence_id FROM del_ev WHERE evidence_id IS NOT NULL) s)
  INTO v_obs, v_ev, v_del_blob_ids;

  -- Delete only the now-unreferenced candidate blobs; enqueue EVIDENCE_FILE tasks for exactly those removed.
  WITH del AS (
     DELETE FROM broker_intelligence_evidenceblob b
     WHERE b.id = ANY (coalesce(v_del_blob_ids, ARRAY[]::bigint[]))
       AND NOT EXISTS (SELECT 1 FROM broker_intelligence_brokerevent e WHERE e.evidence_id = b.id)
       AND NOT EXISTS (SELECT 1 FROM broker_intelligence_brokermessageobservation o WHERE o.evidence_id = b.id)
     RETURNING b.sha256
  ), ins AS (
     INSERT INTO broker_intelligence_deletiontask(request_id, kind, target_ref, status, attempts, last_error, created_at)
     SELECT p_request_id, 'EVIDENCE_FILE', sha256, 'PENDING', 0, '', now() FROM del
     ON CONFLICT (request_id, kind, target_ref) DO NOTHING
     RETURNING 1
  )
  SELECT count(*) INTO v_blobs FROM del;

  -- Release inbound PROTECT references to the owner's identities before deleting them:
  --   (a) audit.identity (nulled); (b) TradingAccountBrokerContact.broker_email_identity (the binding is the
  --   subject's data, so it is DELETED — adversarial-review MED: without this, erasure of any subject with a
  --   broker-contact binding aborts on the FK).
  UPDATE broker_intelligence_brokeremailidentityaudit SET identity_id = NULL
  WHERE identity_id IN (SELECT id FROM broker_intelligence_brokeremailidentity WHERE user_id = v_owner);
  DELETE FROM broker_intelligence_tradingaccountbrokercontact
  WHERE broker_email_identity_id IN (SELECT id FROM broker_intelligence_brokeremailidentity WHERE user_id = v_owner);
  GET DIAGNOSTICS v_contacts = ROW_COUNT;

  DELETE FROM broker_intelligence_connectedmailbox WHERE user_id = v_owner;
  GET DIAGNOSTICS v_mbx = ROW_COUNT;
  DELETE FROM broker_intelligence_brokeremailidentity WHERE user_id = v_owner;
  GET DIAGNOSTICS v_idn = ROW_COUNT;

  -- Audit-PII anonymisation (Finding G): keep the accountability row, remove/replace the subject identifiers.
  UPDATE broker_intelligence_brokeremailidentityaudit
     SET email = '', proofs = '{}'::jsonb, trading_account_id = NULL, connected_mailbox_id = NULL,
         subject_user_id = NULL
   WHERE subject_user_id = v_owner;
  UPDATE broker_intelligence_tradingaccountbrokercontactaudit
     SET proofs = '{}'::jsonb, trading_account_id = NULL, broker_email_identity_id = NULL,
         connected_mailbox_id = NULL, subject_user_id = NULL
   WHERE subject_user_id = v_owner;
  UPDATE core_auditevent
     SET ip_address = NULL, user_agent = '', metadata = '{}'::jsonb, user_id = NULL
   WHERE user_id = v_owner;

  SELECT count(*) INTO v_total_tasks FROM broker_intelligence_deletiontask WHERE request_id = p_request_id;

  -- Terminal state (counts only; never content). EXECUTED => rows gone; worker drains files to COMPLETED.
  UPDATE broker_intelligence_erasurerequest
     SET status = 'EXECUTED', executed_at = now(), executed_by_id = v_executor,
         outcome = jsonb_build_object(
            'withdrawals', v_wd, 'observations', v_obs, 'broker_events', v_ev,
            'evidence_blobs_deleted', v_blobs, 'connected_mailboxes', v_mbx, 'identities', v_idn,
            'contacts', v_contacts, 'deletion_tasks', v_total_tasks, 'credential_file_tasks', v_cred_tasks,
            'executor_db_session', v_session)
   WHERE id = p_request_id;

  RETURN jsonb_build_object('status', 'EXECUTED', 'owner', v_owner,
            'withdrawals', v_wd, 'observations', v_obs, 'broker_events', v_ev,
            'evidence_blobs_deleted', v_blobs, 'connected_mailboxes', v_mbx, 'identities', v_idn,
            'contacts', v_contacts, 'deletion_tasks', v_total_tasks);
END
$proc$;

-- Drop the old 3-arg signature if a previous provision created it (the executor id argument is removed, P0).
DROP FUNCTION IF EXISTS public.bi_governed_erase_owner(bigint, bigint, text);
ALTER FUNCTION public.bi_governed_erase_owner(bigint, text) OWNER TO guvfx_erasure;
COMMENT ON FUNCTION public.bi_governed_erase_owner(bigint, text) IS 'ADR-0054/erasure-proc/v2';
REVOKE ALL ON FUNCTION public.bi_governed_erase_owner(bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.bi_governed_erase_owner(bigint, text) FROM :"app_role";
GRANT EXECUTE ON FUNCTION public.bi_governed_erase_owner(bigint, text) TO guvfx_erasure_exec;

COMMIT;

\echo 'provision_erasure_privilege.sql: COMMITTED. Run verify_erasure_privilege.sql next.'
