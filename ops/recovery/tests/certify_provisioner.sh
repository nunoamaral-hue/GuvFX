#!/usr/bin/env bash
# GuvFX recovery certification -- PRIVILEGED PROVISIONER trust-boundary certification (R3).
#
# Exercises ops/recovery/tests/setup_isolated_cluster.sh (the dedicated-ephemeral-cluster provisioner)
# against the R3 invariants: NO privileged DDL against an unverified cluster, on EITHER setup or
# teardown. Every destructive/mutating path must be positively gated by an ownership boundary the
# provisioner cannot manufacture on an unknown cluster, and must refuse unidentified / mismatched /
# redirected targets with a PRECISE reason. It creates and destroys only disposable ephemeral
# clusters it owns; it NEVER touches the operator's main cluster.
#
# Requires: initdb/pg_ctl/pg_controldata/psql and a local environment where the current user can run
# initdb. Run from anywhere:  bash ops/recovery/tests/certify_provisioner.sh
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP="$HERE/setup_isolated_cluster.sh"
REC="$(cd "$HERE/.." && pwd)"
# shellcheck source=../lib.sh
. "$REC/lib.sh"
rec_require_cmd initdb pg_ctl pg_controldata psql

PASS=0; FAIL=0
pok()  { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
pbad() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
note() { printf '[prov-certify] %s\n' "$*"; }

# Main (operator) cluster system_identifier -- the "production-like" cluster the provisioner must
# never mutate. Read read-only; this harness never writes to it.
MAINSYS="$(psql -tAqc 'select system_identifier::text from pg_control_system()' postgres 2>/dev/null || true)"
[ -n "$MAINSYS" ] || { echo "[prov-certify] cannot read the local main cluster system_identifier (need a running local PG for the 'must not touch production' checks)" >&2; exit 2; }
MAIN_DBS_BEFORE="$(psql -tAqc 'select count(*) from pg_database' postgres)"
MAIN_ROLES_BEFORE="$(psql -tAqc 'select count(*) from pg_roles' postgres)"
PW="prov_throwaway_$$_$(date +%s)"   # per-run throwaway tester password (no static credential in Git)

# MEDIUM-A: track each ephemeral root in a FILE (via the shared helper), because mktroot is called as
# `R="$(mktroot)"` -- a subshell -- so a shell-variable append would NOT reach the parent trap. File
# writes from the subshell persist, so the EXIT/INT/TERM trap always has the authoritative root list
# even on an abnormal mid-suite termination.
# shellcheck source=eph_tracking.sh
. "$HERE/eph_tracking.sh"
mktroot()  { local r; r="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_provcert.XXXXXX")"; eph_track_root "$r"; printf '%s' "$r"; }
trap eph_cleanup_tracked EXIT INT TERM

up()   { GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_TESTER_PASSWORD="$PW" GUVFX_RECOVERY_EPHEMERAL_ROOT="$1" bash "$SETUP" --up; }
down() { GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_EPHEMERAL_ROOT="$1" bash "$SETUP" --down; }
# Assert a command REFUSES with a precise reason substring.
refuses() { local desc="$1" exp="$2"; shift 2; local out
  if out="$("$@" 2>&1)"; then pbad "$desc (SHOULD have refused but succeeded)";
  else case "$out" in *"$exp"*) pok "$desc";; *) pbad "$desc (refused for WRONG reason: $(printf '%s' "$out" | grep -i refused | tail -1))";; esac; fi; }

