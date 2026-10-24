#!/usr/bin/env bash
# GuvFX recovery tooling -- client/server MAJOR compatibility gate certification.
#
# Proves rec_require_client_server_compat (lib.sh) ACCEPTS a compatible client selection and REJECTS an
# incompatible one, with BOTH positive and negative controls so a trivially-always-pass or
# always-fail regression cannot pass vacuously (RULE 11). No database, no network, no secrets.
#
# Run:  bash ops/recovery/tests/test_client_compat.sh
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REC="$(cd "$HERE/.." && pwd)"
# shellcheck source=../lib.sh
. "$REC/lib.sh"
# lib.sh enables errexit/nounset/pipefail when sourced; this harness drives its own control flow.
set +e +u +o pipefail

pass=0; fail=0
# expect: "ok" => function returns 0 ; "refuse" => function exits non-zero (rec_die)
check() {
  local desc="$1" expect="$2" dmaj="$3" smaj="$4" cls="$5" envassign="${6:-}"
  local out rc
  # Run in a $(...) subshell so the optional env assignment (NAME=VALUE) and any rec_die exit are
  # isolated from this harness. rec_die/rec_log write to stderr, so capture both streams.
  out="$( { [ -n "$envassign" ] && export "$envassign"; rec_require_client_server_compat "$dmaj" "$smaj" "$cls"; } 2>&1 )"
  rc=$?
  local got; if [ "$rc" -eq 0 ]; then got="ok"; else got="refuse"; fi
  if [ "$got" = "$expect" ]; then
    printf 'PASS  [%s] dmaj=%s smaj=%s cls=%s %s -> %s\n' "$desc" "$dmaj" "$smaj" "$cls" "${envassign:-}" "$got"
    pass=$((pass+1))
  else
    printf 'FAIL  [%s] dmaj=%s smaj=%s cls=%s %s -> expected %s, got %s | msg: %s\n' \
      "$desc" "$dmaj" "$smaj" "$cls" "${envassign:-}" "$expect" "$got" "$(printf '%s' "$out" | tail -1)"
    fail=$((fail+1))
  fi
}

echo "== client/server major compatibility gate =="
# POSITIVE control: exact parity must PASS for production (else every negative below is vacuous).
check "prod exact parity 15/15"        ok     15 15 production
check "prod exact parity 16/16"        ok     16 16 production
# NEGATIVE: newer client vs prod is REFUSED without override (the real finding: pg_dump 17 -> PG15).
check "prod newer client 17/15 (no override)" refuse 17 15 production
check "prod newer client 16/15 (no override)" refuse 16 15 production
# Override path: an explicit, deliberate accept-the-risk flag PASSES (with a warning).
check "prod newer client 16/15 + override"    ok     16 15 production "GUVFX_BACKUP_ALLOW_CLIENT_MAJOR_MISMATCH=1"
check "prod newer client 17/15 + override"    ok     17 15 production "GUVFX_BACKUP_ALLOW_CLIENT_MAJOR_MISMATCH=1"
# Older client is refused for EVERY class (incomplete-dump risk), override notwithstanding.
check "prod older client 14/15 (refused)"      refuse 14 15 production
check "prod older client 14/15 + override still refused" refuse 14 15 production "GUVFX_BACKUP_ALLOW_CLIENT_MAJOR_MISMATCH=1"
check "test older client 14/15 (refused)"      refuse 14 15 test
# Non-production: parity preferred but a newer client is allowed (warn, not refuse).
check "test newer client 16/15 (allowed)"      ok     16 15 test
check "dev exact parity 15/15"                 ok     15 15 dev
# Malformed majors fail closed.
check "non-numeric client major"               refuse xx 15 production
check "non-numeric server major"               refuse 15 "" production

echo "---- $pass passed, $fail failed ----"
[ "$fail" -eq 0 ] || { echo "CLIENT-COMPAT CERTIFICATION FAILED"; exit 1; }
echo "CLIENT-COMPAT CERTIFICATION PASSED"
