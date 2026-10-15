#!/usr/bin/env bash
# GuvFX recovery tooling — consistent logical backup (DARK; dry-run by default).
#
# Produces a consistent pg_dump custom-format (-Fc) recovery point of the configured target plus
# a recovery-point METADATA sidecar (JSON: provenance + sha256). When encrypting, pg_dump is
# STREAMED directly into gpg (AES256) so NO plaintext dump is ever written to disk; the artifact
# is written to a temp, integrity-checked, atomically finalized, and only THEN is metadata
# written. Read-only on the DB, but gated: a backup of a PRODUCTION target is refused unless the
# explicit double-gate is set (this packet never sets it). DEFAULT IS DRY-RUN -- pass --execute.
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
# Validate the label (it becomes part of the filename): ASCII word chars only, bounded (HIGH-3 F/G).
printf '%s' "$LABEL" | grep -qE '^[A-Za-z0-9_-]{1,40}$' || rec_die "invalid --label '$LABEL' (allowed [A-Za-z0-9_-], 1-40 chars)."

umask 077   # all artifacts + the metadata sidecar are created owner-only (Gate D)
# Bound how long the (read-only) dump may wait on a lock, so a gated production backup can
# never stall the live trading DB indefinitely -- it ERRORS out instead of hanging. Configurable
# (ms); do NOT set a statement_timeout (a large-table dump legitimately takes a while).
export PGOPTIONS="${PGOPTIONS:-} -c lock_timeout=${GUVFX_BACKUP_LOCK_TIMEOUT_MS:-60000}"

# Fail-closed cleanup: the temp .partial artifact is removed on ANY exit (incl. interrupt), so an
# incomplete/failed backup never leaves a certified-looking artifact. For an ENCRYPTED backup the
# temp is CIPHERTEXT (pg_dump is STREAMED straight into gpg -- plaintext never touches disk), so a
# fragment surviving SIGKILL/power-loss/snapshot is encrypted, not plaintext (HIGH-3).
PARTIAL=""
cleanup_backup() { [ -n "$PARTIAL" ] && [ -e "$PARTIAL" ] && rm -f "$PARTIAL"; return 0; }
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
# Capacity handling (MEDIUM-2). Two layers, because a dump size cannot be known in advance and must
# NOT be presumed from a compression ratio:
#  (1) a CONSERVATIVE ESTIMATE for the pre-flight sanity refusal -- it is an estimate (DB on-disk
#      size + 100 MiB headroom), explicitly NOT a guaranteed maximum (an incompressible or
#      concurrently-growing DB can exceed it); and
#  (2) an ENFORCEABLE output-size CEILING on the single artifact FILE, applied during the run via
#      `ulimit -f` (an rlimit on the size of any file the process writes), so the artifact file cannot
#      grow past `free - reserved_floor` -- i.e. this backup cannot by itself drive the filesystem
#      below the reserved free FLOOR. If the dump would exceed it, the write fails (SIGXFSZ, like
#      ENOSPC); the partial is removed by the trap and NO metadata is written -- an incomplete
#      artifact is never certified. (It bounds the artifact file; it is not a quota over other files.)
# This defends the already-96%-full prod FS without trusting the estimate. Streaming means only ONE
# artifact (ciphertext or plaintext) is ever on disk, so there is no plaintext+ciphertext peak.
reserved_floor="${GUVFX_BACKUP_MIN_FREE_BYTES:-2147483648}"   # keep >= 2 GiB free by default
# Validate the override is a non-negative integer -- a non-integer would make the `[ -lt ]` test
# error to a non-zero status and SKIP the floor check (fail-open). Refuse instead. Same for free_bytes.
case "$reserved_floor" in ''|*[!0-9]*) rec_die "GUVFX_BACKUP_MIN_FREE_BYTES='$reserved_floor' is not a non-negative integer. Fail-closed." ;; esac
case "${free_bytes:-}" in ''|*[!0-9]*) rec_die "could not determine free space at $OUT_DIR (df gave '${free_bytes:-}'). Fail-closed." ;; esac
est_bytes=$(( db_bytes + 104857600 ))   # CONSERVATIVE ESTIMATE for the sanity refusal (NOT a guaranteed max)
rec_log "preflight: db_size=$db_bytes B, conservative_estimate(not a max)=$est_bytes B, reserved_floor=$reserved_floor B, free=$free_bytes B at $OUT_DIR"
if [ "$free_bytes" -lt "$est_bytes" ]; then
  rec_die "preflight: insufficient free space (conservative estimate >= $est_bytes B, have $free_bytes B). Fail-closed."
