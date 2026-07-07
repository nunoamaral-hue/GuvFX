#!/usr/bin/env bash
# GFX-PKT-BROKER-SYMBOL-DEPLOY-AND-SYNC — idempotent installer for the nightly broker-symbol sync cron.
#
# Adds ONE nightly cron line per account (marker "# guvfx-broker-sync") to the invoking user's
# crontab. Re-running is a no-op if already present. Removes with --remove. Touches NOTHING else
# (the h1/m5/h4 scheduler and monitor-chain lines are preserved). Prints no secrets.
#
# This only SCHEDULES an already-safe command: sync_broker_instruments places no order, sends no
# Telegram, and only upserts the symbol cache (stale symbols -> enabled=False, never deleted).
#
#   ACCOUNTS="1" deploy/broker-symbol-sync/install_broker_sync_cron.sh            # install (idempotent)
#   deploy/broker-symbol-sync/install_broker_sync_cron.sh --remove                # uninstall all
#   COMPOSE_DIR=/home/ubuntu/guvfx-prod LOG_DIR=/var/log/guvfx SCHEDULE="17 2 * * *" <script>
set -euo pipefail

MARKER="# guvfx-broker-sync"
# End-anchored ERE so a future sibling marker (e.g. "# guvfx-broker-sync-v2") is never collaterally matched.
MARKER_RE="# guvfx-broker-sync$"
COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
SCHEDULE="${SCHEDULE:-17 2 * * *}"
ACCOUNTS="${ACCOUNTS:-1}"

current_crontab() { crontab -l 2>/dev/null || true; }

if [ "${1:-}" = "--remove" ]; then
  if current_crontab | grep -qE "$MARKER_RE"; then
    current_crontab | grep -vE "$MARKER_RE" | crontab -
    echo "removed: broker-sync cron line(s)"
  else
    echo "noop: no broker-sync cron line present"
  fi
  exit 0
fi

# Install path.
mkdir -p "$LOG_DIR"

if current_crontab | grep -qE "$MARKER_RE"; then
  echo "noop: broker-sync cron already installed"
  current_crontab | grep -E "$MARKER_RE"
  exit 0
fi

# Build one nightly line per account (all share the same MARKER so --remove clears them together).
NEW_LINES=""
for acct in $ACCOUNTS; do
  NEW_LINES="${NEW_LINES}${SCHEDULE} cd ${COMPOSE_DIR} && docker compose exec -T ${BACKEND_SERVICE} python manage.py sync_broker_instruments --account ${acct} >> ${LOG_DIR}/broker_sync.log 2>&1 ${MARKER}"$'\n'
done

{ current_crontab; printf '%s' "$NEW_LINES"; } | crontab -
echo "installed: broker-sync cron for account(s) '${ACCOUNTS}' (schedule: '${SCHEDULE}')"
echo "  -> log: ${LOG_DIR}/broker_sync.log"
crontab -l | grep -E "$MARKER_RE"
