#!/usr/bin/env bash
# GuvFX recovery certification -- OPERATOR provisioning of an isolated test cluster (PRIVILEGED).
#
# This is the OUT-OF-BAND, privileged step that is deliberately SEPARATE from the destructive
# certification harness (HIGH-1). It establishes the isolation boundary that the least-privilege
# harness (certify.sh) later only VERIFIES -- it does NOT run any backup/restore itself. Run it as a
# privileged local role (e.g. the Postgres superuser) on a THROWAWAY cluster only. It:
#   1. refuses if the live cluster is the declared production cluster (GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID);
#   2. creates a least-privilege NON-SUPERUSER role (LOGIN, CREATEDB, pg_read_all_stats) for the harness;
#   3. creates the positive isolation MARKER database, OWNED BY THE PRIVILEGED ROLE (NOT the harness
#      role) so the least-privilege harness can never drop the operator's trust anchor;
# and prints the exact environment to export before running certify.sh.
#
# The harness role's password is operator-supplied via GUVFX_RECOVERY_TESTER_PASSWORD (this script
# never invents or echoes a secret). `--teardown` drops the role + marker. ASCII-only.
#
# Usage:
#   GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID=<prod sysid> GUVFX_RECOVERY_TESTER_PASSWORD=<throwaway> \
#     bash ops/recovery/tests/setup_isolated_cluster.sh
#   ... later:  bash ops/recovery/tests/setup_isolated_cluster.sh --teardown
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REC="$(cd "$HERE/.." && pwd)"
# shellcheck source=../lib.sh
. "$REC/lib.sh"

TEARDOWN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --teardown) TEARDOWN=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) rec_die "unknown arg: $1" ;;
  esac
  shift
done

MAINT="${GUVFX_RECOVERY_ATTEST_MAINT_DB:-postgres}"
ROLE="${GUVFX_RECOVERY_TESTER_ROLE:-guvfx_recov_tester}"
MARKER="${GUVFX_RECOVERY_ISOLATION_MARKER_DB:-guvfx_recovery_isolated_ok}"
case "$ROLE"   in ""|*[!A-Za-z0-9_]*) rec_die "invalid tester role name '$ROLE'." ;; esac
case "$MARKER" in ""|*[!A-Za-z0-9_]*) rec_die "invalid marker db name '$MARKER'." ;; esac
rec_require_cmd psql createdb dropdb

sysid="$(psql -v ON_ERROR_STOP=1 -tAqc 'select system_identifier::text from pg_control_system()' "$MAINT")" \
  || rec_die "cannot read live cluster system_identifier on maintenance db '$MAINT'."
[ -n "$sysid" ] || rec_die "empty system_identifier."
if [ -n "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID:-}" ] && [ "$sysid" = "$GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID" ]; then
  rec_die "REFUSED: live cluster '$sysid' matches GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID -- will not provision a test boundary on production."
fi

if [ "$TEARDOWN" = "1" ]; then
  dropdb --if-exists "$MARKER" 2>/dev/null || true
  psql -v ON_ERROR_STOP=1 -q "$MAINT" -c "DROP ROLE IF EXISTS \"$ROLE\"" || true
  rec_log "teardown: dropped marker db '$MARKER' and role '$ROLE' (if present)."
  exit 0
fi

[ -n "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID:-}" ] \
  || rec_die "GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID must be set so production is positively identifiable before provisioning."
[ -n "${GUVFX_RECOVERY_TESTER_PASSWORD:-}" ] \
  || rec_die "GUVFX_RECOVERY_TESTER_PASSWORD must be set (operator-supplied throwaway password for the least-privilege harness role). This script never invents or prints a secret."
# Constrain the throwaway password charset so it can be embedded in SQL with NO quoting/injection risk
# and NEVER needs to appear on argv (it is sent on psql stdin below).
case "$GUVFX_RECOVERY_TESTER_PASSWORD" in
  *[!A-Za-z0-9_-]*|'') rec_die "GUVFX_RECOVERY_TESTER_PASSWORD must match [A-Za-z0-9_-] (throwaway, >=8 chars recommended)." ;;
esac

# Create / reset the least-privilege harness role. NOSUPERUSER + CREATEDB (to make scratch targets it
# owns) + pg_read_all_stats (to read pg_control_system). No other privileges. The password travels on
# psql STDIN (heredoc), never on argv/ps, and is never echoed. ALTER-if-exists avoids a DROP ROLE that
# would fail when the role still owns scratch databases from a prior run.
psql -v ON_ERROR_STOP=1 -q "$MAINT" <<SQL
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$ROLE') AS role_exists \gset
\if :role_exists
  ALTER ROLE "$ROLE" LOGIN NOSUPERUSER NOCREATEROLE CREATEDB PASSWORD '$GUVFX_RECOVERY_TESTER_PASSWORD';
\else
  CREATE ROLE "$ROLE" LOGIN NOSUPERUSER NOCREATEROLE CREATEDB PASSWORD '$GUVFX_RECOVERY_TESTER_PASSWORD';
\endif
GRANT pg_read_all_stats TO "$ROLE";
SQL

# Plant the positive isolation marker, OWNED BY THE CURRENT PRIVILEGED ROLE (not the harness role),
# so certify.sh (running as the least-privilege role) can never drop it.
if [ "$(psql -tAqc "select count(*) from pg_database where datname='$MARKER'" "$MAINT")" != "1" ]; then
  createdb "$MARKER"
fi

cat >&2 <<EOF
[setup] isolated test cluster provisioned.
[setup]   live system_identifier : $sysid
[setup]   least-privilege role    : $ROLE  (NOSUPERUSER, CREATEDB, pg_read_all_stats)
[setup]   isolation marker db     : $MARKER  (owned by the privileged provisioning role)
[setup]
[setup] Export this environment, then run certify.sh (do NOT export the password into evidence):
[setup]   export GUVFX_RECOVERY_ATTEST_ISOLATED=1
[setup]   export GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID='${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID}'
[setup]   export GUVFX_RECOVERY_ISOLATED_CLUSTER_ID='$sysid'
[setup]   export GUVFX_RECOVERY_ISOLATION_MARKER_DB='$MARKER'
[setup]   export PGUSER='$ROLE' PGPASSWORD='<the throwaway password you set>'
[setup]   export GUVFX_BACKUP_PASSPHRASE='<throwaway>'
[setup]   GUVFX_RECOVERY_ATTEST_ISOLATED=1 bash ops/recovery/tests/certify.sh
EOF