fi
if [ "$(( free_bytes - est_bytes ))" -lt "$reserved_floor" ]; then
  rec_die "preflight: backup's estimated size would leave < reserved floor ($reserved_floor B) free -- refusing to risk filling the filesystem. Set GUVFX_BACKUP_MIN_FREE_BYTES to override deliberately. Fail-closed."
fi
# ENFORCEABLE output ceiling (bytes): the artifact may never consume the reserved floor. Optionally
# capped lower by GUVFX_BACKUP_MAX_ARTIFACT_BYTES (explicit operator quota). Converted to 512-byte
# blocks for `ulimit -f`. This is a HARD runtime bound, unlike the estimate above.
ceiling_bytes=$(( free_bytes - reserved_floor ))
if [ -n "${GUVFX_BACKUP_MAX_ARTIFACT_BYTES:-}" ]; then
  case "$GUVFX_BACKUP_MAX_ARTIFACT_BYTES" in ''|*[!0-9]*) rec_die "GUVFX_BACKUP_MAX_ARTIFACT_BYTES='$GUVFX_BACKUP_MAX_ARTIFACT_BYTES' is not a non-negative integer. Fail-closed." ;; esac
  [ "$GUVFX_BACKUP_MAX_ARTIFACT_BYTES" -lt "$ceiling_bytes" ] && ceiling_bytes="$GUVFX_BACKUP_MAX_ARTIFACT_BYTES"
fi
[ "$ceiling_bytes" -gt 0 ] || rec_die "computed enforceable output ceiling <= 0 B -- insufficient headroom above the reserved floor. Fail-closed."
# bash's `ulimit -f` unit is 1024-byte blocks (POSIX sh would be 512; this script runs under bash via
# its shebang). Dividing by 1024 (floor) keeps the enforced ceiling AT OR BELOW ceiling_bytes, so it
# never exceeds the intended bound. (A 512 divisor would set a 2x-too-large limit -- R2 review HIGH.)
ceiling_blocks=$(( ceiling_bytes / 1024 ))
[ "$ceiling_blocks" -gt 0 ] || rec_die "enforceable output ceiling < 1024 B -- refusing. Fail-closed."

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
if [ "$NO_ENCRYPT" = "0" ]; then final="$OUT_DIR/${base}.dump.gpg"; else final="$OUT_DIR/${base}.dump"; fi
meta="$OUT_DIR/${base}.meta.json"
git_sha="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
# Never silently overwrite an existing recovery point (HIGH-3 E).
if [ -e "$final" ] || [ -e "$meta" ]; then rec_die "refusing to overwrite an existing recovery point ($final / $meta). Fail-closed."; fi

if [ "$EXECUTE" = "0" ]; then
  rec_log "DRY-RUN (default). Would create:"
  if [ "$NO_ENCRYPT" = "0" ]; then
    rec_log "  encrypted artifact (STREAMED pg_dump -Fc | gpg AES256, passphrase from $pp_src; no plaintext on disk): $final"
  else
    rec_log "  plaintext artifact (pg_dump -Fc; non-production, --no-encrypt): $final"
  fi
  rec_log "  enforceable output ceiling (ulimit -f): $ceiling_bytes B ($ceiling_blocks x 512B blocks) -- a larger dump fails safely (no partial certified)"
  rec_log "  metadata (written LAST, only after integrity check + atomic finalize): $meta"
  rec_log "Preflight PASSED. Re-run with --execute to create the backup."
  exit 0
fi

# --- execute (stream -> temp -> integrity -> atomic finalize -> metadata) ---
mkdir -p "$OUT_DIR"
lsn="$(rec_psql_scalar "select pg_current_wal_lsn()" 2>/dev/null || echo unknown)"
sysid="$(rec_cluster_sysid || echo unknown)"
PARTIAL="$OUT_DIR/.$(basename "$final").partial.$$"   # temp; removed on ANY exit; atomic-renamed on success
enc="false"