# SELF-TEST HOOK (used ONLY by certify_cleanup.sh): provision ONE ephemeral cluster via the REAL
# mktroot (a `$(mktroot)` subshell) + up, then abort WITHOUT tearing it down -- firing the EXIT trap --
# so the caller can verify the production trap's abnormal-exit cleanup.
# MEDIUM-A (final): startup failures are NO LONGER suppressed. We REQUIRE a confirmed-RUNNING postmaster
# at the expected private data dir with a verifiable cluster identity, and record POSITIVE startup
# evidence, BEFORE the intentional abort. If startup fails we record running=0 and exit distinctly --
# never a fake success -- so certify_cleanup.sh cannot "pass" a cleanup for a postmaster that never ran.
if [ "${GUVFX_PROVCERT_SELFTEST_ABORT:-0}" = "1" ]; then
  EV="${GUVFX_PROVCERT_SELFTEST_EVIDENCE:-/dev/null}"
  RSELF="$(mktroot)"
  if up "$RSELF" >/dev/null 2>&1 \
     && [ -d "$RSELF/pgdata" ] \
     && pg_ctl -D "$RSELF/pgdata" status >/dev/null 2>&1 \
     && SELFSYS="$(sed -n 's/^sysid=//p' "$RSELF/.guvfx_ephemeral" | head -1)" && [ -n "$SELFSYS" ] \
     && [ "$SELFSYS" = "$(pg_controldata "$RSELF/pgdata" 2>/dev/null | sed -n 's/.*system identifier: *//p' | head -1)" ]; then
    { printf 'running=1\n'; printf 'root=%s\n' "$RSELF"; printf 'datadir=%s\n' "$RSELF/pgdata"; printf 'sysid=%s\n' "$SELFSYS"; } > "$EV"
    printf '%s\n' "$RSELF" > "${GUVFX_PROVCERT_SELFTEST_ROOTFILE:-/dev/null}"
    note "SELFTEST: provisioned + verified RUNNING postmaster at $RSELF (sysid=$SELFSYS); aborting (exit 1) to exercise the trap"
    exit 1
  fi
  printf 'running=0\n' > "$EV"
  note "SELFTEST: ephemeral startup could NOT be confirmed running -- recorded running=0 (no fake success); aborting"
  exit 2
fi

echo; note "=== 1/2: verified disposable-cluster setup + teardown succeed ==="
R1="$(mktroot)"
if up "$R1" >/dev/null 2>&1; then
  P="$(sed -n 's/^port=//p' "$R1/.guvfx_ephemeral")"; S="$(sed -n 's/^sysid=//p' "$R1/.guvfx_ephemeral")"
  reach="$(psql -h "$R1/sock" -p "$P" -U guvfx_eph_boot -tAqc 'select 1' postgres 2>/dev/null || true)"
  [ "$reach" = "1" ] && [ "$S" != "$MAINSYS" ] && pok "verified disposable-cluster setup succeeds (fresh sysid=$S != main)" || pbad "setup succeeds (reachable=$reach sysid=$S)"
else pbad "verified disposable-cluster setup succeeds (up failed)"; fi
if down "$R1" >/dev/null 2>&1 && [ ! -e "$R1/pgdata" ]; then pok "verified disposable-cluster teardown succeeds (data dir removed)"; else pbad "teardown succeeds"; fi

echo; note "=== 3: --teardown with NO production identifier is refused ==="
R3="$(mktroot)"; up "$R3" >/dev/null 2>&1
refuses "teardown refused with no GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID" "must be set on EVERY privileged path" \
  env -u GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID GUVFX_RECOVERY_EPHEMERAL_ROOT="$R3" bash "$SETUP" --down
down "$R3" >/dev/null 2>&1 || true

echo; note "=== 4: setup cannot ADOPT a pre-existing cluster (sentinel id cannot authorize it) ==="
R4="$(mktroot)"; mkdir -p "$R4/pgdata"; : > "$R4/pgdata/PG_VERSION"   # a pre-existing (foreign) data dir
refuses "setup refuses to adopt/clobber a pre-existing data dir (even with a prod id set)" "already exists" \
  env GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_TESTER_PASSWORD="$PW" GUVFX_RECOVERY_EPHEMERAL_ROOT="$R4" bash "$SETUP" --up

echo; note "=== 5: teardown refuses when the data-dir identity != recorded ephemeral sysid ==="
R5="$(mktroot)"; up "$R5" >/dev/null 2>&1
# tamper the recorded sysid so the live/offline data-dir identity no longer matches
sed -i.bak 's/^sysid=.*/sysid=1234567890123456789/' "$R5/.guvfx_ephemeral"; rm -f "$R5/.guvfx_ephemeral.bak"
refuses "teardown refuses an identity mismatch (data-dir sysid != recorded)" "!= recorded ephemeral sysid" down "$R5"
# fix it back and tear down for real
sed -i.bak "s/^sysid=.*/sysid=$(pg_controldata "$R5/pgdata" | sed -n 's/.*system identifier: *//p')/" "$R5/.guvfx_ephemeral"; rm -f "$R5/.guvfx_ephemeral.bak"
down "$R5" >/dev/null 2>&1 || true

echo; note "=== 6: teardown refuses an UNKNOWN (unstamped) data dir ==="
R6="$(mktroot)"; mkdir -p "$R6/pgdata"; : > "$R6/pgdata/PG_VERSION"; printf 'owner_token=x\n' > "$R6/.guvfx_ephemeral"
refuses "teardown refuses a data dir with no GuvFX ownership stamp" "no GuvFX ephemeral ownership stamp" down "$R6"

