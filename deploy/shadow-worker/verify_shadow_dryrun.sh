#!/usr/bin/env bash
# EXEC-E2b-PERSIST — deployment verification for the managed shadow worker.
#
# Runs ONE full-pipeline dry-run and asserts the shadow worker processed it
# through order_check ONLY, with no order placed:
#   PLACE_ORDER_SHADOW job (demo) -> shadow worker claim -> bridge /mt5/order_check
#   -> job SUCCESS with validation diagnostics, no ticket/deal/order, order_send=0.
#
# Read-only w.r.t. real trading: it creates and then deletes a single dry-run job
# and never calls order_send. It prints NO secrets. Run AFTER the shadow service
# is up. Exit 0 = PASS, non-zero = FAIL.
#
# Config via env (safe defaults for the prod layout):
#   BACKEND_CONTAINER   backend container/service (default: guvfx-backend)
#   SHADOW_CONTAINER    shadow worker container   (default: guvfx-mt5-shadow-worker)
#   SHADOW_WORKER_ID    expected claimer identity (default: mt5-shadow-worker-1)
#   NORMAL_CONTAINER    normal worker container   (default: guvfx-mt5-trade-ingest-worker)
#   POLL_TIMEOUT_SEC    max wait for completion   (default: 40)
set -euo pipefail

BACKEND_CONTAINER="${BACKEND_CONTAINER:-guvfx-backend}"
SHADOW_CONTAINER="${SHADOW_CONTAINER:-guvfx-mt5-shadow-worker}"
SHADOW_WORKER_ID="${SHADOW_WORKER_ID:-mt5-shadow-worker-1}"
NORMAL_CONTAINER="${NORMAL_CONTAINER:-guvfx-mt5-trade-ingest-worker}"
POLL_TIMEOUT_SEC="${POLL_TIMEOUT_SEC:-40}"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== E2b-PERSIST shadow dry-run verification =="

# Preconditions: shadow container running; normal worker must NOT have the flag.
[ "$(docker inspect "$SHADOW_CONTAINER" --format '{{.State.Status}}' 2>/dev/null)" = "running" ] \
  || fail "shadow container '$SHADOW_CONTAINER' is not running"

if docker inspect "$NORMAL_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
     | grep -q '^MT5_SHADOW_WORKER=1'; then
  fail "normal worker '$NORMAL_CONTAINER' has MT5_SHADOW_WORKER=1 (must be default OFF)"
fi
echo "ok: shadow container running; normal worker default OFF"

# Create one PLACE_ORDER_SHADOW dry-run job on a demo account; print only the id.
JOB_ID=$(docker exec "$BACKEND_CONTAINER" python manage.py shell -c "
from execution.models import ExecutionJob as J
from trading.models import TradingAccount
acct = TradingAccount.objects.filter(is_demo=True).first()
assert acct, 'no demo account available'
job = J.objects.create(job_type='PLACE_ORDER_SHADOW', account=acct, status='PENDING',
    terminal_node=None,
    payload={'symbol':'EURUSD','side':'BUY','lots':'0.01','is_demo':True,
             'execution_mode':'SHADOW','comment':'E2b-persist-verify','windows_username':'guvfx_u_1'})
print('JOBID:'+str(job.id))
" 2>/dev/null | sed -n 's/^JOBID://p')
[ -n "${JOB_ID:-}" ] || fail "could not create dry-run job"
echo "ok: created dry-run PLACE_ORDER_SHADOW job #$JOB_ID"

# Poll for the shadow worker to claim + complete it via order_check.
deadline=$((SECONDS + POLL_TIMEOUT_SEC))
STATUS=""
while [ "$SECONDS" -lt "$deadline" ]; do
  STATUS=$(docker exec "$BACKEND_CONTAINER" python manage.py shell -c "
from execution.models import ExecutionJob as J
print(J.objects.get(id=$JOB_ID).status)
" 2>/dev/null | tr -d '[:space:]')
  [ "$STATUS" = "SUCCESS" ] || [ "$STATUS" = "FAILED" ] && break
  sleep 2
done

# Assert outcome: SUCCESS, claimed by the shadow identity, order_check-only, no order.
RESULT=$(docker exec "$BACKEND_CONTAINER" python manage.py shell -c "
import json
from execution.models import ExecutionJob as J
j = J.objects.get(id=$JOB_ID)
r = j.result or {}
w = getattr(j,'claimed_by_worker_id',None) or getattr(j,'worker_id',None) or ''
print(json.dumps({
  'status': j.status,
  'claimed_by': w,
  'order_send_called': r.get('order_send_called'),
  'retcode': r.get('retcode'),
  'has_order_ids': any(k in r for k in ('order','deal','ticket','position')),
}))
" 2>/dev/null | tail -1)
echo "result: $RESULT"

python3 - "$RESULT" "$SHADOW_WORKER_ID" <<'PY'
import json, sys
r = json.loads(sys.argv[1]); shadow_id = sys.argv[2]
assert r["status"] == "SUCCESS", f"job status {r['status']} != SUCCESS"
assert r["claimed_by"] == shadow_id, f"claimed by {r['claimed_by']!r} != {shadow_id!r}"
assert r["order_send_called"] is False, "order_send_called must be False"
assert r["has_order_ids"] is False, "no order/deal/ticket/position may be present"
print("PASS: order_check-only dry-run, no order placed, claimed by shadow identity")
PY

# Cleanup the dry-run job.
docker exec "$BACKEND_CONTAINER" python manage.py shell -c "
from execution.models import ExecutionJob as J
J.objects.filter(id=$JOB_ID).delete()
print('cleaned')
" >/dev/null 2>&1 || true
echo "ok: dry-run job #$JOB_ID cleaned up"
echo "== VERIFICATION PASSED =="
