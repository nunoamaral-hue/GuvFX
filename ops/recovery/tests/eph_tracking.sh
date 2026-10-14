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

# Stop + remove every tracked ephemeral cluster. Fail-safe: a cluster that will not stop is LEFT in
# place (not removed under a running postmaster). Removes only tracked absolute-path dirs; drops no
# role/database. Clears the tracking file when done.
eph_cleanup_tracked() {
  local r f="${GUVFX_EPH_ROOTS_FILE:-}"
  [ -n "$f" ] && [ -f "$f" ] || return 0
  while IFS= read -r r; do
    [ -n "$r" ] || continue
    case "$r" in /*) : ;; *) continue ;; esac   # only absolute paths we recorded
    if [ -d "$r/pgdata" ]; then
      pg_ctl -D "$r/pgdata" -w -m immediate stop >/dev/null 2>&1 || true
      if pg_ctl -D "$r/pgdata" status >/dev/null 2>&1; then
        printf '%s [eph_tracking][WARN] postmaster at %s did not stop -- leaving it in place (safe leak)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$r/pgdata" >&2
        continue
      fi
    fi
    rm -rf "$r" 2>/dev/null || true
  done < "$f"
  rm -f "$f" 2>/dev/null || true
  return 0
}
