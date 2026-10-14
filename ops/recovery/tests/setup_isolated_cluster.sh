#!/usr/bin/env bash
# GuvFX recovery certification -- DEDICATED EPHEMERAL CLUSTER provisioner (PRIVILEGED; R3).
#
# R3 trust boundary (the HIGH finding): earlier revisions created a role + marker on a SHARED,
# pre-existing PostgreSQL cluster and, on --teardown, issued destructive DROP ROLE / DROP DATABASE
# before the production-cluster check -- so a mis-run could mutate an unidentified (even production)
# cluster, and a placeholder production id proved nothing about disposability.
#
# This revision instead PROVISIONS ITS OWN disposable cluster with `initdb`: a private data dir,
# a private unix socket, and NO TCP listener (listen_addresses=''). Disposability is therefore
# INTRINSIC -- we created the whole cluster, so its system_identifier is brand-new and it shares
# nothing with any existing/production cluster. The provisioner NEVER issues DDL against a shared
# cluster: the only CREATE ROLE / CREATE DATABASE run inside the fresh cluster we own, and --down
# simply stops that postmaster and removes the files WE created (verified by an ownership stamp).
#
# Invariants (A-J): production id is MANDATORY on every privileged path (setup AND teardown);
# both paths enforce the SAME positively-verified boundary (our ownership stamp under a controlled
# root); an unidentified / unstamped / unknown cluster is refused; a placeholder production id alone
# authorizes nothing (authorization is the stamp, not an id comparison); the provisioner connects
# ONLY via its own explicit socket (PGSERVICE / libpq defaults are cleared, no TCP); the connecting
# role and the cluster's own system_identifier are verified; teardown removes ONLY our files and can
# never touch an unrelated role/database/data-dir; a failed/interrupted setup cleans up ONLY the
# partial ephemeral cluster it just created, never a shared target. ASCII-only.
#
# Usage:
#   GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID=<prod sysid> GUVFX_RECOVERY_TESTER_PASSWORD=<throwaway> \
#   GUVFX_RECOVERY_EPHEMERAL_ROOT=/path/to/new/dir  bash ops/recovery/tests/setup_isolated_cluster.sh --up
#   ... it prints the env to export, then run certify.sh ...
#   GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID=<prod sysid> \
#   GUVFX_RECOVERY_EPHEMERAL_ROOT=/path/to/that/dir  bash ops/recovery/tests/setup_isolated_cluster.sh --down
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REC="$(cd "$HERE/.." && pwd)"
# shellcheck source=../lib.sh
. "$REC/lib.sh"

MODE="up"
while [ $# -gt 0 ]; do
  case "$1" in
    --up) MODE="up" ;;
    --down|--teardown) MODE="down" ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) rec_die "unknown arg: $1" ;;
  esac
  shift
done

rec_require_cmd initdb pg_ctl postgres psql pg_controldata

# F -- NEUTRALISE every inherited libpq connection variable so PGSERVICE / PGHOST / PGPORT / PGHOSTADDR
# / a ~/.pg_service entry can never silently redirect a privileged operation to another (e.g.
# production) cluster. This MUST include PGHOSTADDR: a non-empty hostaddr takes precedence in libpq and
# forces a TCP connect to that IP, demoting even an explicit `-h /socket` to auth/SSL use only -- so an
# inherited PGHOSTADDR would redirect our connections off the private socket. Every connection below is
# made EXPLICITLY to our own private socket with -h/-p/-U. (Unsetting, not setting to "", because an
# empty PGPORT is itself an invalid value that breaks initdb's bootstrap.) As a STRUCTURAL backstop,
# --up also verifies the connection actually landed on the cluster we just initdb'd (offline vs live
# system_identifier) BEFORE any DDL, so a redirect via any missed variable still fails closed.
unset PGSERVICE PGSERVICEFILE PGSYSCONFDIR PGHOST PGHOSTADDR PGPORT PGUSER PGDATABASE PGPASSWORD PGOPTIONS 2>/dev/null || true
# macOS workaround: without a fixed locale the postmaster can abort at startup with "became
# multithreaded during startup" (a CoreFoundation locale-lookup quirk). A fixed C locale avoids it;
# harmless for a throwaway cluster. (No effect on Linux, where production runs.)
export LC_ALL=C

