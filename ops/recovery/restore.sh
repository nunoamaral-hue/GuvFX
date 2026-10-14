#!/usr/bin/env bash
# GuvFX recovery tooling — restore into an ISOLATED target (DARK; dry-run by default).
#
# Restores a verified backup artifact into a NON-PRODUCTION target database. A restore
# into a production-class target is refused CATEGORICALLY (rec_guard_mutation ... restore)
# -- a production restore is a separately-governed manual operation, never run by this tool.
# The artifact is verified (verify.sh) before any restore. A non-empty target is refused
# unless --force (which uses pg_restore --clean). DEFAULT IS DRY-RUN; pass --execute to act.
#
# Connection = the TARGET via standard libpq env (PGHOST/PGPORT/PGUSER/PGDATABASE). The
# target DB must already exist (create an empty scratch DB first). Fail-closed throughout.
#
# Usage:
#   GUVFX_RECOVERY_TARGET_CLASS=test PGDATABASE=scratch ops/recovery/restore.sh --meta M.json
#   ... add --execute to perform the restore.
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

META=""; EXECUTE=0; FORCE=0; JOBS=1; REC_PROD_ACK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --meta) META="${2:-}"; shift ;;
    --execute) EXECUTE=1 ;;
    --force) FORCE=1 ;;
    --jobs) JOBS="${2:-1}"; shift ;;
    # Accepted but INERT for restore: a production restore is refused categorically by the guard
    # regardless. Parsing it (rather than dying on 'unknown arg') ensures the categorical refusal
    # is what actually fires -- so it is genuinely exercised/testable (T1 recovery review).
    --i-understand-production) REC_PROD_ACK=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) rec_die "unknown arg: $1" ;;
  esac
  shift
done
export REC_PROD_ACK
[ -n "$META" ] && [ -r "$META" ] || rec_die "--meta FILE is required and must be readable"
rec_require_cmd pg_restore psql awk

cls="$(rec_target_class)"
rec_guard_mutation "restore" restore   # categorically refuses a production target

# Verify the artifact BEFORE touching the target (fail-closed if corrupt/unreadable).
rec_log "verifying artifact before restore ..."
"$HERE/verify.sh" --meta "$META" >&2 || rec_die "pre-restore verification FAILED -- refusing to restore."

meta_get() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$META" | head -1; }
dir="$(cd "$(dirname "$META")" && pwd)"
artifact="$dir/$(meta_get artifact)"
encrypted="$(meta_get encrypted)"
arch_srv="$(meta_get server_version)"; arch_major="${arch_srv%%.*}"

# Target preflight.
tgt_ver="$(rec_psql_scalar "show server_version" 2>/dev/null)" || rec_die "cannot reach TARGET DB ($(rec_dsn_summary))"
tgt_major="${tgt_ver%%.*}"
rec_log "preflight: target_server=$tgt_ver archive_server=$arch_srv"
[ "$tgt_major" -ge "$arch_major" ] || rec_die "target major ($tgt_major) < archive major ($arch_major) -- incompatible restore target. Fail-closed."

if rec_target_is_empty; then
  rec_log "target is empty (no public tables)."
else
  [ "$FORCE" = "1" ] || rec_die "TARGET IS NOT EMPTY and --force not given -- refusing to overwrite. Fail-closed."
  rec_log "WARNING: target not empty; --force set -> pg_restore --clean will drop+recreate objects."
fi

if [ "$EXECUTE" = "0" ]; then
  rec_log "DRY-RUN (default). Would restore:"
  rec_log "  artifact: $artifact (encrypted=$encrypted)"
  rec_log "  into:     $(rec_dsn_summary)"
  rec_log "  mode:     $([ "$FORCE" = 1 ] && echo 'pg_restore --clean --if-exists' || echo 'pg_restore (empty target)') jobs=$JOBS"
  rec_log "Preflight PASSED. Re-run with --execute to perform the restore."
  exit 0
fi

# Decrypt if needed, into a temp file, then restore.
umask 077   # any decrypted temp is owner-only
tmp=""; cleanup() { [ -n "$tmp" ] && { shred -u "$tmp" 2>/dev/null || rm -f "$tmp"; }; return 0; }
trap cleanup EXIT INT TERM   # shred the decrypted plaintext even on operator interrupt
src="$artifact"
if [ "$encrypted" = "true" ]; then
  rec_require_cmd gpg
  pp_src="$(rec_passphrase_source)" || rec_die "artifact encrypted but no passphrase configured"
  tmp="$(mktemp "${TMPDIR:-/tmp}/guvfx_restore.XXXXXX")"
  if [ "$pp_src" = "file" ]; then
    gpg --batch --yes --quiet --decrypt --passphrase-file "$GUVFX_BACKUP_PASSPHRASE_FILE" -o "$tmp" "$artifact" || rec_die "decrypt failed"
  else
    printf '%s' "$GUVFX_BACKUP_PASSPHRASE" | gpg --batch --yes --quiet --decrypt --passphrase-fd 0 -o "$tmp" "$artifact" || rec_die "decrypt failed"
  fi
  src="$tmp"
fi

clean_flags=""
[ "$FORCE" = "1" ] && clean_flags="--clean --if-exists"
# Atomicity vs parallelism: --single-transaction (atomic, all-or-nothing) is incompatible
# with --jobs>1. Prefer ATOMIC by default (jobs=1). Parallel restore is opt-in and sacrifices
# atomicity, so we warn and rely on --exit-on-error instead.
txn_flags="--single-transaction"
if [ "$JOBS" -gt 1 ]; then
  txn_flags=""
  rec_log "WARNING: --jobs=$JOBS requested -> parallel restore is NOT atomic (no --single-transaction); relying on --exit-on-error. Prefer jobs=1 for an atomic recovery."
else
  JOBS=1
fi
rec_log "executing pg_restore into $(rec_dsn_summary) (jobs=$JOBS) ..."
# shellcheck disable=SC2086
pg_restore --no-owner --no-privileges --exit-on-error $txn_flags $clean_flags \
  --jobs "$JOBS" -d "$PGDATABASE" "$src" 2>&1 | tail -20 || rec_die "pg_restore FAILED (rolled back if atomic). Fail-closed."
rec_log "RESTORE complete into $(rec_dsn_summary)"
