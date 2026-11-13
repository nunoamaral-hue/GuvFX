#!/usr/bin/env bash
# DASHBOARD AUTO-MONITORING ENROLMENT (ADR-0103) — idempotent installer for the enrolment cron.
#
# Adds ONE cron line (the idempotent `reconcile_monitoring_enrolment reconcile --apply` pass) to
# the invoking user's crontab, identified by the end-anchored marker "# guvfx-monitoring-enrolment".
# Re-running is a no-op if the line is already present. Removes it with --remove. Touches NOTHING
# else in the crontab (the hosted-observation / monitor-chain / soak lines are preserved). Creates
# the log directory + file (writable by this user) and a logrotate rule. Prints no secrets.
#
# This only SCHEDULES an already-safe, TWO-LEVEL DARK command: the reconciler's --apply is a
# dormant no-op unless BOTH MONITORING_GATEWAY_ENABLED and MONITORING_ENROLMENT_RECONCILE_ENABLED
# are on; even then it only reserves/registers MonitoringRoute rows + writes alerts, never marks a
# route READY, contacts a host, or places an order. See crontab.monitoring-enrolment.
#
#   deploy/monitoring-enrolment-scheduler/install_monitoring_enrolment_cron.sh            # install (idempotent)
#   deploy/monitoring-enrolment-scheduler/install_monitoring_enrolment_cron.sh --remove   # uninstall
#   COMPOSE_DIR=/home/ubuntu/guvfx-prod LOG_DIR=/var/log/guvfx <script>                    # overridable
set -euo pipefail

MARKER="# guvfx-monitoring-enrolment"
# End-anchored ERE so a future sibling whose comment merely CONTAINS the marker as a prefix
# (e.g. "# guvfx-monitoring-enrolment-v2") is never matched or collaterally removed.
MARKER_RE="# guvfx-monitoring-enrolment$"
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
SCHEDULE="${SCHEDULE:-*/5 * * * *}"

CRON_LINE="${SCHEDULE} cd ${COMPOSE_DIR} && docker compose exec -T ${BACKEND_SERVICE} python manage.py reconcile_monitoring_enrolment reconcile --apply >> ${LOG_DIR}/monitoring_enrolment.log 2>&1 ${MARKER}"

current_crontab() { crontab -l 2>/dev/null || true; }

if [ "${1:-}" = "--remove" ]; then
  if current_crontab | grep -qE "$MARKER_RE"; then
    current_crontab | grep -vE "$MARKER_RE" | crontab -
    echo "removed: monitoring-enrolment cron line"
  else
    echo "noop: no monitoring-enrolment cron line present"
  fi
  exit 0
fi

# Install path.
mkdir -p "$LOG_DIR" 2>/dev/null || true

# Ensure the cron log target EXISTS and is WRITABLE by this user. The cron redirect
# (">> monitoring_enrolment.log") fails silently with "Permission denied" -- and the command never
# executes -- if LOG_DIR is root-owned and the log was never pre-created as this user. Provision it
# idempotently so a host rebuild cannot reintroduce that silent outage.
LOG_FILE="${LOG_DIR}/monitoring_enrolment.log"
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
# Size-based rotation so the per-5-minute log can't grow unbounded.
if command -v sudo >/dev/null 2>&1 && [ ! -f /etc/logrotate.d/guvfx-monitoring-enrolment ]; then
  sudo tee /etc/logrotate.d/guvfx-monitoring-enrolment >/dev/null <<ROT || true
${LOG_DIR}/monitoring_enrolment.log {
    size 20M
    rotate 5
    missingok
    notifempty
    copytruncate
}
ROT
  echo "provisioned logrotate: /etc/logrotate.d/guvfx-monitoring-enrolment"
fi

if current_crontab | grep -qE "$MARKER_RE"; then
  echo "noop: monitoring-enrolment cron already installed"
  current_crontab | grep -E "$MARKER_RE"
  exit 0
fi

{ current_crontab; echo "$CRON_LINE"; } | crontab -
echo "installed: monitoring-enrolment cron (every: '${SCHEDULE}')"
echo "  -> log: ${LOG_DIR}/monitoring_enrolment.log"
echo "  NOTE: TWO-LEVEL DARK until MONITORING_GATEWAY_ENABLED and MONITORING_ENROLMENT_RECONCILE_ENABLED"
echo "        are both set -- installing this changes nothing yet."
crontab -l | grep -E "$MARKER_RE"
