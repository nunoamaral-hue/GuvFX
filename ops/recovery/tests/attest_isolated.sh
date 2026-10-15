# shellcheck shell=bash
# GuvFX recovery certification -- INDEPENDENT isolated-cluster attestation (HIGH-1).
#
# This guard establishes, BEFORE any mutating certification step, that the harness is pointed at a
# genuinely isolated, operator-provisioned test cluster -- using evidence the DESTRUCTIVE HARNESS
# CANNOT MANUFACTURE on an unknown cluster. Specifically it:
#   - NEVER derives the authorized cluster id from whatever it happens to connect to; the operator
#     INDEPENDENTLY attests GUVFX_RECOVERY_ISOLATED_CLUSTER_ID and the guard only VERIFIES the live
#     cluster matches it;
#   - NEVER creates the isolation marker database (the operator plants it out-of-band);
#   - NEVER treats "localhost" (or a local socket) as proof of isolation -- a production cluster is
#     also local on its VPS;
#   - REQUIRES production to be positively identifiable (GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID is
#     mandatory here) so a denylist-absent run cannot self-attest production;
#   - REQUIRES a least-privilege, NON-SUPERUSER connecting role with no implicit rights over other
#     databases.
#
# Prints a single-line precise reason and returns 2 on any failure; returns 0 only when isolation is
# INDEPENDENTLY established. Pure check: performs NO mutation. Sourced by certify.sh and callable
# directly by the HIGH-1 regressions with manipulated env.

# Catalog reads run against an explicit maintenance DB (cluster-global answers), never a silent
# libpq default that could select an application/production database.
attest_psql() { psql -v ON_ERROR_STOP=1 -tAqc "$1" "${GUVFX_RECOVERY_ATTEST_MAINT_DB:-postgres}" 2>/dev/null; }

attest_independent_test_cluster() {
  local live su marker mn ownsprod
  [ "${GUVFX_RECOVERY_ATTEST_ISOLATED:-0}" = "1" ] \
    || { echo "REFUSED: GUVFX_RECOVERY_ATTEST_ISOLATED is not set to 1 (operator must attest a throwaway cluster)"; return 2; }
  # Explicit, loopback connection -- never a silent libpq default that could select production.
  [ -n "${PGHOST:-}" ] \
    || { echo "REFUSED: PGHOST must be explicitly set to the isolated test cluster (no libpq default)"; return 2; }
  case "${PGHOST}" in
    localhost|127.0.0.1|::1|/*) : ;;
    *) echo "REFUSED: PGHOST='${PGHOST}' is not a local/loopback socket or address"; return 2 ;;
  esac
  if [ -n "${PGHOSTADDR:-}" ]; then
    case "${PGHOSTADDR}" in 127.0.0.1|::1) : ;; *) echo "REFUSED: PGHOSTADDR='${PGHOSTADDR}' is not loopback"; return 2 ;; esac
  fi
  # Production MUST be positively identifiable (mandatory denylist -- closes the 'denylist absent' hole).
  [ -n "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID:-}" ] \
    || { echo "REFUSED: GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID is not set -- production must be positively identifiable before certifying"; return 2; }
  # Operator must INDEPENDENTLY attest the isolated cluster id (the guard never derives it from the live connection).
  [ -n "${GUVFX_RECOVERY_ISOLATED_CLUSTER_ID:-}" ] \
    || { echo "REFUSED: GUVFX_RECOVERY_ISOLATED_CLUSTER_ID is not set -- the operator must independently attest the test cluster's system_identifier"; return 2; }
  live="$(attest_psql 'select system_identifier::text from pg_control_system()')" \
    || { echo "REFUSED: cannot read the live cluster system_identifier (connection/privilege)"; return 2; }
  [ -n "$live" ] || { echo "REFUSED: empty live system_identifier"; return 2; }
  [ "$live" != "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID}" ] \
    || { echo "REFUSED: live cluster IS the declared production cluster (system_identifier match) -- will not certify against production"; return 2; }
  [ "$live" = "${GUVFX_RECOVERY_ISOLATED_CLUSTER_ID}" ] \
    || { echo "REFUSED: live cluster system_identifier '$live' != attested GUVFX_RECOVERY_ISOLATED_CLUSTER_ID '${GUVFX_RECOVERY_ISOLATED_CLUSTER_ID}' -- wrong or unverified cluster"; return 2; }
  # Least privilege: a superuser could create/drop/mutate ANY database (including production) -- refuse.
  su="$(attest_psql 'select rolsuper from pg_roles where rolname=current_user')" \
    || { echo "REFUSED: cannot read connecting role privileges"; return 2; }
  [ "$su" = "f" ] \
    || { echo "REFUSED: connecting role is a SUPERUSER -- certification requires a least-privilege, non-superuser test role"; return 2; }
  # Pre-existing isolation marker the harness did NOT create.
  marker="${GUVFX_RECOVERY_ISOLATION_MARKER_DB:-guvfx_recovery_isolated_ok}"
  case "$marker" in ""|*[!A-Za-z0-9_]*) echo "REFUSED: invalid GUVFX_RECOVERY_ISOLATION_MARKER_DB '$marker'"; return 2 ;; esac
  mn="$(attest_psql "select count(*) from pg_database where datname='$marker'")" \
    || { echo "REFUSED: cannot check the isolation marker database"; return 2; }
  [ "${mn:-0}" = "1" ] \
    || { echo "REFUSED: pre-existing isolation marker database '$marker' is ABSENT -- the harness must NOT create it; the operator plants it out-of-band on the isolated cluster (setup_isolated_cluster.sh)"; return 2; }
  # The test role must not OWN the declared production application database if one exists here.
  if [ -n "${GUVFX_RECOVERY_PRODUCTION_DB:-}" ]; then
    ownsprod="$(attest_psql "select count(*) from pg_database d join pg_roles r on r.oid=d.datdba where d.datname='${GUVFX_RECOVERY_PRODUCTION_DB}' and r.rolname=current_user")" \
      || { echo "REFUSED: cannot check production-db ownership"; return 2; }
    [ "${ownsprod:-0}" = "0" ] \
      || { echo "REFUSED: test role OWNS the declared production database '${GUVFX_RECOVERY_PRODUCTION_DB}'"; return 2; }
  fi
  return 0
}
