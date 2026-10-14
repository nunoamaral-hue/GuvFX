#!/usr/bin/env bash
# GuvFX recovery certification -- HARNESS CLEANUP-TRACKING certification (R3 CLOSEOUT / FINAL).
#
# Proves the PRODUCTION harness (certify_provisioner.sh) actually cleans up an ephemeral cluster on an
# ABNORMAL mid-suite termination -- i.e. the EXIT/INT/TERM trap is NOT a no-op -- and that it does so
# ONLY after a postmaster was PROVABLY running (non-vacuous). It also certifies the shared cleanup
# safety contract (eph_safe_remove_root): a cluster whose shutdown cannot be confirmed is LEFT in place
# rather than deleted, with safe retry.
#
# It runs END-TO-END against the REAL certify_provisioner.sh via a self-test abort mode that provisions
# one cluster (requiring a confirmed-running postmaster + recorded startup evidence) and then exits
# non-zero WITHOUT tearing it down, firing the trap. If the subshell-safe tracking regressed, the trap
# would clean nothing and this test FAILS; if startup silently failed, the recorded evidence is
# running=0 and this test FAILS -- so it cannot pass vacuously.
#
# Requires: initdb/pg_ctl/pg_controldata/psql and a local PG the current user can initdb.
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROV="$HERE/certify_provisioner.sh"
SETUP="$HERE/setup_isolated_cluster.sh"
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
PW="cleanup_$$_$(date +%s)"

RF="$(mktemp "${TMPDIR:-/tmp}/guvfx_selftest_root.XXXXXX")"
EV="$(mktemp "${TMPDIR:-/tmp}/guvfx_selftest_ev.XXXXXX")"
UNREL="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_unrelated.XXXXXX")"; printf 'keep-me\n' > "$UNREL/keep.txt"
SROOT=""; RMB=""
cleanup() {
  # Use the SAME safe contract for our own fixtures (never rm a data dir under a running postmaster).
  [ -n "$SROOT" ] && eph_safe_remove_root "$SROOT" || true
  [ -n "$RMB" ] && eph_safe_remove_root "$RMB" || true
  eph_cleanup_tracked
  rm -rf "$UNREL" "$RF" "$EV" 2>/dev/null || true
  return 0
}
trap cleanup EXIT INT TERM

echo; note "=== static bind: certify_provisioner.sh actually uses the shared tracking helper ==="
if grep -q 'eph_tracking.sh' "$PROV" && grep -q 'eph_track_root' "$PROV" && grep -q 'trap eph_cleanup_tracked' "$PROV"; then
  pok "certify_provisioner.sh sources eph_tracking.sh and wires track + trap (test binds to real harness)"
else pbad "certify_provisioner.sh does not wire the shared tracking helper"; fi

echo; note "=== subshell persistence: a root tracked inside a \$(...) subshell is visible to the parent ==="
DUMMY="/nonexistent/guvfx_track_probe_$$"
discard="$( eph_track_root "$DUMMY" )" || true
if grep -qxF "$DUMMY" "$GUVFX_EPH_ROOTS_FILE" 2>/dev/null; then pok "root tracked from a command-substitution subshell IS visible to the parent"; else pbad "subshell-tracked root NOT visible to parent (regression)"; fi

echo; note "=== END-TO-END: real certify_provisioner.sh provisions + verifies RUNNING, aborts, trap cleans up ==="
env -u GUVFX_EPH_ROOTS_FILE GUVFX_PROVCERT_SELFTEST_ABORT=1 GUVFX_PROVCERT_SELFTEST_ROOTFILE="$RF" GUVFX_PROVCERT_SELFTEST_EVIDENCE="$EV" \
  bash "$PROV" >/dev/null 2>&1 || true
SROOT="$(head -1 "$RF" 2>/dev/null || true)"
# MEDIUM-A: REQUIRE positive startup evidence (a postmaster was provably running) before trusting the
# cleanup assertions. Without running=1 + a sysid, the end-to-end proves nothing and must NOT pass.
STARTED_OK=0
if grep -q '^running=1' "$EV" 2>/dev/null && grep -q '^sysid=[0-9]' "$EV" 2>/dev/null && [ -n "$SROOT" ]; then
  STARTED_OK=1; pok "positive startup evidence: a RUNNING postmaster was confirmed before abort (running=1 + sysid)"
else pbad "no positive startup evidence (running=1 + sysid) -- cleanup cannot be certified non-vacuously"; fi

if [ "$STARTED_OK" = "1" ]; then
  if pg_ctl -D "$SROOT/pgdata" status >/dev/null 2>&1; then pbad "postmaster still running after abort -- trap did NOT clean up (regression)"; else pok "the owned ephemeral postmaster was stopped by the abnormal-exit trap"; fi
  if [ -e "$SROOT" ]; then pbad "temp dir survived the abort -- trap did NOT remove it ($SROOT)"; else pok "the temporary ephemeral directory was removed by the abnormal-exit trap"; fi