if [ "$NO_ENCRYPT" = "0" ]; then
  # Feed the passphrase on fd 3 (NEVER argv): file -> cat FILE; env -> printf (bash builtin, not
  # visible in ps). pg_dump streams on gpg's stdin so plaintext is NEVER written to disk (HIGH-3).
  if [ "$pp_src" = "file" ]; then feed() { cat "$GUVFX_BACKUP_PASSPHRASE_FILE"; }; else feed() { printf '%s' "$GUVFX_BACKUP_PASSPHRASE"; }; fi
  rec_log "streaming encrypted backup: pg_dump -Fc | gpg AES256 -> temp (no plaintext on disk; output ceiling ${ceiling_bytes} B)"
  # `ulimit -f` in a subshell bounds the ciphertext artifact gpg writes: if the dump would exceed the
  # ceiling, gpg dies on SIGXFSZ (ENOSPC-equivalent), the pipeline fails, and the partial is removed
  # by the trap below with NO metadata written. pg_dump writes to the PIPE (not size-limited); gpg
  # writes the regular file (limited). Fail-closed.
  ( ulimit -f "$ceiling_blocks"; pg_dump -Fc -Z 6 | gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase-fd 3 -o "$PARTIAL" 3< <(feed) ) \
    || rec_die "encrypted backup FAILED or exceeded the enforceable output ceiling (${ceiling_bytes} B; reserved floor protected) -- partial removed, no metadata. Fail-closed."
  # integrity-before-finalize: a FULL decrypt to STDOUT -> /dev/null (well-formed, not truncated,
  # MDC intact, passphrase correct) -- no plaintext file on disk. (gpg's '-o /dev/null' is unusable
  # because gpg writes a <output>.part temp; decrypting to stdout avoids that.) Failure -> not
  # finalized, no metadata (HIGH-3 B/C).
  gpg --batch --yes --quiet --decrypt --passphrase-fd 3 "$PARTIAL" >/dev/null 3< <(feed) \
    || rec_die "post-encryption integrity check FAILED (decrypt) -- not finalizing. Fail-closed."
  enc="true"
else
  rec_log "backup (UNENCRYPTED, non-production): pg_dump -Fc -> temp (output ceiling ${ceiling_bytes} B)"
  # Same enforceable ceiling: pg_dump writes the regular file, so a dump exceeding the ceiling dies
  # on SIGXFSZ, the partial is removed by the trap, and no metadata is written. Fail-closed.
  ( ulimit -f "$ceiling_blocks"; pg_dump -Fc -Z 6 -f "$PARTIAL" ) \
    || rec_die "unencrypted backup FAILED or exceeded the enforceable output ceiling (${ceiling_bytes} B) -- partial removed, no metadata. Fail-closed."
  pg_restore --list "$PARTIAL" >/dev/null 2>&1 || rec_die "post-dump integrity check FAILED (unreadable TOC) -- not finalizing. Fail-closed."
fi

bytes="$(wc -c < "$PARTIAL" | tr -d ' ')"
sha="$(rec_sha256 "$PARTIAL")"
if [ -e "$final" ]; then rec_die "refusing to overwrite existing artifact at finalize: $final"; fi
mv "$PARTIAL" "$final"   # atomic within the same directory
PARTIAL=""               # finalized -> disarm the cleanup trap

# Metadata is written LAST -- only after a complete, integrity-checked, atomically-finalized artifact.
cat > "$meta" <<JSON
{
  "schema": "guvfx.recovery.recovery_point/1",
  "label": "$LABEL",
  "created_utc": "$(rec_ts)",
  "target_class": "$cls",
  "database": "${PGDATABASE:-}",
  "system_identifier": "$sysid",
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
rec_log "backup complete: $final ($bytes B, sha256=$sha, encrypted=$enc)"
rec_log "metadata: $meta"
rec_log "NOTE: on-host only. Copy $final + $meta to OFF-HOST storage to protect against host/storage loss (Gate D)."
