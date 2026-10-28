#!/usr/bin/env bash
# Install (or --remove) the GuvFX FX ingestion + freshness-monitor cron lines. Idempotent; mirrors
# deploy/equity-snapshots/install_equity_snapshot_cron.sh. Identified by the end-anchored markers below.
#
# Safe to run: the ingestion is append-only/idempotent and the monitor is read-only. No secret is written to the
# crontab. Re-running replaces the GuvFX FX lines in place.
#
#   Install:  deploy/fx-ingestion/install_fx_ingestion_cron.sh
#   Remove:   deploy/fx-ingestion/install_fx_ingestion_cron.sh --remove
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/ubuntu/guvfx-app}"
LOG_DIR="${LOG_DIR:-/var/log/guvfx}"
INGEST="${REPO_DIR}/deploy/fx-ingestion/run_fx_boe_ingest.sh"
FRESH="${REPO_DIR}/deploy/fx-ingestion/run_fx_freshness_check.sh"

M_INGEST="# guvfx-fetch-fx-boe"
M_FRESH_PM="# guvfx-fx-freshness-pm"
M_FRESH_AM="# guvfx-fx-freshness-am"

mkdir -p "$LOG_DIR" 2>/dev/null || sudo mkdir -p "$LOG_DIR"
chmod +x "$INGEST" "$FRESH"

strip() { crontab -l 2>/dev/null | grep -vF "$M_INGEST" | grep -vF "$M_FRESH_PM" | grep -vF "$M_FRESH_AM" || true; }

if [ "${1:-}" = "--remove" ]; then
  strip | crontab -
  echo "removed GuvFX FX ingestion cron lines"
  exit 0
fi

L_INGEST="0 15 * * * ${INGEST} >> ${LOG_DIR}/fetch_fx_boe.log 2>&1 ${M_INGEST}"
L_FRESH_PM="0 16 * * * ${FRESH} >> ${LOG_DIR}/fx_freshness.log 2>&1 ${M_FRESH_PM}"
L_FRESH_AM="0 8 * * * ${FRESH} >> ${LOG_DIR}/fx_freshness.log 2>&1 ${M_FRESH_AM}"

( strip; echo "$L_INGEST"; echo "$L_FRESH_PM"; echo "$L_FRESH_AM" ) | crontab -
echo "installed GuvFX FX ingestion cron:"
echo "  $L_INGEST"
echo "  $L_FRESH_PM"
echo "  $L_FRESH_AM"
