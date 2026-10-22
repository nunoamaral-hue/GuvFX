#!/usr/bin/env bash
# ADR-0054 — DISPOSABLE PostgreSQL-15 rehearsal for controlled erasure. REHEARSAL-ONLY.
# Spins up a throwaway postgres:15 container (synthetic data only; no prod connectivity, no NAS, no OAuth, no
# prod env file), provisions the privilege boundary with newly-generated disposable credentials, runs the full
# integration sequence, verifies, rolls back, and DESTROYS the container. Produces an evidence log.
#
# Usage:  BACKEND=/path/to/backend ops/erasure/rehearse_erasure.sh   (run from the repo root)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="${BACKEND:-$REPO_ROOT/backend}"
PY="$BACKEND/.venv/bin/python"
CONTAINER="guvfx_erasure_rehearsal_$$"
PORT="${REHEARSAL_PORT:-55432}"
DBNAME="guvfx_rehearsal"
WORK="$(mktemp -d)"
EVLOG="$WORK/evidence.log"
PSQL_SUPER=(docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DBNAME")

SUPER_PW="$(openssl rand -hex 16)"
APP_PW="$(openssl rand -hex 16)"
EXEC_PW="$(openssl rand -hex 16)"
MIGRATE_PW="$(openssl rand -hex 16)"
READONLY_PW="$(openssl rand -hex 16)"

log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$EVLOG"; }
teardown(){ log "teardown: removing container $CONTAINER"; docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap teardown EXIT

export DJANGO_SECRET_KEY="rehearsal-only-$(openssl rand -hex 8)"
export DB_NAME="$DBNAME" DB_USER="guvfx_app" DB_PASSWORD="$APP_PW" DB_HOST="127.0.0.1" DB_PORT="$PORT"
export BROKER_INTELLIGENCE_EVIDENCE_ROOT="$WORK/evidence_store"
export BROKER_INTELLIGENCE_CREDENTIAL_ROOT="$WORK/credential_store"
mkdir -p "$BROKER_INTELLIGENCE_EVIDENCE_ROOT" "$BROKER_INTELLIGENCE_CREDENTIAL_ROOT"
chmod 700 "$BROKER_INTELLIGENCE_CREDENTIAL_ROOT"

log "=== 0) start disposable postgres:15 container (port $PORT) ==="
docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD="$SUPER_PW" -e POSTGRES_DB="$DBNAME" \
  -p "$PORT:5432" postgres:15 >/dev/null
# wait for readiness
for i in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U postgres -d "$DBNAME" >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$CONTAINER" pg_isready -U postgres -d "$DBNAME" >/dev/null

log "=== 1) bootstrap app role (temporary superuser for migrate; owns the schema) ==="
"${PSQL_SUPER[@]}" <<SQL
DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_app') THEN
  CREATE ROLE guvfx_app LOGIN SUPERUSER PASSWORD '$APP_PW'; END IF; END \$\$;
ALTER ROLE guvfx_app PASSWORD '$APP_PW';
GRANT ALL ON SCHEMA public TO guvfx_app;
ALTER SCHEMA public OWNER TO guvfx_app;
SQL

log "=== 2) django migrate (as guvfx_app) ==="
( cd "$BACKEND" && "$PY" manage.py migrate --noinput >>"$EVLOG" 2>&1 )
log "migrate complete"

log "=== 3) drop temporary superuser from the app role (it must be an ordinary role now) ==="
"${PSQL_SUPER[@]}" -c "ALTER ROLE guvfx_app NOSUPERUSER;"

log "=== 4) PREPROVISION phase: negative control + seed synthetic data + governed lifecycle ==="
( cd "$BACKEND" && "$PY" manage.py rehearse_controlled_erasure --phase=preprovision \
    --i-understand-this-is-a-disposable-rehearsal 2>&1 | tee -a "$EVLOG" )

log "=== 5) PROVISION the privilege boundary (superuser) ==="
docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DBNAME" \
  -v target_db="$DBNAME" -v allow_destructive_provisioning=1 -v synthetic_confirmed=1 \
  -v app_role=guvfx_app -v erasure_exec_pw="$EXEC_PW" -v migrate_pw="$MIGRATE_PW" -v readonly_pw="$READONLY_PW" \
  < "$REPO_ROOT/ops/erasure/provision_erasure_privilege.sql" 2>&1 | tee -a "$EVLOG"

log "=== 6) VERIFY the privilege boundary (superuser) ==="
docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DBNAME" -v app_role=guvfx_app \
  < "$REPO_ROOT/ops/erasure/verify_erasure_privilege.sql" 2>&1 | tee -a "$EVLOG"

log "=== 6b) confirm the migration role can still DDL under the new schema ownership (future migrations) ==="
if docker exec -i -e PGPASSWORD="$MIGRATE_PW" "$CONTAINER" psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U guvfx_migrate \
     -d "$DBNAME" -c "CREATE TABLE _mig_probe(id int); DROP TABLE _mig_probe;" >/dev/null 2>&1; then
  echo "EVIDENCE|20-future-migrations-work|PASS|migrate role CREATE/DROP ok post-provision" | tee -a "$EVLOG"
else
  echo "EVIDENCE|20-future-migrations-work|FAIL|migrate role cannot DDL post-provision" | tee -a "$EVLOG"
fi

log "=== 7) POSTPROVISION phase: DB boundary + real governed execution + outbox + anonymisation + concurrency ==="
( cd "$BACKEND" && \
  ERASURE_EXECUTOR_DB_USER=guvfx_erasure_exec ERASURE_EXECUTOR_DB_PASSWORD="$EXEC_PW" \
  BROKER_INTELLIGENCE_ERASURE_ENABLED=1 \
  "$PY" manage.py rehearse_controlled_erasure --phase=postprovision \
    --i-understand-this-is-a-disposable-rehearsal 2>&1 | tee -a "$EVLOG" )

log "=== 8) ROLLBACK + confirm the original arrangement is restored ==="
docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DBNAME" \
  -v target_db="$DBNAME" -v app_role=guvfx_app \
  < "$REPO_ROOT/ops/erasure/rollback_erasure_privilege.sql" 2>&1 | tee -a "$EVLOG"
# after rollback: the procedure is gone and the evidence table is owned by the app role again
"${PSQL_SUPER[@]}" -t <<SQL 2>&1 | tee -a "$EVLOG"
SELECT 'EVIDENCE|22-rollback-proc-gone|' ||
  CASE WHEN count(*)=0 THEN 'PASS' ELSE 'FAIL' END || '|procs='||count(*)
  FROM pg_proc WHERE proname='bi_governed_erase_owner';
SELECT 'EVIDENCE|22-rollback-ownership-restored|' ||
  CASE WHEN pg_get_userbyid(relowner)='guvfx_app' THEN 'PASS' ELSE 'FAIL' END ||
  '|owner='||pg_get_userbyid(relowner)
  FROM pg_class WHERE relname='broker_intelligence_evidenceblob';
SQL

log "=== DONE. Evidence log: $EVLOG ==="
echo "----- EVIDENCE SUMMARY -----"
grep -E "^EVIDENCE\|" "$EVLOG" || true
# non-zero exit if any FAIL line present
if grep -qE "^EVIDENCE\|[^|]*\|FAIL\|" "$EVLOG"; then echo "REHEARSAL: FAILURES PRESENT"; exit 1; fi
echo "REHEARSAL: ALL EVIDENCE PASS"
cp "$EVLOG" "$REPO_ROOT/ops/erasure/last_rehearsal_evidence.log" 2>/dev/null || true