echo; note "=== 7/8/9: hostile PGHOST / PGHOSTADDR / PGSERVICE / PGUSER cannot redirect the provisioner ==="
# PGHOSTADDR=127.0.0.1 is the key vector: libpq would demote an explicit -h /socket to a TCP connect
# at that IP:PORT. The provisioner listens socket-only (no TCP), so WITHOUT the fix this connects to
# a refused TCP port and --up fails; WITH the fix (PGHOSTADDR unset) it uses its own socket and succeeds.
R7="$(mktroot)"
if env PGHOST=/nonexistent/sock PGHOSTADDR=127.0.0.1 PGPORT=1 PGDATABASE=postgres PGSERVICE=bogus PGUSER=evilrole \
     GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_TESTER_PASSWORD="$PW" GUVFX_RECOVERY_EPHEMERAL_ROOT="$R7" \
     bash "$SETUP" --up >/dev/null 2>&1 \
   && [ "$(sed -n 's/^sysid=//p' "$R7/.guvfx_ephemeral")" != "$MAINSYS" ] \
   && [ "$(psql -h "$R7/sock" -p "$(sed -n 's/^port=//p' "$R7/.guvfx_ephemeral")" -U guvfx_eph_boot -tAqc 'select 1' postgres 2>/dev/null)" = "1" ]; then
  pok "inherited PGHOST/PGHOSTADDR/PGPORT/PGDATABASE/PGSERVICE/PGUSER are NEUTRALISED (provisioner used only its own socket; not redirected)"
else pbad "hostile connection env should be neutralised, not redirect/fail"; fi
down "$R7" >/dev/null 2>&1 || true

echo; note "=== 10/11: teardown cannot remove UNRELATED databases/roles (issues no DROP against a shared cluster) ==="
R10="$(mktroot)"; up "$R10" >/dev/null 2>&1; down "$R10" >/dev/null 2>&1
MAIN_DBS_AFTER="$(psql -tAqc 'select count(*) from pg_database' postgres)"
MAIN_ROLES_AFTER="$(psql -tAqc 'select count(*) from pg_roles' postgres)"
[ "$MAIN_DBS_AFTER" = "$MAIN_DBS_BEFORE" ] && pok "teardown left the main cluster's databases untouched ($MAIN_DBS_AFTER)" || pbad "main DB count changed ($MAIN_DBS_BEFORE -> $MAIN_DBS_AFTER)"
[ "$MAIN_ROLES_AFTER" = "$MAIN_ROLES_BEFORE" ] && pok "teardown left the main cluster's roles untouched ($MAIN_ROLES_AFTER)" || pbad "main role count changed ($MAIN_ROLES_BEFORE -> $MAIN_ROLES_AFTER)"
# and a teardown pointed at an unrelated (unstamped) dir refuses rather than removing anything
R11="$(mktroot)"; mkdir -p "$R11/pgdata"; : > "$R11/pgdata/PG_VERSION"
refuses "teardown refuses an unrelated data dir (cannot be pointed at a foreign cluster)" "no GuvFX ephemeral ownership stamp" down "$R11"

echo; note "=== 12: a FAILED setup performs no unsafe cleanup and leaves the main cluster intact ==="
R12="$(mktroot)"; mkdir -p "$R12/pgdata"; : > "$R12/pgdata/PG_VERSION"   # forces the 'already exists' refusal
( env GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_TESTER_PASSWORD="$PW" GUVFX_RECOVERY_EPHEMERAL_ROOT="$R12" bash "$SETUP" --up ) >/dev/null 2>&1 && pbad "failed-setup test: up should have refused" || {
  mdbs="$(psql -tAqc 'select count(*) from pg_database' postgres)"; [ "$mdbs" = "$MAIN_DBS_BEFORE" ] && [ -f "$R12/pgdata/PG_VERSION" ] && pok "failed setup refused BEFORE mutation; main cluster intact; pre-existing dir not clobbered" || pbad "failed setup side effects (main dbs=$mdbs)"; }

echo; note "=== 13: the existing recovery certification harness (64 cases) still succeeds on an ephemeral cluster ==="
R13="$(mktroot)"; up "$R13" >/dev/null 2>&1
EPHSYS="$(sed -n 's/^sysid=//p' "$R13/.guvfx_ephemeral")"; EPHPORT="$(sed -n 's/^port=//p' "$R13/.guvfx_ephemeral")"
if env PGHOST="$R13/sock" PGPORT="$EPHPORT" PGUSER=guvfx_recov_tester PGPASSWORD="$PW" \
     GUVFX_RECOVERY_ATTEST_ISOLATED=1 GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" \
     GUVFX_RECOVERY_ISOLATED_CLUSTER_ID="$EPHSYS" GUVFX_RECOVERY_ISOLATION_MARKER_DB=guvfx_recovery_isolated_ok \
     GUVFX_BACKUP_PASSPHRASE="prov-cert-throwaway-$$" \
     bash "$HERE/certify.sh" >"$R13/certify.out" 2>&1 && grep -q 'CERTIFICATION PASSED' "$R13/certify.out"; then
  pok "recovery certify.sh still PASSES on the ephemeral cluster ($(grep -oE 'PASS=[0-9]+ FAIL=[0-9]+' "$R13/certify.out" | tail -1))"
