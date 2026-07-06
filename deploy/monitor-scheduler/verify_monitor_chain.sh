#!/usr/bin/env bash
# E3-MONITOR-SCHEDULING — post-deploy verification for the monitor chain.
#
# Proves the scheduled command is SAFE by running it once and asserting it created no order, no
# WIMS contract, and transmitted no notification — before/after counts must be unchanged for
# those, while internal outcome records / candidates may increase (that is the command's job and
# is idempotent). Read-only w.r.t. real trading; calls no order_send. Prints no secrets.
#
# Run AFTER installing the cron (or any time — it is idempotent and safe). Exit 0 = PASS.
#
# Config via env (safe defaults for the prod layout):
#   BACKEND_CONTAINER   backend container/service (default: guvfx-backend)
set -euo pipefail

BACKEND_CONTAINER="${BACKEND_CONTAINER:-guvfx-backend}"
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== E3-MONITOR-SCHEDULING chain verification =="

# Snapshot the three boundary invariants (order jobs / WIMS contracts / transmitted deliveries).
snapshot() {
  docker exec "$BACKEND_CONTAINER" python manage.py shell -c "
from execution.models import ExecutionJob, NotificationDelivery
from wims.models import ConsumptionContract
print('%d %d %d' % (
    ExecutionJob.objects.count(),
    ConsumptionContract.objects.count(),
    NotificationDelivery.objects.filter(transmitted=True).count(),
))" 2>/dev/null | tr -d '\r'
}

read -r JOBS_B CONTRACTS_B TX_B <<<"$(snapshot)"
[ -n "${JOBS_B:-}" ] || fail "could not read pre-run snapshot"
echo "ok: pre-run  jobs=$JOBS_B wims_contracts=$CONTRACTS_B transmitted=$TX_B"

# 1. Structural smoke run — --limit 0 exercises the full wiring while touching zero rows.
docker exec "$BACKEND_CONTAINER" python manage.py run_monitor_chain --limit 0 \
  | grep -q "monitor-chain:" || fail "structural (--limit 0) run produced no summary line"
echo "ok: structural --limit 0 run wired end-to-end (zero rows touched)"

# 2. Real bounded run — processes any pre-existing closed trades (internal records only).
OUT="$(docker exec "$BACKEND_CONTAINER" python manage.py run_monitor_chain 2>&1)"
echo "$OUT" | grep -q "monitor-chain:" || fail "chain produced no summary line"
echo "$OUT" | grep -q "dispatch\[enabled=" || fail "summary missing dispatch posture"
echo "$OUT" | grep -q "failures=none" || fail "a chain step failed: $OUT"
echo "chain output: $OUT"

read -r JOBS_A CONTRACTS_A TX_A <<<"$(snapshot)"
echo "ok: post-run jobs=$JOBS_A wims_contracts=$CONTRACTS_A transmitted=$TX_A"

# Boundary assertions — these three must be UNCHANGED by a chain run.
[ "$JOBS_A" = "$JOBS_B" ] || fail "chain created $((JOBS_A - JOBS_B)) ExecutionJob(s) — must create none"
[ "$CONTRACTS_A" = "$CONTRACTS_B" ] || fail "chain created a WIMS ConsumptionContract — must create none"
[ "$TX_A" = "$TX_B" ] || fail "chain transmitted a notification — must transmit none (dry-run)"

echo "== VERIFICATION PASSED == (no order, no WIMS contract, nothing transmitted)"
