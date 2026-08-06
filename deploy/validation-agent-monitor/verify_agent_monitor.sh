#!/usr/bin/env bash
# VALIDATION-AGENT-MONITOR — read-only verifier. Confirms the cron line is present and runs ONE
# probe pass in --dry-run mode (delivers nothing) to prove the pipeline executes. Prints no secrets.
#
#   deploy/validation-agent-monitor/verify_agent_monitor.sh
set -euo pipefail

MARKER_RE="# guvfx-agent-monitor$"
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"

echo "== cron line =="
if crontab -l 2>/dev/null | grep -E "$MARKER_RE"; then
  echo "OK: agent-monitor cron present"
else
  echo "MISSING: agent-monitor cron not installed"
fi

echo "== dry-run probe pass (delivers nothing) =="
cd "$COMPOSE_DIR"
# Capture the outcome + exit code. Exit 0 (healthy) / 10 (agent-unhealthy-but-ran) / disabled all mean the
# monitor RAN cleanly. Exit 20 (config-error) / 30 (probe-failure) mean the monitor itself is broken and the
# verifier must FAIL loudly — never pass a blind/unconfigured monitor as if it were healthy.
set +e
OUT="$(docker compose exec -T "$BACKEND_SERVICE" python manage.py run_agent_readiness_probe --dry-run --json)"
CODE=$?
set -e
echo "outcome: ${OUT:-<no output>}"
echo "exit code: ${CODE}"
case "$CODE" in
  0|10) echo "OK: monitor ran (band-dependent exit ${CODE})"; ;;
  20)   echo "FAIL: config_error (20) — monitor unconfigured (no base_url/keyring)"; exit 1; ;;
  30)   echo "FAIL: probe_failure (30) — monitor crashed"; exit 1; ;;
  50)   echo "WARN: overlap_refused (50) — a scheduled run held the lock; re-run to verify"; ;;
  *)    echo "NOTE: exit ${CODE} (0=healthy/disabled, 10=agent-unhealthy-but-ran)"; ;;
esac
