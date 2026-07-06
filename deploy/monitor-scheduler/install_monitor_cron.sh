#!/usr/bin/env bash
# E3-MONITOR-SCHEDULING — idempotent installer for the post-trade monitor-chain cron.
#
# Adds ONE cron line (the recommended ordered chain) to the invoking user's crontab, identified
# by the marker "# guvfx-monitor-chain". Re-running is a no-op if the line is already present.
# Removes it with --remove. Touches NOTHING else in the crontab (the existing h1/m5/h4 scheduler
# lines are preserved). Creates the log directory if absent. Prints no secrets.
#
# This only SCHEDULES an already-safe command: the chain places no order, sends no Telegram
# (dispatch dry-run + flag-gated OFF by default), and publishes nothing to WIMS. See README.md.
#
#   deploy/monitor-scheduler/install_monitor_cron.sh            # install (idempotent)
#   deploy/monitor-scheduler/install_monitor_cron.sh --remove   # uninstall
#   COMPOSE_DIR=/home/ubuntu/guvfx-prod LOG_DIR=/var/log/guvfx <script>   # overridable
set -euo pipefail

MARKER="# guvfx-monitor-chain"
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
SCHEDULE="${SCHEDULE:-* * * * *}"

CRON_LINE="${SCHEDULE} cd ${COMPOSE_DIR} && docker compose exec -T ${BACKEND_SERVICE} python manage.py run_monitor_chain >> ${LOG_DIR}/monitor_chain.log 2>&1 ${MARKER}"

current_crontab() { crontab -l 2>/dev/null || true; }

if [ "${1:-}" = "--remove" ]; then
  if current_crontab | grep -qF "$MARKER"; then
    current_crontab | grep -vF "$MARKER" | crontab -
    echo "removed: monitor-chain cron line"
  else
    echo "noop: no monitor-chain cron line present"
  fi
  exit 0
fi

# Install path.
mkdir -p "$LOG_DIR"

if current_crontab | grep -qF "$MARKER"; then
  echo "noop: monitor-chain cron already installed"
  current_crontab | grep -F "$MARKER"
  exit 0
fi

{ current_crontab; echo "$CRON_LINE"; } | crontab -
echo "installed: monitor-chain cron (every: '${SCHEDULE}')"
echo "  -> log: ${LOG_DIR}/monitor_chain.log"
crontab -l | grep -F "$MARKER"
