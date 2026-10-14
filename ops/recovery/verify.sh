#!/usr/bin/env bash
# GuvFX recovery tooling — backup integrity + readability verification (DARK; read-only).
#
# Verifies a backup artifact WITHOUT restoring it:
#   1. metadata sidecar present + parseable,
#   2. sha256(artifact) matches the recorded digest (integrity / not truncated),
#   3. pg_restore --list reads a NON-EMPTY archive TOC (valid, non-truncated custom-format dump).
#      For an ENCRYPTED artifact the ciphertext is STREAMED through gpg into pg_restore --list, so
#      the check proves BOTH (a) the ciphertext decrypts (passphrase/MDC OK) AND (b) the decrypted
#      content really is a PostgreSQL custom-format archive -- never just that decryption succeeded.
#      No plaintext is ever written to disk.
# A passing pg_dump is NOT restoration certification; this is the cheap pre-restore gate.
# Fail-closed: any mismatch/failure exits non-zero. Never writes to any database.
#
# Usage: ops/recovery/verify.sh --meta /path/to/guvfx_<label>_<stamp>.meta.json
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

META=""
while [ $# -gt 0 ]; do
  case "$1" in
    --meta) META="${2:-}"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) rec_die "unknown arg: $1" ;;
  esac
  shift
done
[ -n "$META" ] && [ -r "$META" ] || rec_die "--meta FILE is required and must be readable"
rec_require_cmd pg_restore awk

# Minimal JSON field extractor (ASCII, no jq dependency): "key": value.
meta_get() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$META" | head -1; }

dir="$(cd "$(dirname "$META")" && pwd)"
artifact="$dir/$(meta_get artifact)"
want_sha="$(meta_get sha256)"
encrypted="$(meta_get encrypted)"
rec_log "verify: artifact=$artifact encrypted=$encrypted"
[ -r "$artifact" ] || rec_die "artifact not found/readable: $artifact"

got_sha="$(rec_sha256 "$artifact")"
# sha256 vs the sidecar detects CORRUPTION / TRUNCATION / accidental modification. It is NOT a MAC:
# the sidecar is unsigned, so a determined tamperer who also rewrites the sidecar is not caught here
# (a signed/MAC'd manifest is a tracked follow-up). This check is integrity, not authenticity.
[ "$got_sha" = "$want_sha" ] || rec_die "sha256 MISMATCH: recorded=$want_sha actual=$got_sha -- artifact differs from its recorded digest (corruption/truncation/accidental modification). Fail-closed."
rec_log "sha256 OK ($got_sha)"

if [ "$encrypted" = "true" ]; then
  rec_require_cmd gpg
  pp_src="$(rec_passphrase_source)" || rec_die "artifact is encrypted but no passphrase configured (GUVFX_BACKUP_PASSPHRASE[_FILE])"
  # Two streamed passes (no plaintext file; passphrase on fd 3, never argv):
  #   PASS 1 -- FULL decrypt to /dev/null, whose gpg EXIT STATUS is enforced. This catches a wrong
  #   passphrase and, crucially, a TRUNCATED / data-section-corrupted ciphertext whose gpg MDC fails
  #   at the END of the stream. (pg_restore --list alone cannot catch this: the -Fc TOC sits at the
  #   FRONT, so a front-intact but tail-corrupt dump would read a TOC before gpg errored -- so the
  #   format pass must NOT be the only check. R2 review MEDIUM.)
  #   PASS 2 -- decrypt | pg_restore --list, validating the decrypted bytes ARE a non-empty PostgreSQL
  #   custom-format archive, so encrypted NON-PostgreSQL content (which decrypts fine) is REJECTED
  #   (MEDIUM-1): successful decryption alone is NOT archive verification.
  rec_log "test-decrypting (AES256, to stdout; NO plaintext on disk) + validating PostgreSQL archive TOC via $pp_src"
  if [ "$pp_src" = "file" ]; then feed() { cat "$GUVFX_BACKUP_PASSPHRASE_FILE"; }; else feed() { printf '%s' "$GUVFX_BACKUP_PASSPHRASE"; }; fi
  gpg --batch --yes --quiet --decrypt --passphrase-fd 3 "$artifact" >/dev/null 3< <(feed) \
    || rec_die "GPG decryption/MDC integrity FAILED (wrong passphrase / corrupt / truncated ciphertext). Fail-closed."
  toc="$(gpg --batch --yes --quiet --decrypt --passphrase-fd 3 "$artifact" 3< <(feed) 2>/dev/null | pg_restore --list 2>/dev/null | grep -cvE '^(;|$)' || true)"
  [ "${toc:-0}" -gt 0 ] || rec_die "decrypted content is not a readable, non-empty PostgreSQL custom-format archive (e.g. encrypted non-PostgreSQL data). Fail-closed."
  rec_log "decryption + MDC integrity OK; PostgreSQL archive TOC OK ($toc entries)"
else
  n="$(pg_restore --list "$artifact" 2>/dev/null | grep -cvE '^(;|$)' || true)"
  [ "${n:-0}" -gt 0 ] || rec_die "pg_restore --list produced no TOC entries -- not a readable custom-format dump. Fail-closed."
  rec_log "pg_restore TOC OK ($n entries)"
fi
rec_log "VERIFY PASSED for $(basename "$artifact")"
