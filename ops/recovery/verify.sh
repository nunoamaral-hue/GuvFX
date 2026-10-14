#!/usr/bin/env bash
# GuvFX recovery tooling — backup integrity + readability verification (DARK; read-only).
#
# Verifies a backup artifact WITHOUT restoring it:
#   1. metadata sidecar present + parseable,
#   2. sha256(artifact) matches the recorded digest (integrity / not truncated),
#   3. if encrypted: a test GPG decryption succeeds (passphrase + ciphertext OK),
#   4. pg_restore --list reads the archive TOC (valid, non-truncated custom-format dump).
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
  # Decrypt to STDOUT -> /dev/null: proves the ciphertext is complete + decryptable (gpg MDC catches
  # truncation/tamper) WITHOUT ever writing plaintext to disk. Passphrase on fd 3 (never argv).
  rec_log "test-decrypting (AES256, to stdout; NO plaintext on disk) via $pp_src"
  if [ "$pp_src" = "file" ]; then feed() { cat "$GUVFX_BACKUP_PASSPHRASE_FILE"; }; else feed() { printf '%s' "$GUVFX_BACKUP_PASSPHRASE"; }; fi
  gpg --batch --yes --quiet --decrypt --passphrase-fd 3 "$artifact" >/dev/null 3< <(feed) \
    || rec_die "GPG decryption FAILED (wrong passphrase / corrupt / truncated ciphertext). Fail-closed."
  rec_log "decryption + MDC integrity OK (ciphertext complete and decryptable)"
else
  n="$(pg_restore --list "$artifact" 2>/dev/null | grep -cvE '^(;|$)' || true)"
  [ "${n:-0}" -gt 0 ] || rec_die "pg_restore --list produced no TOC entries -- not a readable custom-format dump. Fail-closed."
  rec_log "pg_restore TOC OK ($n entries)"
fi
rec_log "VERIFY PASSED for $(basename "$artifact")"