# --- names / paths (deterministic from the controlled root) ----------------
ROLE="${GUVFX_RECOVERY_TESTER_ROLE:-guvfx_recov_tester}"
MARKER="${GUVFX_RECOVERY_ISOLATION_MARKER_DB:-guvfx_recovery_isolated_ok}"
BOOT="${GUVFX_RECOVERY_EPHEMERAL_BOOT:-guvfx_eph_boot}"
case "$ROLE"   in ""|*[!A-Za-z0-9_]*) rec_die "invalid tester role name '$ROLE'." ;; esac
case "$MARKER" in ""|*[!A-Za-z0-9_]*) rec_die "invalid marker db name '$MARKER'." ;; esac
case "$BOOT"   in ""|*[!A-Za-z0-9_]*) rec_die "invalid bootstrap role name '$BOOT'." ;; esac

# A -- production id is MANDATORY on EVERY privileged path (setup AND teardown). It is defence in
# depth here (the cluster is ours by construction), but it must never be omittable on a mutating path.
[ -n "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID:-}" ] \
  || rec_die "GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID must be set on EVERY privileged path (setup and teardown) so production is positively identifiable. Fail-closed."

# The controlled root owns the whole ephemeral environment lifecycle.
ROOT="${GUVFX_RECOVERY_EPHEMERAL_ROOT:-}"
if [ "$MODE" = "up" ] && [ -z "$ROOT" ]; then
  ROOT="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_eph.XXXXXX")"
fi
[ -n "$ROOT" ] || rec_die "GUVFX_RECOVERY_EPHEMERAL_ROOT must be set (the controlled root of the disposable cluster). Fail-closed."
case "$ROOT" in /*) : ;; *) rec_die "GUVFX_RECOVERY_EPHEMERAL_ROOT must be an absolute path. Fail-closed." ;; esac
PGDATA="$ROOT/pgdata"; SOCKDIR="$ROOT/sock"; STATE="$ROOT/.guvfx_ephemeral"
PORT="${GUVFX_RECOVERY_EPHEMERAL_PORT:-$(( 54000 + ($$ % 1000) ))}"

# Every provisioner connection goes EXPLICITLY to our own socket, with libpq/service env CLEARED, so
# PGSERVICE or inherited PGHOST/PGPORT can never silently redirect the operation to another cluster. [F,G]
eph_psql() { local db="$1" sql="$2"; psql -v ON_ERROR_STOP=1 -tAqc "$sql" -h "$SOCKDIR" -p "$PORT" -U "$BOOT" "$db"; }

# Offline data-dir system_identifier (works whether or not a postmaster is running).
eph_offline_sysid() {
  if command -v pg_controldata >/dev/null 2>&1; then
    pg_controldata "$PGDATA" 2>/dev/null | sed -n 's/.*system identifier: *\([0-9]\{1,\}\).*/\1/p' | head -1
  fi
}

# Positive proof that PGDATA is OUR freshly-provisioned ephemeral cluster (not a shared/production
# data dir, not an adopted pre-existing one). Required by BOTH --up (post-init) and --down. [B,C,E,H,I]
assert_our_ephemeral_dir() {
  local token
  [ -d "$PGDATA" ] || rec_die "REFUSED: ephemeral data dir '$PGDATA' does not exist. Fail-closed."
  [ "$PGDATA" = "$ROOT/pgdata" ] || rec_die "REFUSED: '$PGDATA' is not the controlled-root data dir. Fail-closed."
  [ -f "$PGDATA/PG_VERSION" ] || rec_die "REFUSED: '$PGDATA' is not a PostgreSQL data directory. Fail-closed."
  [ -f "$PGDATA/.guvfx_ephemeral_owner" ] || rec_die "REFUSED: '$PGDATA' carries no GuvFX ephemeral ownership stamp -- not a cluster THIS tool provisioned. Will not touch it. Fail-closed."
  [ -f "$STATE" ] || rec_die "REFUSED: no ephemeral state file at '$STATE'. Fail-closed."
  token="$(cat "$PGDATA/.guvfx_ephemeral_owner" 2>/dev/null || true)"
  [ -n "$token" ] && grep -qxF "owner_token=$token" "$STATE" \
    || rec_die "REFUSED: ephemeral ownership stamp does not match the recorded state token -- refusing to act on a dir we cannot prove we own. Fail-closed."
}

