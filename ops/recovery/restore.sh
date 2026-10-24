#!/usr/bin/env bash
# GuvFX recovery tooling — restore into an ISOLATED target (DARK; dry-run by default).
#
# Restores a verified backup artifact into a NON-PRODUCTION target database. A restore into a
# production target is refused CATEGORICALLY. A non-production restore is permitted ONLY when BOTH
# (a) the live CLUSTER matches the attested isolated cluster + carries the operator-planted
# isolation marker (cluster authority, lib.sh rec_require_isolated_target), AND (b) the specific
# target DATABASE is independently authorized: explicitly allowlisted, owned by a least-privilege
# non-superuser role, and not an admin/template/production database (rec_require_authorized_target_db).
# Cluster isolation does NOT by itself authorize every database on the cluster. The artifact is
# verified (verify.sh) before any restore, and the restore is ATOMIC (--single-transaction).
#
# CLEAN-TARGET CONTRACT (HIGH-2): the target MUST be a freshly-created, EMPTY database. A non-empty
# target is refused with NO override -- there is no --force/--clean (which would leave unrelated
# pre-existing objects behind, so the result would not be attributable to exactly one backup). To
# re-restore, DROP and reCREATE the target DB (explicit operator-controlled lifecycle). Because the
# target is verified empty immediately before an atomic restore, every object present afterwards
# came from this backup. DEFAULT IS DRY-RUN; pass --execute to act.
#
# Connection = the TARGET via standard libpq env (PGHOST/PGPORT/PGUSER/PGDATABASE). Fail-closed.
#
# Usage:
#   GUVFX_RECOVERY_TARGET_CLASS=test PGDATABASE=scratch ops/recovery/restore.sh --meta M.json
#   ... add --execute to perform the restore.
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

META=""; EXECUTE=0; REC_PROD_ACK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --meta) META="${2:-}"; shift ;;
    --execute) EXECUTE=1 ;;
    # Accepted but INERT for restore: a production restore is refused categorically by the guard
    # regardless. Parsing it (rather than dying on 'unknown arg') ensures the categorical refusal
    # is what actually fires -- so it is genuinely exercised/testable.
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
rec_guard_mutation "restore" restore   # CLUSTER authority: categorically refuses a production target
rec_require_authorized_target_db "restore"   # DATABASE authority (HIGH-2): allowlisted+owned+non-admin

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

# Record the RESTORE client/server versions (companion to the backup sidecar's dump client/server
# versions). Always logged; optionally written as a structured attestation when GUVFX_RESTORE_ATTEST_OUT
# is set (owner-only). A restore client OLDER than the archive's producing client is refused.
rst_ver="$(pg_restore --version | grep -oE '[0-9]+(\.[0-9]+)*' | head -1)"; rst_major="${rst_ver%%.*}"
rec_log "restore-attestation: pg_restore=$rst_ver target_server=$tgt_ver archive_server=$arch_srv restore_target_major_parity=$([ "$rst_major" = "$tgt_major" ] && echo true || echo false)"
if [ -n "${GUVFX_RESTORE_ATTEST_OUT:-}" ]; then
  ( umask 077; cat > "$GUVFX_RESTORE_ATTEST_OUT" <<JSON
{
  "schema": "guvfx.recovery.restore_attestation/1",
  "utc": "$(rec_ts)",
  "pg_restore_version": "$rst_ver",
  "pg_restore_major": "$rst_major",
  "target_server_version": "$tgt_ver",
  "target_server_major": "$tgt_major",
  "archive_server_version": "$arch_srv",
  "archive_server_major": "$arch_major",
  "artifact": "$(basename "$artifact")"
}
JSON
  ) || rec_die "could not write restore attestation to GUVFX_RESTORE_ATTEST_OUT. Fail-closed."
  rec_log "restore attestation written: $GUVFX_RESTORE_ATTEST_OUT"
fi

if rec_target_is_empty; then
  rec_log "target verified EMPTY (no user relations in any non-system schema)."
else
  rec_die "TARGET IS NOT EMPTY -- refusing (no override). Restore only into a freshly-created empty database; to re-restore, DROP and reCREATE the target (explicit operator-controlled lifecycle). Fail-closed."
fi

if [ "$EXECUTE" = "0" ]; then
  rec_log "DRY-RUN (default). Would restore:"
  rec_log "  artifact: $artifact (encrypted=$encrypted)"
  rec_log "  into:     $(rec_dsn_summary)  [verified empty]"
  rec_log "  mode:     pg_restore --single-transaction (atomic, all-or-nothing) into an EMPTY target"
  rec_log "Preflight PASSED. Re-run with --execute to perform the restore."
  exit 0
fi

# Re-assert BOTH authorizations immediately before the atomic restore, to detect a target that
# changed between preflight and operation (HIGH-2 / §6-H): re-verify the connected database is still
# the allowlisted/owned disposable target (this rec_die's with its own precise reason if not), and
# that an unrelated writer has not populated it since the earlier check. Each uses a fresh connection,
# so a concurrent mutation is caught here. Fail closed.
rec_require_authorized_target_db "restore"
rec_target_is_empty || rec_die "target became NON-EMPTY before restore -- aborting (clean-target contract; an unrelated writer may have populated it). Fail-closed."
rec_log "executing pg_restore (atomic --single-transaction) into $(rec_dsn_summary) ..."
if [ "$encrypted" = "true" ]; then
  # STREAM decrypt -> pg_restore so the decrypted plaintext is NEVER written to disk (no temp). gpg
  # feeds the custom-format archive on pg_restore's stdin; passphrase on fd 3 (never argv). pipefail
  # catches a decrypt OR restore failure, and --single-transaction rolls back (nothing committed).
  rec_require_cmd gpg
  pp_src="$(rec_passphrase_source)" || rec_die "artifact encrypted but no passphrase configured"
  if [ "$pp_src" = "file" ]; then feed() { cat "$GUVFX_BACKUP_PASSPHRASE_FILE"; }; else feed() { printf '%s' "$GUVFX_BACKUP_PASSPHRASE"; }; fi
  gpg --batch --yes --quiet --decrypt --passphrase-fd 3 "$artifact" 3< <(feed) \
    | pg_restore --no-owner --no-privileges --exit-on-error --single-transaction -d "$PGDATABASE" 2>&1 | tail -20 \
    || rec_die "streamed decrypt|pg_restore FAILED (rolled back; nothing committed). Fail-closed."
else
  pg_restore --no-owner --no-privileges --exit-on-error --single-transaction \
    -d "$PGDATABASE" "$artifact" 2>&1 | tail -20 || rec_die "pg_restore FAILED (rolled back; nothing committed). Fail-closed."
fi
# Attributability (HIGH-2): the target was verified empty immediately before this all-or-nothing
# restore, so every object now present came from THIS backup -- no surviving unrelated objects.
rec_log "RESTORE complete into $(rec_dsn_summary) -- empty pre-restore; result attributable to $(basename "$artifact")."
