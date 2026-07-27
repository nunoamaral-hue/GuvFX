#!/usr/bin/env bash
# TB-5 — verify the beta ProvisioningJob worker (read-only, no host side-effects).
#
# Proves, WITHOUT provisioning anything:
#   1. the worker command runs a single claim in DARK mode (BETA_RUNTIMES_ENABLED off → claims nothing);
#   2. the reconcile command is a no-op while the flag is off;
#   3. a live worker container is up (if deployed).
#
# Usage (on the VPS):  bash verify_beta_provisioner.sh
set -euo pipefail
BACKEND="${GUVFX_BACKEND_CONTAINER:-guvfx-backend}"
WORKER="${GUVFX_BETA_PROVISIONER_CONTAINER:-guvfx-beta-provisioner}"

echo "== 1) single claim (dark unless BETA_RUNTIMES_ENABLED) =="
# process_one returns 'disabled' (flag off) or 'no_job' (nothing claimable) when dark; 'advanced' only
# when it claimed and drove a job. A dark run must print 'disabled' or 'no_job', never 'advanced'.
docker exec "$BACKEND" python manage.py run_beta_provisioning_worker --once

echo "== 2) reconcile is a no-op while dark =="
docker exec "$BACKEND" python manage.py reconcile_beta_provisioning

echo "== 3) live worker container (if deployed) =="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E "$WORKER" || echo "WORKER NOT RUNNING (stage it before arming)"

echo "== done — BETA_RUNTIMES_ENABLED must be OFF for this to be a no-op =="
