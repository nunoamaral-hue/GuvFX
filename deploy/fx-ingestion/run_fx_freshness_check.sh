#!/usr/bin/env bash
# run_fx_freshness_check.sh - FX ingestion freshness monitor (read-only).
#
# Runs the read-only `fx_ingestion_health` command (priority-aware: BOE then FED_H10) and decides an alert:
#   - status != HEALTHY (STALE/EMPTY)                 -> FX_FRESHNESS_ALERT, exit 2
#   - HEALTHY but age_days >= WARN_AGE (default 5)    -> FX_FRESHNESS_ALERT, exit 3  (before the 7-day refusal)
#   - health command failed                            -> FX_FRESHNESS_ALERT, exit non-zero
#   - otherwise                                        -> single OK line, exit 0 (quiet when healthy and fresh)
#
# "Effective date not advancing" is covered transitively: if ingestion stalls, age_days grows past WARN_AGE and
# this fires before the hard 7-day staleness refusal removes GBP conversion. Read-only: no write, no order, no
# FRED. The FX_FRESHNESS_ALERT lines + non-zero exit are the actionable signal; wiring their delivery to the
# existing ops alert sink (AGENT_ALERT_SINK) is a separate, reviewed follow-on.
set -uo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/home/ubuntu/guvfx-prod}"
BACKEND_SERVICE="${BACKEND_SERVICE:-guvfx-backend}"
WARN_AGE="${WARN_AGE:-5}"                # alert when age_days >= this (staleness refusal is 7)

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) fx-freshness $*"; }

j="$(cd "$COMPOSE_DIR" && docker compose exec -T "$BACKEND_SERVICE" python manage.py fx_ingestion_health 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  log "FX_FRESHNESS_ALERT health_command_failed rc=$rc $j"
  exit "$rc"
fi

val() { printf '%s' "$j" | sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" | head -1; }
status="$(val status)"
age="$(val age_days)"
src="$(val source)"
eff="$(val latest_effective_date)"

if [ "$status" != "HEALTHY" ]; then
  log "FX_FRESHNESS_ALERT status=$status source=$src age_days=$age effective=$eff GBP->USD conversion at risk"
  exit 2
fi
if [ -n "$age" ] && [ "$age" -ge "$WARN_AGE" ] 2>/dev/null; then
  log "FX_FRESHNESS_ALERT status=HEALTHY_but_aging age_days=$age ge ${WARN_AGE} source=$src effective=$eff ingestion may have stalled"
  exit 3
fi
log "OK status=HEALTHY source=$src age_days=$age effective=$eff"
exit 0
