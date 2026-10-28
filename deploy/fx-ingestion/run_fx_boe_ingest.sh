#!/usr/bin/env bash
# run_fx_boe_ingest.sh - daily Bank of England XUDLUSS (GBP->USD) ingestion wrapper.
#
# Single-instance (flock, non-blocking), bounded per-attempt runtime (timeout), bounded retries with backoff,
# structured UTC log lines. Append-only and idempotent: the underlying `fetch_fx_boe` writes missing revisions
# only (identical observation_checksum skipped) via the dedicated fx_ingest identity; it never updates/deletes an
# existing observation, never places/modifies an order, never touches FRED. On final failure it emits an
# actionable `FX_INGEST_ALERT` line and exits non-zero (picked up by log/ops monitoring).
#
# No secret is embedded here; the backend container holds the fx_ingest credential in its own env.
set -uo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
LOCK_FILE="${LOCK_FILE:-/tmp/guvfx-fetch-fx-boe.lock}"
RUN_TIMEOUT="${RUN_TIMEOUT:-120}"        # seconds per attempt
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
BACKOFFS="${BACKOFFS:-30 120}"           # seconds to wait before attempts 2, 3

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) fx-boe-ingest $*"; }

exec 9>"$LOCK_FILE" 2>/dev/null || { log "LOCKFILE_OPEN_FAILED $LOCK_FILE"; exit 1; }
if ! flock -n 9; then
  log "SKIP another ingestion is already running (lock held) - no overlap"
  exit 0
fi

read -r -a backoff_arr <<< "$BACKOFFS"
attempt=0
while : ; do
  attempt=$((attempt + 1))
  out="$(cd "$COMPOSE_DIR" && timeout "$RUN_TIMEOUT" docker compose exec -T "$BACKEND_SERVICE" \
         python manage.py fetch_fx_boe 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    log "OK attempt=$attempt rc=0 $out"
    exit 0
  fi
  log "FAIL attempt=$attempt rc=$rc $out"
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    log "FX_INGEST_ALERT after=${attempt}_attempts rc=$rc BoE ingestion failing - manual check required"
    exit "$rc"
  fi
  sleep "${backoff_arr[$((attempt - 1))]:-120}"
done
