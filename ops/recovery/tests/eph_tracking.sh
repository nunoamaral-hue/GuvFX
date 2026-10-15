# shellcheck shell=bash
# GuvFX recovery certification -- ephemeral-cluster lifecycle tracking (R3 CLOSEOUT MEDIUM-A).
#
# The provisioner test harness creates each ephemeral root inside a COMMAND SUBSTITUTION
# (`R="$(mktroot)"`), which bash runs in a SUBSHELL. A shell variable (or an `export`) assigned inside
# that subshell does NOT propagate to the parent, so the parent's EXIT/INT/TERM trap would be blind to
# the clusters created there. The fix: establish the tracking file in the PARENT at SOURCE TIME --
# sourcing (`. eph_tracking.sh`) runs in the caller's shell, NOT a subshell -- and EXPORT it, so every
# `$(mktroot)` subshell INHERITS the same path and its appends land in the one file the parent trap
# reads. (An earlier revision created the file lazily inside the subshell; that made the trap a no-op
# on the real harness and leaked one stray tracking file per call -- the HIGH this revision fixes.)
#
# Sourced by certify_provisioner.sh (production path) and certify_cleanup.sh (the MEDIUM-A regression)
# so both exercise the SAME code. Each tracked root is a directory this run created under TMPDIR;
# cleanup stops its ephemeral postmaster (ours, socket-only) and removes ONLY that tracked dir. It
# NEVER drops a role/database and NEVER touches a shared/production cluster or an untracked path. If a
# postmaster cannot be confirmed stopped, its data dir is LEFT IN PLACE (a safe leak) rather than
# removed under a running server.

# Establish + export the per-process tracking file IN THE PARENT (source time). Honour an inherited
# value if a caller already set one. This line runs in the sourcing shell, so the assignment persists.
if [ -z "${GUVFX_EPH_ROOTS_FILE:-}" ]; then
  GUVFX_EPH_ROOTS_FILE="$(mktemp "${TMPDIR:-/tmp}/guvfx_eph_roots.XXXXXX")"
fi
export GUVFX_EPH_ROOTS_FILE

# Record one ephemeral root. Safe to call from inside a command-substitution subshell: the subshell
# INHERITS the exported GUVFX_EPH_ROOTS_FILE from the parent, so the append lands in the one file the
# parent will read -- no per-subshell file is ever created.
eph_track_root() { printf '%s\n' "$1" >> "$GUVFX_EPH_ROOTS_FILE"; }

# Safely retire ONE ephemeral root (the shared cleanup-safety contract): stop its postmaster (waited),
# VERIFY it stopped (`pg_ctl status` non-zero = not running), and remove the dir ONLY if confirmed
# stopped; otherwise LEAVE the data dir in place (never rm under a running postmaster) and return
# non-zero so a caller can treat an uncertain shutdown as a failure and preserve evidence. Acts only
# on an absolute `.../pgdata`-bearing tracked path; drops no role/database; touches no other cluster.
# `pg_ctl status` exit codes: 0 = running, 3 = NOT running (cleanly stopped), 4 = inaccessible /
# unspecified data dir, other = error. Confirmed-stopped is ONLY exit 3; 0 and any other code (UNKNOWN)
# both mean "do not delete". Identical contract to lib.sh rec_pg_confirmed_stopped (kept local so this
# helper has no lib.sh dependency; delegates to it when available to stay provably consistent).
eph_pg_confirmed_stopped() {
  if command -v rec_pg_confirmed_stopped >/dev/null 2>&1; then rec_pg_confirmed_stopped "$1"; return; fi
  local rc=0
  pg_ctl -D "$1" status >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 3 ]
}

eph_safe_remove_root() {
  local r="$1"
  [ -n "$r" ] || return 0
  case "$r" in /*) : ;; *) return 0 ;; esac
  if [ -d "$r/pgdata" ]; then
    pg_ctl -D "$r/pgdata" -w -m immediate stop >/dev/null 2>&1 || true
    # Remove ONLY on a POSITIVE stopped (exit 3). Running (0) or UNKNOWN (4/error) -> leave + non-zero.
    if ! eph_pg_confirmed_stopped "$r/pgdata"; then
      printf '%s [eph_tracking][WARN] postmaster at %s not CONFIRMED stopped (pg_ctl status != 3) -- leaving data dir in place (safe); evidence preserved\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$r/pgdata" >&2
      return 1
    fi
  fi
  rm -rf "$r" 2>/dev/null || true
  return 0
}

# Stop + remove every tracked ephemeral cluster via the safe contract above. A cluster that will not
# stop is LEFT in place. Clears the tracking file when done.
eph_cleanup_tracked() {
  local r f="${GUVFX_EPH_ROOTS_FILE:-}"
  [ -n "$f" ] && [ -f "$f" ] || return 0
  while IFS= read -r r; do [ -n "$r" ] && eph_safe_remove_root "$r" || true; done < "$f"
  rm -f "$f" 2>/dev/null || true
  return 0
}
