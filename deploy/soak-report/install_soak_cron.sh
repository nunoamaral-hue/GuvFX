#!/usr/bin/env bash
# Install the WS-G soak-report cron (idempotent; mirrors monitor-scheduler).
set -euo pipefail
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
SCHEDULE="${SCHEDULE:-0 * * * *}"
MARKER="# guvfx-soak-report"
sudo mkdir -p "$LOG_DIR"
CRON_LINE="${SCHEDULE} cd ${COMPOSE_DIR} && docker compose exec -T ${BACKEND_SERVICE} python manage.py soak_report >> ${LOG_DIR}/soak_report.log 2>&1 ${MARKER}"
( crontab -l 2>/dev/null | grep -v "$MARKER" || true; echo "$CRON_LINE" ) | crontab -
echo "installed soak-report cron: ${SCHEDULE}"
