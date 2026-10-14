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
[ "$got_sha" = "$want_sha" ] || rec_die "sha256 MISMATCH: recorded=$want_sha actual=$got_sha (corrupt/truncated/tampered). Fail-closed."
rec_log "sha256 OK ($got_sha)"

umask 077   # any decrypted temp is owner-only
tmp=""; cleanup() { [ -n "$tmp" ] && { shred -u "$tmp" 2>/dev/null || rm -f "$tmp"; }; return 0; }
trap cleanup EXIT INT TERM   # shred the decrypted plaintext even on operator interrupt
toc_target="$artifact"
if [ "$encrypted" = "true" ]; then
  rec_require_cmd gpg
  pp_src="$(rec_passphrase_source)" || rec_die "artifact is encrypted but no passphrase configured (GUVFX_BACKUP_PASSPHRASE[_FILE])"
  tmp="$(mktemp "${TMPDIR:-/tmp}/guvfx_verify.XXXXXX")"
  rec_log "test-decrypting (AES256) via passphrase source: $pp_src"
  if [ "$pp_src" = "file" ]; then
    gpg --batch --yes --quiet --decrypt --passphrase-file "$GUVFX_BACKUP_PASSPHRASE_FILE" -o "$tmp" "$artifact" \
      || rec_die "GPG decryption FAILED (wrong passphrase or corrupt ciphertext). Fail-closed."
  else
    printf '%s' "$GUVFX_BACKUP_PASSPHRASE" | gpg --batch --yes --quiet --decrypt --passphrase-fd 0 -o "$tmp" "$artifact" \
      || rec_die "GPG decryption FAILED (wrong passphrase or corrupt ciphertext). Fail-closed."
  fi
  rec_log "decryption OK"
  toc_target="$tmp"
fi

n="$(pg_restore --list "$toc_target" 2>/dev/null | grep -cvE '^(;|$)' || true)"
[ "${n:-0}" -gt 0 ] || rec_die "pg_restore --list produced no TOC entries -- archive is not a readable custom-format dump. Fail-closed."
rec_log "pg_restore TOC OK ($n entries)"
rec_log "VERIFY PASSED for $(basename "$artifact")"
