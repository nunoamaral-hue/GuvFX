-- F-8 runtime-role grants for analytics_dailyfxobservation (ADR-0058).
--
-- Applied UNDER the F-8 runtime-role cutover — NOT by this PR, and NOT in production until a separate governed
-- GO. Combined with the append-only triggers (migration 0003_fx_observation_append_only_triggers), this makes
-- the FX store immutable from every NON-OWNER identity: the web/dashboard runtime is SELECT-only, and the
-- governed ingestion writer may only INSERT (append-only). Neither may UPDATE/DELETE/TRUNCATE.
--
-- TRUNCATE is owner-only in PostgreSQL, so a non-owner runtime cannot TRUNCATE regardless of these grants; the
-- REVOKE below is belt-and-suspenders. While the application still connects as a superuser/owner identity
-- (pre-F-8), that identity CAN TRUNCATE and can bypass triggers — which is exactly why FX enablement is BLOCKED
-- until F-8 (see ADR-0058).
--
-- Substitute your F-8 role names:
--   <runtime_role>  = the SELECT-only application/web runtime role
--   <ingest_role>   = the governed ingestion writer identity that runs `manage.py fetch_fx_daily`

REVOKE ALL ON TABLE analytics_dailyfxobservation FROM PUBLIC;

-- Web / dashboard runtime: SELECT only (no write path to FX observations at all).
GRANT SELECT ON TABLE analytics_dailyfxobservation TO <runtime_role>;

-- Governed ingestion writer: SELECT + INSERT only (append-only).
GRANT SELECT, INSERT ON TABLE analytics_dailyfxobservation TO <ingest_role>;

-- Defence in depth: neither role may mutate or truncate.
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE analytics_dailyfxobservation FROM <runtime_role>, <ingest_role>;

-- Verify (expect: runtime_role has only SELECT; ingest_role has SELECT+INSERT; neither has UPDATE/DELETE/TRUNCATE):
--   SELECT grantee, privilege_type FROM information_schema.role_table_grants
--   WHERE table_name = 'analytics_dailyfxobservation' ORDER BY grantee, privilege_type;
