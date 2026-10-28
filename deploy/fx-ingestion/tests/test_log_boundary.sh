#!/usr/bin/env bash
# Regression test for the FX log-redirection boundary.
#
# The defect this guards against: the FX cron runs as the unprivileged cron user and redirects with
# `>> /var/log/guvfx/<name>.log`; that directory is root-owned 0755, so the cron user could not CREATE a missing
# log file, and a missing redirect target makes the shell fail to start the scheduled command - silently. The
# installer now pre-creates the logs and both installer and verifier test the real `>>` boundary (not just that
# the directory exists). This test exercises the shared helpers with POSITIVE and NEGATIVE controls (RULE 11).
#
# Runs as a normal user: no sudo, no crontab, no network, no production contact. Self-contained in a temp dir.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/fx-ingestion/lib_fx_log.sh
. "${HERE}/../lib_fx_log.sh"

fails=0
ok()  { echo "PASS: $*"; }
bad() { echo "FAIL: $*"; fails=$((fails + 1)); }
_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null; }

TMP="$(mktemp -d)"
trap 'chmod -R u+rwx "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT

# 1) POSITIVE: fx_ensure_log creates a missing log, appendable, at the least-privilege mode.
L="${TMP}/fetch_fx_boe.log"
fx_ensure_log "$L"
[ -f "$L" ] && ok "fx_ensure_log created $L" || bad "fx_ensure_log did not create $L"
if fx_check_appendable "$L"; then ok "created log is appendable"; else bad "created log not appendable"; fi
m="$(_mode "$L")"
[ "$m" = "640" ] && ok "mode is least-privilege 640" || bad "unexpected mode '$m' (want 640)"

# 2) IDEMPOTENT + NON-DESTRUCTIVE: existing content is preserved and re-running does not error.
printf 'existing-line\n' >> "$L"
fx_ensure_log "$L"
if grep -q 'existing-line' "$L"; then ok "fx_ensure_log preserved existing content (idempotent)"; else bad "fx_ensure_log clobbered existing content"; fi

# 3) NEGATIVE CONTROL: fx_check_appendable must DETECT the real failure - a new redirect target the current user
#    cannot create because the containing directory is not user-writable (mirrors the root-owned 0755 production
#    directory). Skipped under root, which bypasses mode bits and so cannot reproduce the denial.
if [ "$(id -u)" = "0" ]; then
  echo "SKIP: running as root - cannot simulate directory-permission denial (root bypasses mode bits)"
else
  RO="${TMP}/rodir"
  mkdir -p "$RO"
  chmod 500 "$RO"   # r-x: owner may traverse/read but not create new entries
  if fx_check_appendable "${RO}/newlog.log"; then
    bad "fx_check_appendable wrongly reported a non-creatable target as appendable"
  else
    ok "fx_check_appendable correctly detected a non-creatable redirect target"
  fi
  chmod 700 "$RO"
fi

echo "----"
if [ "$fails" -eq 0 ]; then
  echo "ALL FX LOG-BOUNDARY TESTS PASSED"
  exit 0
else
  echo "$fails TEST(S) FAILED"
  exit 1
fi
