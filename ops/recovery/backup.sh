#!/usr/bin/env bash
# GuvFX recovery tooling — consistent logical backup (DARK; dry-run by default).
#
# Produces a single consistent pg_dump custom-format archive (-Fc) of the configured
# target, an optional GPG-symmetric-encrypted copy, and a recovery-point METADATA
# sidecar (JSON) carrying provenance + a sha256 integrity digest. Read-only on the DB,
# but gated: a backup of a PRODUCTION target is refused unless the explicit double-gate
# is set (this packet never sets it). DEFAULT IS DRY-RUN -- pass --execute to act.
#
# Connection: standard libpq env (PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD|~/.pgpass).
# Encryption passphrase: GUVFX_BACKUP_PASSPHRASE or GUVFX_BACKUP_PASSPHRASE_FILE.
#
# Usage:
#   GUVFX_RECOVERY_TARGET_CLASS=test PGDATABASE=... ops/recovery/backup.sh --out /tmp/bk
#   ... add --execute to actually create the backup.
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

EXECUTE=0; OUT_DIR=""; LABEL="manual"; NO_ENCRYPT=0; REC_PROD_ACK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --out) OUT_DIR="${2:-}"; shift ;;
    --label) LABEL="${2:-manual}"; shift ;;
    --no-encrypt) NO_ENCRYPT=1 ;;
    --i-understand-production) REC_PROD_ACK=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) rec_die "unknown arg: $1" ;;
  esac
  shift
done
export REC_PROD_ACK
[ -n "$OUT_DIR" ] || rec_die "--out DIR is required"

umask 077   # all artifacts + the metadata sidecar are created owner-only (Gate D)
# Bound how long the (read-only) dump may wait on a lock, so a gated production backup can
# never stall the live trading DB indefinitely -- it ERRORS out instead of hanging. Configurable
# (ms); do NOT set a statement_timeout (a large-table dump legitimately takes a while).
export PGOPTIONS="${PGOPTIONS:-} -c lock_timeout=${GUVFX_BACKUP_LOCK_TIMEOUT_MS:-60000}"

# Fail-safe erasure of a transient plaintext dump on ANY exit (incl. a failed/interrupted
# encryption) -- the plaintext must never survive when encryption was intended (Gate D, HIGH).
PLAINTEXT_TO_SHRED=""
cleanup_backup() {
  [ -n "$PLAINTEXT_TO_SHRED" ] && [ -e "$PLAINTEXT_TO_SHRED" ] && { shred -u "$PLAINTEXT_TO_SHRED" 2>/dev/null || rm -f "$PLAINTEXT_TO_SHRED"; }
  return 0
}
trap cleanup_backup EXIT INT TERM

rec_require_cmd pg_dump psql awk df date
cls="$(rec_target_class)"
rec_guard_mutation "backup" generic   # read-only on DB, but a prod backup needs the double-gate

# --- preflight (read-only) -------------------------------------------------
rec_log "preflight: target $(rec_dsn_summary)"
srv_ver="$(rec_psql_scalar "show server_version" 2>/dev/null)" || rec_die "cannot reach target DB (preflight)"
# Extract the leading numeric version (robust against suffixes like ' (Homebrew)').
srv_num="$(printf '%s' "$srv_ver" | grep -oE '[0-9]+(\.[0-9]+)*' | head -1)"; srv_major="${srv_num%%.*}"
dump_ver="$(pg_dump --version | grep -oE '[0-9]+(\.[0-9]+)*' | head -1)"; dump_major="${dump_ver%%.*}"
rec_log "preflight: server=$srv_ver pg_dump=$dump_ver"
[ "$dump_major" -ge "$srv_major" ] || rec_die "pg_dump major ($dump_major) < server major ($srv_major) -- a dump taken with an older client can be incomplete/incompatible"

db_bytes="$(rec_psql_scalar "select pg_database_size(current_database())")" || rec_die "cannot read database size"
free_bytes="$(rec_free_bytes "$OUT_DIR" 2>/dev/null || rec_free_bytes "$(dirname "$OUT_DIR")")"
# A custom-format dump is compressed (~0.6x DB). When ENCRYPTING, the plaintext .dump and the
# .dump.gpg COEXIST until the plaintext is shredded post-encryption, so the transient peak is
# ~1.2x. Budget for the worst case so a backup can't fill a near-full FS mid-encryption.
if [ "$NO_ENCRYPT" = "1" ]; then factor=6; else factor=12; fi
need_bytes=$(( db_bytes * factor / 10 + 104857600 ))
rec_log "preflight: db_size=$db_bytes B, est_need=$need_bytes B (factor $((factor/10)).$((factor%10))x), free=$free_bytes B at $OUT_DIR"
if [ "${free_bytes:-0}" -lt "$need_bytes" ]; then
  rec_die "preflight: insufficient free space (need ~$need_bytes B, have $free_bytes B). Fail-closed."
