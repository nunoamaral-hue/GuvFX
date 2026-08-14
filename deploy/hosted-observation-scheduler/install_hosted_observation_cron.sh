#!/usr/bin/env bash
# BETA-READINESS — idempotent installer for the hosted-workspace observation/provisioning cron.
#
# Adds ONE cron line (the ordered allocate -> observe -> auto-arm chain) to the invoking user's
# crontab, identified by the end-anchored marker "# guvfx-hosted-observations". Re-running is a
# no-op if the line is already present. Removes it with --remove. Touches NOTHING else in the
# crontab (the monitor-chain / soak / strategy scheduler lines are preserved). Creates the log
# directory + file (writable by this user) and a logrotate rule. Prints no secrets.
#
# This only SCHEDULES an already-safe, DARK command: run_hosted_observations is a dormant no-op
# unless HOSTED_OBSERVATION_SCHEDULER_ENABLED is on, launches no MT5, sends no order, and arms
# nothing by itself (it self-gates + is a singleton + idempotent). See crontab.hosted-observations.
#
#   deploy/hosted-observation-scheduler/install_hosted_observation_cron.sh            # install (idempotent)
#   deploy/hosted-observation-scheduler/install_hosted_observation_cron.sh --remove   # uninstall
#   COMPOSE_DIR=/home/ubuntu/guvfx-prod LOG_DIR=/var/log/guvfx <script>               # overridable
set -euo pipefail

MARKER="# guvfx-hosted-observations"
# End-anchored ERE so a future sibling whose comment merely CONTAINS the marker as a prefix
# (e.g. "# guvfx-hosted-observations-v2") is never matched or collaterally removed. The managed
# cron line always ends with exactly MARKER.
MARKER_RE="# guvfx-hosted-observations$"
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
SCHEDULE="${SCHEDULE:-* * * * *}"

CRON_LINE="${SCHEDULE} cd ${COMPOSE_DIR} && docker compose exec -T ${BACKEND_SERVICE} python manage.py run_hosted_observations >> ${LOG_DIR}/hosted_observations.log 2>&1 ${MARKER}"

current_crontab() { crontab -l 2>/dev/null || true; }

if [ "${1:-}" = "--remove" ]; then
  if current_crontab | grep -qE "$MARKER_RE"; then
    current_crontab | grep -vE "$MARKER_RE" | crontab -
    echo "removed: hosted-observations cron line"
  else
    echo "noop: no hosted-observations cron line present"
  fi
  exit 0
fi

# Install path.
mkdir -p "$LOG_DIR" 2>/dev/null || true

# Ensure the cron log target EXISTS and is WRITABLE by this user. The cron redirect
# (">> hosted_observations.log") fails silently with "Permission denied" — and the command
# never executes — if LOG_DIR is root-owned and the log was never pre-created as this user.
# (This exact gap silently killed the monitor chain for weeks.) Provision it idempotently so a
# host rebuild cannot reintroduce the outage.
LOG_FILE="${LOG_DIR}/hosted_observations.log"
if [ ! -w "$LOG_FILE" ]; then
  if [ -w "$LOG_DIR" ]; then
    touch "$LOG_FILE"
  else
    sudo mkdir -p "$LOG_DIR"
    sudo touch "$LOG_FILE"
    sudo chown "$(id -un):$(id -gn)" "$LOG_FILE"
  fi
  echo "provisioned writable log: $LOG_FILE (owner $(id -un))"
fi
# Size-based rotation so the per-minute log can't grow unbounded.
if command -v sudo >/dev/null 2>&1 && [ ! -f /etc/logrotate.d/guvfx-hosted-observations ]; then
  sudo tee /etc/logrotate.d/guvfx-hosted-observations >/dev/null <<ROT || true
${LOG_DIR}/hosted_observations.log {
    size 20M
    rotate 5
    missingok
    notifempty
    copytruncate
}
ROT
  echo "provisioned logrotate: /etc/logrotate.d/guvfx-hosted-observations"
fi

if current_crontab | grep -qE "$MARKER_RE"; then
  echo "noop: hosted-observations cron already installed"
  current_crontab | grep -E "$MARKER_RE"
  exit 0
fi

{ current_crontab; echo "$CRON_LINE"; } | crontab -
echo "installed: hosted-observations cron (every: '${SCHEDULE}')"
echo "  -> log: ${LOG_DIR}/hosted_observations.log"
echo "  NOTE: DARK until HOSTED_OBSERVATION_SCHEDULER_ENABLED is set — installing this changes nothing yet."
crontab -l | grep -E "$MARKER_RE"
