#!/usr/bin/env bash
# GuvFX recovery certification -- HARNESS CLEANUP-TRACKING certification (R3 CLOSEOUT MEDIUM-A).
#
# Proves the PRODUCTION harness (certify_provisioner.sh) actually cleans up an ephemeral cluster on an
# ABNORMAL mid-suite termination -- i.e. the EXIT/INT/TERM trap is NOT a no-op. It does this END-TO-END:
# it runs the REAL certify_provisioner.sh in a self-test abort mode that provisions one cluster via the
# real `$(mktroot)` subshell and then exits non-zero WITHOUT tearing it down, firing the trap. If the
# subshell-safe tracking regressed, the trap would clean nothing and this test would see the leaked
# cluster and FAIL -- so it cannot pass vacuously. It also directly checks that a root tracked from a
# command-substitution subshell is visible to the parent (the exact property MEDIUM-A requires), and
# that cleanup leaves the main cluster and unrelated directories untouched.
#
# Requires: initdb/pg_ctl/pg_controldata/psql and a local PG the current user can initdb.
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROV="$HERE/certify_provisioner.sh"
REC="$(cd "$HERE/.." && pwd)"
# shellcheck source=../lib.sh
. "$REC/lib.sh"
rec_require_cmd initdb pg_ctl pg_controldata psql
# shellcheck source=eph_tracking.sh
. "$HERE/eph_tracking.sh"   # establishes + exports GUVFX_EPH_ROOTS_FILE in THIS parent (source time)

PASS=0; FAIL=0
pok()  { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
pbad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
note() { printf '[cleanup-certify] %s\n' "$*"; }

MAINSYS="$(psql -tAqc 'select system_identifier::text from pg_control_system()' postgres 2>/dev/null || true)"
[ -n "$MAINSYS" ] || { echo "[cleanup-certify] need a running local PG to prove the main cluster is untouched" >&2; exit 2; }
MDB0="$(psql -tAqc 'select count(*) from pg_database' postgres)"
MRL0="$(psql -tAqc 'select count(*) from pg_roles' postgres)"

RF="$(mktemp "${TMPDIR:-/tmp}/guvfx_selftest_root.XXXXXX")"
UNREL="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_unrelated.XXXXXX")"; printf 'keep-me\n' > "$UNREL/keep.txt"
SROOT=""
cleanup() {
  # Clean anything THIS test might have leaked (defensive); then our own temp files.
  [ -n "$SROOT" ] && [ -d "$SROOT/pgdata" ] && pg_ctl -D "$SROOT/pgdata" -w -m immediate stop >/dev/null 2>&1 || true
  [ -n "$SROOT" ] && rm -rf "$SROOT" 2>/dev/null || true
  eph_cleanup_tracked
  rm -rf "$UNREL" "$RF" 2>/dev/null || true
  return 0
}
trap cleanup EXIT INT TERM

echo; note "=== static bind: certify_provisioner.sh actually uses the shared tracking helper ==="
if grep -q 'eph_tracking.sh' "$PROV" && grep -q 'eph_track_root' "$PROV" && grep -q 'trap eph_cleanup_tracked' "$PROV"; then
  pok "certify_provisioner.sh sources eph_tracking.sh and wires track + trap (test binds to real harness)"
else pbad "certify_provisioner.sh does not wire the shared tracking helper"; fi

echo; note "=== subshell persistence: a root tracked inside a \$(...) subshell is visible to the parent ==="
# This is the exact shape mktroot uses. With the source-time exported file it persists; if tracking
# regressed to lazy per-subshell creation it would NOT, and this check would fail.
DUMMY="/nonexistent/guvfx_track_probe_$$"
discard="$( eph_track_root "$DUMMY" )" || true
if grep -qxF "$DUMMY" "$GUVFX_EPH_ROOTS_FILE" 2>/dev/null; then pok "root tracked from a command-substitution subshell IS visible to the parent"; else pbad "subshell-tracked root NOT visible to parent (MEDIUM-A regression)"; fi

echo; note "=== END-TO-END: real certify_provisioner.sh aborts mid-suite; its trap must clean up ==="
# Run the REAL harness in self-test abort mode: it provisions ONE cluster via the real mktroot subshell,
# records the root to $RF, then exits 1 WITHOUT down() -- firing its EXIT trap. We do NOT pre-set
# GUVFX_EPH_ROOTS_FILE for it (so it uses its own source-time file, exactly as a normal run does).
env -u GUVFX_EPH_ROOTS_FILE GUVFX_PROVCERT_SELFTEST_ABORT=1 GUVFX_PROVCERT_SELFTEST_ROOTFILE="$RF" \
  bash "$PROV" >/dev/null 2>&1 || true
SROOT="$(head -1 "$RF" 2>/dev/null || true)"

# Point 1: the harness recorded (knew) the root it created.
if [ -n "$SROOT" ]; then pok "1. the harness recorded the ephemeral root it created ($SROOT)"; else pbad "1. no root recorded by the self-test abort"; fi
# Point 2: the owned postmaster was stopped by the trap.
if [ -n "$SROOT" ] && pg_ctl -D "$SROOT/pgdata" status >/dev/null 2>&1; then pbad "2. postmaster still running after abort -- trap did NOT clean up (MEDIUM-A regression)"; else pok "2. the owned ephemeral postmaster was stopped by the abnormal-exit trap"; fi
# Point 3: the temporary data directory was removed by the trap.
if [ -n "$SROOT" ] && [ -e "$SROOT" ]; then pbad "3. temp dir survived the abort -- trap did NOT remove it ($SROOT)"; else pok "3. the temporary ephemeral directory was removed by the abnormal-exit trap"; fi
# Point 4: the main cluster is unchanged.
MDB1="$(psql -tAqc 'select count(*) from pg_database' postgres)"; MRL1="$(psql -tAqc 'select count(*) from pg_roles' postgres)"
if [ "$MDB1" = "$MDB0" ] && [ "$MRL1" = "$MRL0" ]; then pok "4. the main PostgreSQL cluster is unchanged (dbs=$MDB1 roles=$MRL1)"; else pbad "4. main cluster changed (dbs $MDB0->$MDB1 roles $MRL0->$MRL1)"; fi
# Point 5: unrelated directories untouched.
if [ -f "$UNREL/keep.txt" ] && [ "$(cat "$UNREL/keep.txt")" = "keep-me" ]; then pok "5. unrelated directory + sentinel untouched"; else pbad "5. an unrelated directory was affected"; fi

echo; note "================= CLEANUP CERTIFY SUMMARY: PASS=$PASS FAIL=$FAIL ================="
[ "$FAIL" -eq 0 ] || { note "CLEANUP CERTIFICATION FAILED"; exit 1; }
note "CLEANUP CERTIFICATION PASSED"