if [ "$MODE" = "up" ]; then
  # E,J -- never adopt a pre-existing cluster, and never clobber existing data. ALL THREE paths this
  # run creates ($PGDATA, $SOCKDIR, $STATE) must be absent, so the failure-cleanup below can only ever
  # remove files THIS run created -- never a pre-existing sock/ subtree or state file an operator left
  # under a reused GUVFX_RECOVERY_EPHEMERAL_ROOT.
  for p in "$PGDATA" "$SOCKDIR" "$STATE"; do
    [ ! -e "$p" ] || rec_die "REFUSED: '$p' already exists under the controlled root -- the provisioner creates a FRESH disposable cluster and never adopts/clobbers pre-existing paths. Point GUVFX_RECOVERY_EPHEMERAL_ROOT at a NEW empty directory. Fail-closed."
  done
  [ -n "${GUVFX_RECOVERY_TESTER_PASSWORD:-}" ] \
    || rec_die "GUVFX_RECOVERY_TESTER_PASSWORD must be set (throwaway password for the least-privilege harness role). This script never invents or prints a secret."
  case "$GUVFX_RECOVERY_TESTER_PASSWORD" in *[!A-Za-z0-9_-]*|'') rec_die "GUVFX_RECOVERY_TESTER_PASSWORD must match [A-Za-z0-9_-]." ;; esac

  OWNER_TOKEN="guvfx-eph-$$-$(date -u +%Y%m%dT%H%M%SZ)"
  START_ATTEMPTED=0
  # J -- a failed/interrupted setup cleans up ONLY the ephemeral paths THIS run creates. These paths
  # provably did not pre-exist (we refused above if $PGDATA existed), and $SOCKDIR/$STATE live under
  # the controlled root, so removing exactly them can never touch a shared/production cluster.
  cleanup_up() {
    # Equivalent protection for failed-setup cleanup, with the SAME exit-code semantics as teardown.
    # Guard whenever a postmaster START WAS ATTEMPTED -- NOT only when pg_ctl reported success: a
    # `pg_ctl -w start` can return NON-ZERO while the forked postmaster keeps running (e.g. PGCTLTIMEOUT
    # elapsed, or an interrupted wait), so gating on success would skip the guard and rm a LIVE cluster.
    # So: once start was attempted, stop + WAIT, then require a POSITIVE stopped (pg_ctl status exit 3)
    # before any rm; if running (0) or UNKNOWN (4/other) LEAVE the data dir in place and report. Only
    # when start was NEVER attempted (initdb failed first -> no postmaster) are the partial paths removed.
    if [ "$START_ATTEMPTED" = "1" ]; then
      pg_ctl -D "$PGDATA" -w -m immediate stop >/dev/null 2>&1 || true
      if ! rec_pg_confirmed_stopped "$PGDATA"; then
        rec_err "failed-setup cleanup: postmaster at $PGDATA is not CONFIRMED stopped (pg_ctl status != 3) -- LEAVING the data dir in place for investigation (stop it and remove $ROOT manually). Not deleting a live/uncertain cluster."
        return 0
      fi
    fi
    rm -rf "$PGDATA" "$SOCKDIR" "$STATE" 2>/dev/null || true
    [ -z "${GUVFX_RECOVERY_EPHEMERAL_ROOT:-}" ] && rmdir "$ROOT" 2>/dev/null || true
    return 0
  }
  mkdir -p "$ROOT" "$SOCKDIR"
  trap cleanup_up EXIT INT TERM   # armed BEFORE initdb: an initdb/start failure cleans up our paths only
  rec_log "initdb: provisioning a FRESH disposable cluster at $PGDATA (bootstrap superuser '$BOOT', trust auth, no sync)"
  initdb -D "$PGDATA" -U "$BOOT" -A trust --no-sync >/dev/null 2>&1 \
      || rec_die "initdb failed -- could not create the disposable cluster. Fail-closed."
  printf '%s' "$OWNER_TOKEN" > "$PGDATA/.guvfx_ephemeral_owner"
  rec_log "starting ephemeral postmaster: socket-only (listen_addresses=''), -k $SOCKDIR -p $PORT"
  # Mark the attempt BEFORE the call: `pg_ctl -w start` can exit non-zero with the postmaster still
  # alive, so cleanup_up must run its stop+confirm guard even if the start below reports failure.
  START_ATTEMPTED=1
  pg_ctl -D "$PGDATA" -w -l "$PGDATA/startup.log" -o "-k $SOCKDIR -p $PORT -c listen_addresses= -c unix_socket_permissions=0700" start >/dev/null 2>&1 \
    || rec_die "pg_ctl start failed (see $PGDATA/startup.log). Fail-closed."

  # G,14,F -- STRUCTURAL anti-redirection check FIRST, fail-closed, BEFORE any other connection or DDL.
  # `off` is the data dir's OWN system_identifier read OFFLINE from disk (no connection); `live_sysid`
  # is read over the connection. If a connection was redirected to a DIFFERENT cluster (e.g. an
  # inherited PGHOSTADDR we failed to neutralise), live_sysid would be that other cluster's id and the
  # two will NOT match -- so we refuse before issuing current_user / CREATE ROLE / CREATE DATABASE. The
  # offline read is MANDATORY (pg_controldata is a required command): an unreadable offline id fails
  # closed rather than skipping the check.
  off="$(eph_offline_sysid)"
  [ -n "$off" ] || rec_die "REFUSED: cannot read the ephemeral data dir's offline system_identifier (pg_controldata) -- cannot prove the connection targets the cluster we created. Fail-closed."
  live_sysid="$(eph_psql postgres 'select system_identifier::text from pg_control_system()')" \
    || rec_die "cannot read the ephemeral cluster system_identifier. Fail-closed."
  [ -n "$live_sysid" ] || rec_die "empty system_identifier on the ephemeral cluster. Fail-closed."
  [ "$off" = "$live_sysid" ] \
    || rec_die "REFUSED: live-connection system_identifier '$live_sysid' != our data dir's offline id '$off' -- the connection was REDIRECTED to another cluster (e.g. via PGHOSTADDR). Aborting before any DDL. Fail-closed."
  [ "$live_sysid" != "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID}" ] \
    || rec_die "REFUSED: the freshly-provisioned cluster's system_identifier matches GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID -- impossible for a fresh initdb; aborting. Fail-closed."
  cur="$(eph_psql postgres 'select current_user')"; [ "$cur" = "$BOOT" ] \
    || rec_die "REFUSED: connected as '$cur', expected bootstrap superuser '$BOOT'. Fail-closed."
  # (No STATE-based ownership assert here: THIS run just ran initdb into a fresh dir we refused to
  # adopt if it pre-existed, and wrote our stamp -- ownership is established by construction. The
  # STATE token + stamp are written below for --down to verify later.)

  # The ONLY DDL in this tool -- and it runs inside the fresh cluster WE own (never a shared cluster).
  # Least-privilege harness role: NOSUPERUSER + CREATEDB + pg_read_all_stats; password on STDIN only.
  psql -v ON_ERROR_STOP=1 -q -h "$SOCKDIR" -p "$PORT" -U "$BOOT" postgres <<SQL
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$ROLE') AS role_exists \gset
\if :role_exists
  ALTER ROLE "$ROLE" LOGIN NOSUPERUSER NOCREATEROLE CREATEDB PASSWORD '$GUVFX_RECOVERY_TESTER_PASSWORD';