fi

# Encryption policy: production-class backups MUST be encrypted.
pp_src=""; if rec_passphrase_source >/dev/null 2>&1; then pp_src="$(rec_passphrase_source)"; fi
if [ "$cls" = "production" ] && [ "$NO_ENCRYPT" = "1" ]; then
  rec_die "REFUSED: --no-encrypt is not permitted for a production-class backup."
fi
if [ "$NO_ENCRYPT" = "0" ] && [ -z "$pp_src" ]; then
  if [ "$cls" = "production" ]; then
    rec_die "REFUSED: production backup requires an encryption passphrase (GUVFX_BACKUP_PASSPHRASE[_FILE]); none set."
  fi
  rec_log "WARNING: no passphrase configured; backup will be UNENCRYPTED (allowed only for $cls)."
  NO_ENCRYPT=1
fi
[ "$NO_ENCRYPT" = "0" ] && rec_require_cmd gpg

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
base="guvfx_${LABEL}_${stamp}"
archive="$OUT_DIR/${base}.dump"
meta="$OUT_DIR/${base}.meta.json"
git_sha="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"

if [ "$EXECUTE" = "0" ]; then
  rec_log "DRY-RUN (default). Would create:"
  rec_log "  archive: $archive  (pg_dump -Fc)"
  [ "$NO_ENCRYPT" = "0" ] && rec_log "  encrypted: ${archive}.gpg  (AES256, passphrase from $pp_src)"
  rec_log "  metadata: $meta"
  rec_log "Preflight PASSED. Re-run with --execute to create the backup."
  exit 0
fi

# --- execute ---------------------------------------------------------------
mkdir -p "$OUT_DIR"
lsn="$(rec_psql_scalar "select pg_current_wal_lsn()" 2>/dev/null || echo unknown)"
# Arm the plaintext fail-safe BEFORE writing any plaintext when encryption is intended, so an
# interrupt DURING pg_dump / hashing (not only during gpg) also shreds it (closes the arming window).
[ "$NO_ENCRYPT" = "0" ] && PLAINTEXT_TO_SHRED="$archive"
rec_log "executing pg_dump -Fc -> $archive"
pg_dump -Fc -Z 6 -f "$archive"
bytes="$(wc -c < "$archive" | tr -d ' ')"
sha="$(rec_sha256 "$archive")"

final="$archive"; enc="false"
if [ "$NO_ENCRYPT" = "0" ]; then
  rec_log "encrypting (AES256) -> ${archive}.gpg"
  if [ "$pp_src" = "file" ]; then
    gpg --batch --yes --symmetric --cipher-algo AES256 \
        --passphrase-file "$GUVFX_BACKUP_PASSPHRASE_FILE" -o "${archive}.gpg" "$archive"
  else
    printf '%s' "$GUVFX_BACKUP_PASSPHRASE" | gpg --batch --yes --symmetric --cipher-algo AES256 \
        --passphrase-fd 0 -o "${archive}.gpg" "$archive"
  fi
  shred -u "$archive" 2>/dev/null || rm -f "$archive"
  PLAINTEXT_TO_SHRED=""           # plaintext removed on the success path; disarm the trap
  final="${archive}.gpg"; enc="true"; bytes="$(wc -c < "$final" | tr -d ' ')"; sha="$(rec_sha256 "$final")"
fi

cat > "$meta" <<JSON
{
  "schema": "guvfx.recovery.recovery_point/1",
  "label": "$LABEL",
  "created_utc": "$(rec_ts)",
  "target_class": "$cls",
  "database": "${PGDATABASE:-}",
  "server_version": "$srv_ver",
  "pg_dump_version": "$dump_ver",
  "dump_format": "custom(-Fc)",
  "wal_lsn": "$lsn",
  "artifact": "$(basename "$final")",
  "encrypted": $enc,
  "cipher": "$([ "$enc" = true ] && echo AES256 || echo none)",
  "bytes": $bytes,
  "sha256": "$sha",
  "source_db_bytes": $db_bytes,
  "git_sha": "$git_sha",
  "tool_version": "$REC_VERSION"
}
JSON
rec_log "backup complete: $final ($bytes B, sha256=$sha)"
rec_log "metadata: $meta"
rec_log "NOTE: on-host only. Copy to OFF-HOST storage to protect against host/storage loss (Gate D)."
