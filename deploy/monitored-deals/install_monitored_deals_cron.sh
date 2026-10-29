#!/usr/bin/env bash
# Install the monitored deal-history ingest cron (idempotent; mirrors deploy/equity-snapshots/install_equity_snapshot_cron.sh).
# The command itself is DARK unless MONITORED_DEALS_INGEST_ENABLED=1 in the backend container env, so installing the
# cron is safe on its own — it no-ops until the flag is set. The enqueued jobs are read-only (deals ingest); no orders.
#
# Install:  deploy/monitored-deals/install_monitored_deals_cron.sh
# Remove:   deploy/monitored-deals/install_monitored_deals_cron.sh --remove
set -euo pipefail
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
SCHEDULE="${SCHEDULE:-*/15 * * * *}"
MARKER="# guvfx-monitored-deals"

if [[ "${1:-}" == "--remove" ]]; then
  ( crontab -l 2>/dev/null | grep -v "$MARKER" || true ) | crontab -
  echo "removed monitored-deals cron"
  exit 0
fi

sudo mkdir -p "$LOG_DIR"
# Pre-create the log file owned by the cron user (mirrors the FX-ingestion log-dir fix: a root-owned 0755 /var/log/guvfx
# silently blocks a non-root cron user from creating its own log, so scheduled runs produce no output).
sudo touch "${LOG_DIR}/monitored_deals.log"
sudo chown "$(id -un)":"$(id -gn)" "${LOG_DIR}/monitored_deals.log" || true
sudo chmod 0640 "${LOG_DIR}/monitored_deals.log" || true

CRON_LINE="${SCHEDULE} cd ${COMPOSE_DIR} && docker compose exec -T ${BACKEND_SERVICE} python manage.py ingest_monitored_deals >> ${LOG_DIR}/monitored_deals.log 2>&1 ${MARKER}"
( crontab -l 2>/dev/null | grep -v "$MARKER" || true; echo "$CRON_LINE" ) | crontab -
echo "installed monitored-deals cron: ${SCHEDULE} (DARK until MONITORED_DEALS_INGEST_ENABLED=1)"