\else
  CREATE ROLE "$ROLE" LOGIN NOSUPERUSER NOCREATEROLE CREATEDB PASSWORD '$GUVFX_RECOVERY_TESTER_PASSWORD';
\endif
GRANT pg_read_all_stats TO "$ROLE";
SQL
  # Marker DB OWNED BY THE BOOTSTRAP ROLE (not the harness role), so the least-privilege harness can
  # never drop the operator's trust anchor.
  createdb -h "$SOCKDIR" -p "$PORT" -U "$BOOT" -O "$BOOT" "$MARKER"

  umask 077
  { printf 'owner_token=%s\n' "$OWNER_TOKEN"
    printf 'pgdata=%s\n' "$PGDATA"
    printf 'sockdir=%s\n' "$SOCKDIR"
    printf 'port=%s\n' "$PORT"
    printf 'boot=%s\n' "$BOOT"
    printf 'role=%s\n' "$ROLE"
    printf 'marker=%s\n' "$MARKER"
    printf 'sysid=%s\n' "$live_sysid"
    printf 'root_was_supplied=%s\n' "$( [ -n "${GUVFX_RECOVERY_EPHEMERAL_ROOT:-}" ] && echo 1 || echo 0)"
  } > "$STATE"
  trap - EXIT INT TERM   # success: disarm the failure-cleanup (the cluster stays up for certify.sh)

  cat >&2 <<EOF
