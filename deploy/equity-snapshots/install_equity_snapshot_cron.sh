#!/usr/bin/env bash
# Install the equity-snapshot-ledger cron (idempotent; mirrors deploy/soak-report/install_soak_cron.sh).
# The command itself is DARK unless EQUITY_SNAPSHOT_LEDGER_ENABLED=1 in the backend container env, so installing
# the cron is safe on its own — it no-ops until the flag is set.
set -euo pipefail
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
SCHEDULE="${SCHEDULE:-*/5 * * * *}"
MARKER="# guvfx-equity-snapshots"
sudo mkdir -p "$LOG_DIR"
CRON_LINE="${SCHEDULE} cd ${COMPOSE_DIR} && docker compose exec -T ${BACKEND_SERVICE} python manage.py capture_equity_snapshots >> ${LOG_DIR}/equity_snapshots.log 2>&1 ${MARKER}"
( crontab -l 2>/dev/null | grep -v "$MARKER" || true; echo "$CRON_LINE" ) | crontab -
echo "installed equity-snapshot cron: ${SCHEDULE} (DARK until EQUITY_SNAPSHOT_LEDGER_ENABLED=1)"
