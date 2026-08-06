#!/usr/bin/env bash
# VALIDATION-AGENT-MONITOR — idempotent installer for the readiness-probe scheduler cron.
#
# Adds ONE cron line (run_agent_readiness_probe, once per minute) to the invoking user's crontab,
# identified by the marker "# guvfx-agent-monitor". Re-running is a no-op if the line is already
# present. Removes it with --remove. Touches NOTHING else in the crontab (the post-trade
# monitor-chain line and the h1/m5/h4 scheduler lines are preserved). Prints no secrets.
#
# This only SCHEDULES an already-safe command. The scheduled pass is DARK unless
# VALIDATION_AGENT_MONITORING_ENABLED is on; even then it only probes :8791 (signed NEGOTIATE),
# writes the singleton AgentMonitorState row, and delivers an alert ONLY if an external sink is
# configured. It places no order, creates no validation attempt, and touches no customer account.
#
#   deploy/validation-agent-monitor/install_agent_monitor_cron.sh            # install (idempotent)
#   deploy/validation-agent-monitor/install_agent_monitor_cron.sh --remove   # uninstall
#   COMPOSE_DIR=/home/ubuntu/guvfx-prod LOG_DIR=/var/log/guvfx <script>       # overridable
set -euo pipefail

MARKER="# guvfx-agent-monitor"
# End-anchored ERE so a future sibling whose comment merely PREFIXES the marker (e.g.
# "# guvfx-agent-monitor-v2") is never matched or collaterally removed.
MARKER_RE="# guvfx-agent-monitor$"
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
SCHEDULE="${SCHEDULE:-* * * * *}"

CRON_LINE="${SCHEDULE} cd ${COMPOSE_DIR} && docker compose exec -T ${BACKEND_SERVICE} python manage.py run_agent_readiness_probe >> ${LOG_DIR}/agent_monitor.log 2>&1 ${MARKER}"

current_crontab() { crontab -l 2>/dev/null || true; }

if [ "${1:-}" = "--remove" ]; then
  if current_crontab | grep -qE "$MARKER_RE"; then
    # `|| true` so that removing the ONLY line (grep -vE yields an empty, exit-1 stream) does not abort under
    # `set -e` and misreport a successful removal as a failure.
    { current_crontab | grep -vE "$MARKER_RE" || true; } | crontab -
    echo "removed: agent-monitor cron line"
  else
    echo "noop: no agent-monitor cron line present"
  fi
  exit 0
fi

# Install path.
mkdir -p "$LOG_DIR" 2>/dev/null || true

# Ensure the cron log target EXISTS and is WRITABLE by this user — the cron redirect fails silently
# with "Permission denied" (and the probe never executes) if LOG_DIR is root-owned and the log was
# never pre-created as this user. This is the exact gap that once left the post-trade chain dead.
LOG_FILE="${LOG_DIR}/agent_monitor.log"
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
if command -v sudo >/dev/null 2>&1 && [ ! -f /etc/logrotate.d/guvfx-agent-monitor ]; then
  sudo tee /etc/logrotate.d/guvfx-agent-monitor >/dev/null <<ROT || true
${LOG_DIR}/agent_monitor.log {
    size 20M
    rotate 5
    missingok
    notifempty
    copytruncate
}
ROT
  echo "provisioned logrotate: /etc/logrotate.d/guvfx-agent-monitor"
fi

if current_crontab | grep -qE "$MARKER_RE"; then
  echo "noop: agent-monitor cron already installed"
  current_crontab | grep -E "$MARKER_RE"
  exit 0
fi

{ current_crontab; echo "$CRON_LINE"; } | crontab -
echo "installed: agent-monitor cron (every: '${SCHEDULE}')"
echo "  -> log: ${LOG_DIR}/agent_monitor.log"
echo "  NOTE: DARK until VALIDATION_AGENT_MONITORING_ENABLED=true is set on guvfx-backend."
crontab -l | grep -E "$MARKER_RE"