else pbad "recovery certify.sh failed on the ephemeral cluster (see output tail: $(tail -1 "$R13/certify.out" 2>/dev/null))"; fi
down "$R13" >/dev/null 2>&1 || true

echo; note "=== 14: a production-like cluster is rejected BEFORE any destructive teardown op ==="
R14="$(mktroot)"; up "$R14" >/dev/null 2>&1
# Point the prod denylist at the ephemeral cluster's OWN sysid: teardown must refuse (prod match)
# before stopping/removing anything -- proving the production guard fires pre-destruction.
EPH14="$(sed -n 's/^sysid=//p' "$R14/.guvfx_ephemeral")"
refuses "teardown refuses when the target identity matches the production denylist (pre-destruction)" "matches GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID" \
  env GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$EPH14" GUVFX_RECOVERY_EPHEMERAL_ROOT="$R14" bash "$SETUP" --down
[ -e "$R14/pgdata" ] && pok "data dir still present after the refused teardown (nothing destroyed)" || pbad "data dir was removed despite the production-match refusal"
down "$R14" >/dev/null 2>&1 || true

echo; note "=== 15: SETUP (not just teardown) refuses with no production identifier ==="
R15="$(mktroot)"
refuses "setup refused with no GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID (mandatory on the setup path too)" "must be set on EVERY privileged path" \
  env -u GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID GUVFX_RECOVERY_TESTER_PASSWORD="$PW" GUVFX_RECOVERY_EPHEMERAL_ROOT="$R15" bash "$SETUP" --up

echo; note "=== 16: anti-forgery -- teardown refuses when the stamp token DISAGREES with the recorded state ==="
R16="$(mktroot)"; up "$R16" >/dev/null 2>&1
printf 'forged-token-not-in-state' > "$R16/pgdata/.guvfx_ephemeral_owner"   # stamp present but token != STATE
refuses "teardown refuses a stamp token that does not match the recorded state token" "does not match the recorded state token" down "$R16"
# restore the real token so the trap can clean it up
printf '%s' "$(sed -n 's/^owner_token=//p' "$R16/.guvfx_ephemeral" | head -1)" > "$R16/pgdata/.guvfx_ephemeral_owner"
down "$R16" >/dev/null 2>&1 || true

echo; note "=== 17: MEDIUM-B -- teardown REFUSES to delete when the postmaster did NOT stop (verified shutdown) ==="
R17="$(mktroot)"; up "$R17" >/dev/null 2>&1
# pg_ctl shim: `stop` is a no-op success (the postmaster stays UP); everything else (incl. `status`)
# delegates to the REAL pg_ctl -- so after the "stop" the status check still reports a running server.
SHIM="$R17/shimbin"; mkdir -p "$SHIM"; REAL_PGCTL="$(command -v pg_ctl)"
cat > "$SHIM/pg_ctl" <<SH
#!/usr/bin/env bash
for a in "\$@"; do [ "\$a" = "stop" ] && exit 0; done
exec "$REAL_PGCTL" "\$@"
SH
chmod +x "$SHIM/pg_ctl"
if out="$( PATH="$SHIM:$PATH" GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_EPHEMERAL_ROOT="$R17" bash "$SETUP" --down 2>&1 )"; then
  pbad "teardown should FAIL (non-zero) when the postmaster did not stop"
else
  if printf '%s' "$out" | grep -q "not CONFIRMED stopped" && [ -f "$R17/pgdata/PG_VERSION" ] && [ -f "$R17/.guvfx_ephemeral" ] && [ -f "$R17/pgdata/.guvfx_ephemeral_owner" ]; then
    pok "teardown REFUSES (non-zero) and PRESERVES data dir + state + ownership stamp when shutdown is unconfirmed (status 0/running)"
  else pbad "teardown refusal/evidence-preservation wrong (reason: $(printf '%s' "$out" | grep -i refused | tail -1))"; fi
