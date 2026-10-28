#!/usr/bin/env bash
# verify_fx_ingestion.sh - validate the installed FX ingestion schedule without triggering an ingestion.
# Checks: the three cron lines are present; wrapper scripts are executable; flock contention refuses a second
# instance; the freshness monitor runs and reports (quiet OK when healthy). Read-only; starts no ingestion.
set -uo pipefail
REPO_DIR="${REPO_DIR:-/home/ubuntu/guvfx-app}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
INGEST="${REPO_DIR}/deploy/fx-ingestion/run_fx_boe_ingest.sh"
FRESH="${REPO_DIR}/deploy/fx-ingestion/run_fx_freshness_check.sh"
INGEST_LOG="${LOG_DIR}/fetch_fx_boe.log"
FRESH_LOG="${LOG_DIR}/fx_freshness.log"

# shellcheck source=deploy/fx-ingestion/lib_fx_log.sh
. "${REPO_DIR}/deploy/fx-ingestion/lib_fx_log.sh"

echo "== cron lines present =="
crontab -l 2>/dev/null | grep -E "guvfx-fetch-fx-boe|guvfx-fx-freshness" || { echo "MISSING cron lines"; exit 1; }

echo "== scripts executable =="
[ -x "$INGEST" ] && [ -x "$FRESH" ] && echo "ok" || { echo "scripts not executable"; exit 1; }

echo "== cron-user log redirection boundary (the real failure path, not just dir existence) =="
# The scheduled jobs run as this (cron) user and redirect with '>>' into LOG_DIR. If a redirect target cannot be
# created/appended by this user, the scheduled command fails to even start - historically a SILENT failure. Test
# the actual boundary for BOTH logs. Writes no bytes.
_boundary_rc=0
for _f in "$INGEST_LOG" "$FRESH_LOG"; do
  if [ ! -e "$_f" ]; then
    echo "FAIL: $_f does not exist - cron user cannot create it in root-owned $LOG_DIR (run the installer)"; _boundary_rc=1
  elif fx_check_appendable "$_f"; then
    echo "ok: appendable by $(id -un) -> $_f"
  else
    echo "FAIL: $(id -un) cannot append to $_f via '>>' - cron would fail here"; _boundary_rc=1
  fi
done
[ "$_boundary_rc" -eq 0 ] || { echo "LOG REDIRECTION BOUNDARY FAILED"; exit 1; }

echo "== flock contention (second instance must refuse, exit 0 with SKIP) =="
LK=/tmp/guvfx-fetch-fx-boe.lock
( exec 9>"$LK"; flock -n 9 && sleep 3 ) &
held=$!
sleep 1
LOCK_FILE="$LK" MAX_ATTEMPTS=1 RUN_TIMEOUT=5 "$INGEST" 2>&1 | grep -q "SKIP" && echo "ok: second instance refused" || echo "WARN: contention not demonstrated"
wait "$held" 2>/dev/null || true

echo "== freshness monitor (quiet OK expected when healthy) =="
"$FRESH" || echo "(freshness check returned non-zero = alert condition)"