[setup] dedicated DISPOSABLE cluster provisioned (initdb; socket-only; no TCP).
[setup]   data dir (ours)      : $PGDATA
[setup]   socket dir           : $SOCKDIR   port $PORT
[setup]   system_identifier    : $live_sysid  (brand-new; shares nothing with any existing cluster)
[setup]   bootstrap superuser  : $BOOT
[setup]   least-privilege role : $ROLE  (NOSUPERUSER, CREATEDB, pg_read_all_stats)
[setup]   isolation marker db  : $MARKER  (owned by $BOOT)
[setup]
[setup] Export this environment, then run certify.sh (do NOT export the password into evidence):
[setup]   export GUVFX_RECOVERY_ATTEST_ISOLATED=1
[setup]   export GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID='${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID}'
[setup]   export GUVFX_RECOVERY_ISOLATED_CLUSTER_ID='$live_sysid'
[setup]   export GUVFX_RECOVERY_ISOLATION_MARKER_DB='$MARKER'
[setup]   export PGHOST='$SOCKDIR' PGPORT='$PORT' PGUSER='$ROLE' PGPASSWORD='<the throwaway password you set>'
[setup]   export GUVFX_BACKUP_PASSPHRASE='<throwaway>'
[setup]   GUVFX_RECOVERY_ATTEST_ISOLATED=1 bash ops/recovery/tests/certify.sh
[setup]
[setup] Tear down with:  GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID='${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID}' GUVFX_RECOVERY_EPHEMERAL_ROOT='$ROOT' bash $0 --down
EOF
  # Emit a machine-readable line for test harnesses to capture the root.
  printf 'GUVFX_EPHEMERAL_ROOT=%s\n' "$ROOT"
  exit 0
fi

# --- MODE = down -----------------------------------------------------------
# H,I -- teardown acts ONLY on our stamped ephemeral dir under the controlled root. It stops the
# postmaster and removes the files WE created. It issues NO DROP ROLE / DROP DATABASE against any
# cluster -- so it cannot remove an unrelated role or database no matter how it is pointed.
assert_our_ephemeral_dir
# The data dir's OWN system_identifier (read offline from disk) must match what we recorded (proves it
# is still OUR cluster) and must not be the declared production id. [5,14] This is MANDATORY and
# fail-closed: an unreadable offline id does NOT skip the check (that would be a fail-open -- the
# production-identification and identity cross-check must never be omittable on a privileged path).
recorded_sysid="$(sed -n 's/^sysid=//p' "$STATE" | head -1)"
off="$(eph_offline_sysid)"
[ -n "$off" ] || rec_die "REFUSED: cannot read the data dir's offline system_identifier (pg_controldata) -- cannot prove this is the cluster we provisioned. Fail-closed."
[ "$off" = "$recorded_sysid" ] || rec_die "REFUSED: data-dir system_identifier '$off' != recorded ephemeral sysid '$recorded_sysid' -- not the cluster we provisioned. Fail-closed."
[ "$off" != "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID}" ] || rec_die "REFUSED: data-dir system_identifier matches GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID -- refusing. Fail-closed."
root_supplied="$(sed -n 's/^root_was_supplied=//p' "$STATE" | head -1)"   # read BEFORE removing STATE
# MEDIUM-B: VERIFY the ephemeral postmaster is actually stopped BEFORE any rm. A graceful stop is
# attempted (waiting for completion); then `pg_ctl status` must report NOT running (non-zero). If it
# is still running / uncertain, we do NOT delete the data directory -- that would corrupt a live
# cluster and destroy the evidence -- we preserve the ownership stamp + state, exit non-zero, and let
# the operator stop it and retry. (`pg_ctl status`: exit 0 = a server is running, non-zero = not.)
rec_log "stopping ephemeral postmaster (if running) under $ROOT, then verifying it stopped before removal"
if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then   # exit 0 = running -> attempt a waited stop
  pg_ctl -D "$PGDATA" -w -m fast stop >/dev/null 2>&1 || pg_ctl -D "$PGDATA" -w -m immediate stop >/dev/null 2>&1 || true
fi
# Require a POSITIVE "stopped" (pg_ctl status exit 3) before any rm. 0 (running) and 4/other (UNKNOWN:
# inaccessible/unspecified data dir, or an unexpected error) both refuse -- an error or inaccessible
# data dir must never be read as a clean shutdown (R-closeout correction).
rec_pg_confirmed_stopped "$PGDATA" || rec_die "REFUSED: the ephemeral postmaster at $PGDATA is not CONFIRMED stopped (pg_ctl status != 3; 0=running, 4=inaccessible, other=error) -- refusing to remove its data directory. Ownership stamp + state file are PRESERVED for investigation; resolve and re-run --down to retry. Fail-closed."
rm -rf "$PGDATA" "$SOCKDIR" "$STATE"
# Remove the root dir only if WE created it (not an operator-supplied directory with other contents).
[ "$root_supplied" = "1" ] || rmdir "$ROOT" 2>/dev/null || true
rec_log "teardown complete: disposable cluster at $PGDATA stopped and removed. No shared cluster was touched."
