#!/usr/bin/env bash
# verify_fx_ingestion.sh - validate the installed FX ingestion schedule without triggering an ingestion.
# Checks: the three cron lines are present; wrapper scripts are executable; flock contention refuses a second
# instance; the freshness monitor runs and reports (quiet OK when healthy). Read-only; starts no ingestion.
set -uo pipefail
REPO_DIR="${REPO_DIR:-/home/ubuntu/guvfx-app}"
INGEST="${REPO_DIR}/deploy/fx-ingestion/run_fx_boe_ingest.sh"
FRESH="${REPO_DIR}/deploy/fx-ingestion/run_fx_freshness_check.sh"

echo "== cron lines present =="
crontab -l 2>/dev/null | grep -E "guvfx-fetch-fx-boe|guvfx-fx-freshness" || { echo "MISSING cron lines"; exit 1; }

echo "== scripts executable =="
[ -x "$INGEST" ] && [ -x "$FRESH" ] && echo "ok" || { echo "scripts not executable"; exit 1; }

echo "== flock contention (second instance must refuse, exit 0 with SKIP) =="
LK=/tmp/guvfx-fetch-fx-boe.lock
( exec 9>"$LK"; flock -n 9 && sleep 3 ) &
held=$!
sleep 1
LOCK_FILE="$LK" MAX_ATTEMPTS=1 RUN_TIMEOUT=5 "$INGEST" 2>&1 | grep -q "SKIP" && echo "ok: second instance refused" || echo "WARN: contention not demonstrated"
wait "$held" 2>/dev/null || true

echo "== freshness monitor (quiet OK expected when healthy) =="
"$FRESH" || echo "(freshness check returned non-zero = alert condition)"
