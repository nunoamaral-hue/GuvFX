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
ERX_PW="$(openssl rand -hex 16)"       # per-operator executor LOGIN (member of the guvfx_erasure_exec group)
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
PREOUT="$WORK/preprovision.out"
( cd "$BACKEND" && "$PY" manage.py rehearse_controlled_erasure --phase=preprovision \
    --i-understand-this-is-a-disposable-rehearsal ) >"$PREOUT" 2>&1 || { cat "$PREOUT" | tee -a "$EVLOG"; exit 1; }
grep -E "^EVIDENCE\|" "$PREOUT" | tee -a "$EVLOG"

log "=== 5) PROVISION the privilege boundary (superuser; guvfx_erasure_exec is a NOLOGIN group) ==="
docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DBNAME" \
  -v target_db="$DBNAME" -v allow_destructive_provisioning=1 -v synthetic_confirmed=1 \
  -v app_role=guvfx_app -v migrate_pw="$MIGRATE_PW" -v readonly_pw="$READONLY_PW" \
  < "$REPO_ROOT/ops/erasure/provision_erasure_privilege.sql" 2>&1 | tee -a "$EVLOG"

log "=== 5b) grant-admin (independent process): per-operator executor login + single-use execution grants ==="
# A distinct short-lived LOGIN that is a MEMBER of the NOLOGIN guvfx_erasure_exec group. The procedure resolves
# the human executor from THIS login's session via the grant rows below — the executor identity is never a caller
# argument. The app role has no access to the grant table; only this independent (superuser) process writes it.
docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DBNAME" <<SQL 2>&1 | tee -a "$EVLOG"
DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='guvfx_erx_rh') THEN
  CREATE ROLE guvfx_erx_rh LOGIN PASSWORD '$ERX_PW'; END IF; END \$\$;
ALTER ROLE guvfx_erx_rh PASSWORD '$ERX_PW';
GRANT guvfx_erasure_exec TO guvfx_erx_rh;
SQL
WRONGOP_ID=$(docker exec -i "$CONTAINER" psql -tA -U postgres -d "$DBNAME" \
  -c "SELECT id FROM users_user WHERE username='rh-wrongop-other'" | tr -d '[:space:]')
log "grant-admin: WRONGOP_ID=$WRONGOP_ID; GRANT lines=$(grep -cE '^GRANT\|' "$PREOUT")"
_grant_fail=0
while IFS='|' read -r _tag uname reqid opid state; do
  reqid="$(echo "$reqid" | tr -d '[:space:]')"; opid="$(echo "$opid" | tr -d '[:space:]')"
  state="$(echo "$state" | tr -d '[:space:]')"
  cons="NULL"
  case "$state" in
    none)     log "grant: $uname req=$reqid state=none (no grant)"; continue;;
    expired)  exp="now() - interval '1 hour'"; rev="false"; op="$opid";;
    revoked)  exp="now() + interval '1 hour'"; rev="true";  op="$opid";;
    wrongop)  exp="now() + interval '1 hour'"; rev="false"; op="$WRONGOP_ID";;
    consumed) exp="now() + interval '1 hour'"; rev="false"; op="$opid"; cons="now()";;
    *)        exp="now() + interval '1 hour'"; rev="false"; op="$opid";;
  esac
  if docker exec "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DBNAME" -c \
    "INSERT INTO broker_intelligence_erasureexecutiongrant(request_id, db_session_user, operator_id, expires_at, revoked, consumed_at, created_at) VALUES ($reqid, 'guvfx_erx_rh', $op, $exp, $rev, $cons, now());" </dev/null >/dev/null 2>"$WORK/grant_err"; then
    log "grant: $uname req=$reqid state=$state op=$op OK"
  else
    log "grant: $uname req=$reqid state=$state op=$op FAILED: $(cat "$WORK/grant_err")"; _grant_fail=1
  fi
done < <(grep -E "^GRANT\|" "$PREOUT")
if [ "$_grant_fail" = "0" ]; then
  echo "EVIDENCE|05b-execution-grants-created|PASS|per-operator login + single-use grants installed by the grant-admin" | tee -a "$EVLOG"
else
  echo "EVIDENCE|05b-execution-grants-created|FAIL|one or more grant inserts failed" | tee -a "$EVLOG"
fi

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
  ERASURE_EXECUTOR_DB_USER=guvfx_erx_rh ERASURE_EXECUTOR_DB_PASSWORD="$ERX_PW" \
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