fi
echo; note "=== 18: MEDIUM-B -- a subsequent real teardown succeeds (safe operator retry) ==="
if down "$R17" >/dev/null 2>&1 && [ ! -e "$R17/pgdata" ]; then pok "retry teardown (real pg_ctl) stops + removes the cluster"; else pbad "retry teardown should succeed after the postmaster can stop"; fi

echo; note "=== 19: exit-code semantics -- setup --down refuses on an UNKNOWN pg_ctl status (4), not just running ==="
# pg_ctl status exit 4 = inaccessible / unspecified data dir (UNKNOWN). Teardown must NOT treat that as
# a clean shutdown. Shim: status->4, stop->no-op (cluster stays up); setup --down must REFUSE + preserve.
R19="$(mktroot)"; up "$R19" >/dev/null 2>&1
SHIM19="$R19/shim19"; mkdir -p "$SHIM19"
{ printf '#!/usr/bin/env bash\n'
  printf 'for a in "$@"; do [ "$a" = "stop" ] && exit 0; [ "$a" = "status" ] && exit 4; done\n'
  printf 'exec "%s" "$@"\n' "$(command -v pg_ctl)"; } > "$SHIM19/pg_ctl"
chmod +x "$SHIM19/pg_ctl"
if out="$( PATH="$SHIM19:$PATH" GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_EPHEMERAL_ROOT="$R19" bash "$SETUP" --down 2>&1 )"; then
  pbad "19: setup --down should FAIL (non-zero) on an UNKNOWN (status 4) shutdown"
else
  if printf '%s' "$out" | grep -q "not CONFIRMED stopped" && [ -f "$R19/pgdata/PG_VERSION" ] && [ -f "$R19/.guvfx_ephemeral" ]; then pok "19: setup --down refuses on status 4 (UNKNOWN) and PRESERVES data dir + state"; else pbad "19: wrong refusal/evidence on status 4 (out: $(printf '%s' "$out" | grep -i refused | tail -1))"; fi
fi
down "$R19" >/dev/null 2>&1 || true   # real teardown cleans it up

echo; note "=== 20: failed-setup cleanup does NOT delete a LIVE cluster when pg_ctl start REPORTS failure but the postmaster is up ==="
# Reachable case: `pg_ctl -w start` can exit non-zero (e.g. PGCTLTIMEOUT) while the postmaster it forked
# keeps running. The failed-setup trap (cleanup_up) must still stop+confirm before any rm, NOT rm a live
# cluster. Shim: `start` runs the REAL start (postmaster comes up) then reports failure (exit 1); `stop`
# is a no-op (postmaster stays up); `status` delegates to real (reports running). With the guard keyed on
# start-ATTEMPTED, cleanup_up refuses to delete and PRESERVES the live data dir.
R20="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_t20.XXXXXX")"
SHIM20="$R20/shim20"; mkdir -p "$SHIM20"; REAL20="$(command -v pg_ctl)"
{ printf '#!/usr/bin/env bash\n'
  printf 'for a in "$@"; do\n'
  printf '  [ "$a" = "start" ] && { "%s" "$@"; exit 1; }\n' "$REAL20"
  printf '  [ "$a" = "stop" ] && exit 0\n'
  printf 'done\n'
  printf 'exec "%s" "$@"\n' "$REAL20"; } > "$SHIM20/pg_ctl"
chmod +x "$SHIM20/pg_ctl"
( PATH="$SHIM20:$PATH" GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$MAINSYS" GUVFX_RECOVERY_TESTER_PASSWORD="$PW" GUVFX_RECOVERY_EPHEMERAL_ROOT="$R20" bash "$SETUP" --up ) >/dev/null 2>&1 || true
if [ -f "$R20/pgdata/PG_VERSION" ] && pg_ctl -D "$R20/pgdata" status >/dev/null 2>&1; then
  pok "20: start-reports-failure-but-running -> failed-setup cleanup PRESERVED the live data dir (did not rm a running cluster)"
else
  pbad "20: live data dir was removed (or cluster not running) after a start-reports-failure -- cleanup deleted under a live server"
fi
# real teardown: stop the leaked postmaster + remove its dir
pg_ctl -D "$R20/pgdata" -w -m immediate stop >/dev/null 2>&1 || true; rm -rf "$R20" 2>/dev/null || true

echo; note "================= PROVISIONER CERTIFY SUMMARY: PASS=$PASS FAIL=$FAIL ================="
[ "$FAIL" -eq 0 ] || { note "PROVISIONER CERTIFICATION FAILED"; exit 1; }
note "PROVISIONER CERTIFICATION PASSED"