else
  pbad "SKIPPED stop/removal assertions (no proven running postmaster)"; pbad "SKIPPED (see above)"
fi
MDB1="$(psql -tAqc 'select count(*) from pg_database' postgres)"; MRL1="$(psql -tAqc 'select count(*) from pg_roles' postgres)"
if [ "$MDB1" = "$MDB0" ] && [ "$MRL1" = "$MRL0" ]; then pok "the main PostgreSQL cluster is unchanged (dbs=$MDB1 roles=$MRL1)"; else pbad "main cluster changed (dbs $MDB0->$MDB1 roles $MRL0->$MRL1)"; fi
if [ -f "$UNREL/keep.txt" ] && [ "$(cat "$UNREL/keep.txt")" = "keep-me" ]; then pok "unrelated directory + sentinel untouched"; else pbad "an unrelated directory was affected"; fi

echo; note "=== MEDIUM-A negative: a FAILED ephemeral startup must NOT record a fake success ==="
SHIMS="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_shim_start.XXXXXX")"; REALPGCTL="$(command -v pg_ctl)"
cat > "$SHIMS/pg_ctl" <<SH
#!/usr/bin/env bash
for a in "\$@"; do [ "\$a" = "start" ] && exit 1; done
exec "$REALPGCTL" "\$@"
SH
chmod +x "$SHIMS/pg_ctl"
EVN="$(mktemp "${TMPDIR:-/tmp}/guvfx_selftest_evn.XXXXXX")"
env -u GUVFX_EPH_ROOTS_FILE PATH="$SHIMS:$PATH" GUVFX_PROVCERT_SELFTEST_ABORT=1 GUVFX_PROVCERT_SELFTEST_ROOTFILE=/dev/null GUVFX_PROVCERT_SELFTEST_EVIDENCE="$EVN" \
  bash "$PROV" >/dev/null 2>&1 || true
if grep -q '^running=0' "$EVN" 2>/dev/null && ! grep -q '^running=1' "$EVN" 2>/dev/null; then pok "forced-startup-failure records running=0 (no fake success for a postmaster that never started)"; else pbad "startup failure should record running=0, got: $(tr '\n' ' ' < "$EVN" 2>/dev/null)"; fi
rm -rf "$SHIMS" "$EVN"

echo; note "=== MEDIUM-B: cleanup contract -- a cluster whose shutdown is unconfirmed is PRESERVED, retry succeeds ==="
RMB="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_mb_eph.XXXXXX")"
GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_TESTER_PASSWORD="$PW" GUVFX_RECOVERY_EPHEMERAL_ROOT="$RMB" bash "$SETUP" --up >/dev/null 2>&1 || true
if [ -d "$RMB/pgdata" ] && pg_ctl -D "$RMB/pgdata" status >/dev/null 2>&1; then
  # B/C: simulate a failed shutdown (pg_ctl `stop` no-op shim) -> eph_safe_remove_root must LEAVE the dir.
  SHIMT="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_shim_stop.XXXXXX")"
  cat > "$SHIMT/pg_ctl" <<SH
#!/usr/bin/env bash
for a in "\$@"; do [ "\$a" = "stop" ] && exit 0; done
exec "$REALPGCTL" "\$@"
SH
  chmod +x "$SHIMT/pg_ctl"
  if ( PATH="$SHIMT:$PATH"; eph_safe_remove_root "$RMB" ); then pbad "safe-remove should FAIL (return non-zero) when shutdown is unconfirmed"; else
    if [ -d "$RMB/pgdata" ]; then pok "failed shutdown PRESERVES the data directory (not deleted under a running postmaster)"; else pbad "data dir was removed despite unconfirmed shutdown"; fi
  fi
  rm -rf "$SHIMT"
  # D: safe retry with a REAL stop -> removes it.
  if eph_safe_remove_root "$RMB" && [ ! -e "$RMB" ]; then pok "safe retry (real shutdown) stops + removes the cluster"; RMB=""; else pbad "safe retry should stop + remove the cluster"; fi
else
  pbad "MEDIUM-B setup: expected a running ephemeral cluster to exercise the cleanup contract"
fi
MDB2="$(psql -tAqc 'select count(*) from pg_database' postgres)"; MRL2="$(psql -tAqc 'select count(*) from pg_roles' postgres)"
if [ "$MDB2" = "$MDB0" ] && [ "$MRL2" = "$MRL0" ]; then pok "main cluster unchanged throughout the cleanup-contract tests (dbs=$MDB2 roles=$MRL2)"; else pbad "main cluster changed during cleanup-contract tests"; fi

echo; note "================= CLEANUP CERTIFY SUMMARY: PASS=$PASS FAIL=$FAIL ================="
[ "$FAIL" -eq 0 ] || { note "CLEANUP CERTIFICATION FAILED"; exit 1; }
note "CLEANUP CERTIFICATION PASSED"
