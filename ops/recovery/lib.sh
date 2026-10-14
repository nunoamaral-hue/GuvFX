# shellcheck shell=bash
# GuvFX recovery tooling — shared library (DARK; non-mutating by default; fail-closed).
#
# This file is SOURCED by the recovery scripts. It holds the safety core: the
# production guard, target-class gate, no-secret config loading, logging, and
# preflight helpers. It performs NO action on its own.
#
# SECURITY CONTRACT (Gate D):
#   - No secret is ever hard-coded or logged. The DB password comes from the
#     standard libpq channels (PGPASSWORD / ~/.pgpass / PGSERVICE) and the backup
#     passphrase from GUVFX_BACKUP_PASSPHRASE or GUVFX_BACKUP_PASSPHRASE_FILE.
#   - Target class MUST be declared (GUVFX_RECOVERY_TARGET_CLASS = dev|test|
#     production). Unset => fail closed.
#   - A mutating op against a production target is refused unless BOTH
#     GUVFX_RECOVERY_ALLOW_PRODUCTION=1 and the caller passes --i-understand-production.
#   - RESTORE into a production target is refused CATEGORICALLY (a production
#     restore is a separately-governed manual operation, never run by this tool).
#
# ASCII-only on purpose (portable parsing across shells/encodings).

set -o errexit
set -o nounset
set -o pipefail

REC_VERSION="0.1.0-dark"

rec_ts()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
rec_log() { printf '%s [recovery] %s\n' "$(rec_ts)" "$*" >&2; }
rec_err() { printf '%s [recovery][ERROR] %s\n' "$(rec_ts)" "$*" >&2; }
rec_die() { rec_err "$*"; exit 1; }

# --- command availability -------------------------------------------------
rec_require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || rec_die "required command not found: $c"
  done
}

rec_sha256() {
  # Portable sha256 (Linux: sha256sum; macOS: shasum -a 256). Prints the hash only.
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  elif command -v shasum  >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
  else rec_die "no sha256 tool (sha256sum/shasum) available"; fi
}

# --- target class / connection (no secrets) -------------------------------
# Connection comes from standard libpq env: PGHOST PGPORT PGUSER PGDATABASE.
# We never read or print PGPASSWORD.
rec_dsn_summary() {
  printf 'host=%s port=%s db=%s user=%s class=%s' \
    "${PGHOST:-<unset>}" "${PGPORT:-5432}" "${PGDATABASE:-<unset>}" \
    "${PGUSER:-<unset>}" "${GUVFX_RECOVERY_TARGET_CLASS:-<UNDECLARED>}"
}

rec_target_class() {
  local cls="${GUVFX_RECOVERY_TARGET_CLASS:-}"
  case "$cls" in
    dev|test|production) printf '%s' "$cls" ;;
    "") rec_die "GUVFX_RECOVERY_TARGET_CLASS is not set -- declare the target (dev|test|production). Fail-closed." ;;
    *)  rec_die "GUVFX_RECOVERY_TARGET_CLASS='$cls' is invalid -- must be dev|test|production." ;;
  esac
}

# Defense-in-depth against a MISLABELLED class. If the operator has declared the production
# DSN identity (GUVFX_RECOVERY_PRODUCTION_HOST substring / GUVFX_RECOVERY_PRODUCTION_DB exact)
# and the LIVE libpq connection matches it while the declared class is NOT production, FAIL
# CLOSED -- a stale/wrong class label must never silently permit a mutation of a
# production-looking DSN. (The class is still the primary gate; this is a configured backstop,
# strongly recommended in the production operator environment.)
rec_assert_dsn_vs_class() {
  local cls="$1" ph="${GUVFX_RECOVERY_PRODUCTION_HOST:-}" pd="${GUVFX_RECOVERY_PRODUCTION_DB:-}"
  [ "$cls" = "production" ] && return 0
  if [ -n "$ph" ] && [ -n "${PGHOST:-}" ] && printf '%s' "${PGHOST}" | grep -qF "$ph"; then
    rec_die "REFUSED: PGHOST matches GUVFX_RECOVERY_PRODUCTION_HOST but class='$cls' -- a production DSN cannot be used under a non-production class. Fail-closed."
  fi
  if [ -n "$pd" ] && [ -n "${PGDATABASE:-}" ] && [ "${PGDATABASE}" = "$pd" ]; then
    rec_die "REFUSED: PGDATABASE matches GUVFX_RECOVERY_PRODUCTION_DB but class='$cls' -- a production DSN cannot be used under a non-production class. Fail-closed."
  fi
  return 0
}

# Guard a MUTATING operation. $1 = operation name, $2 = "restore" to apply the
# categorical production-restore refusal. The caller must export REC_PROD_ACK=1
# (set from its --i-understand-production flag) for a production-allowed op.
rec_guard_mutation() {
  local op="$1" kind="${2:-generic}" cls
  cls="$(rec_target_class)"
  rec_assert_dsn_vs_class "$cls"   # mislabel backstop (fires only if prod DSN identity is configured)
  if [ "$cls" != "production" ]; then
    rec_log "mutation '$op' permitted against non-production target ($(rec_dsn_summary))"
    return 0
  fi
  # Target class is production.
  if [ "$kind" = "restore" ]; then
    rec_die "REFUSED: '$op' restores into a PRODUCTION target. This tool never restores into production -- a production restore is a separately-governed manual operation."
  fi
  if [ "${GUVFX_RECOVERY_ALLOW_PRODUCTION:-0}" != "1" ] || [ "${REC_PROD_ACK:-0}" != "1" ]; then
    rec_die "REFUSED: '$op' targets PRODUCTION. Requires BOTH GUVFX_RECOVERY_ALLOW_PRODUCTION=1 and --i-understand-production. Fail-closed."
  fi
  rec_log "WARNING: '$op' permitted against PRODUCTION under explicit double-gate ($(rec_dsn_summary))"
}

# --- encryption key presence (no secret printed) --------------------------
# Returns 0 and echoes the passphrase source if a key is configured, else 1.
rec_passphrase_source() {
  if [ -n "${GUVFX_BACKUP_PASSPHRASE_FILE:-}" ]; then
    [ -r "${GUVFX_BACKUP_PASSPHRASE_FILE}" ] || rec_die "GUVFX_BACKUP_PASSPHRASE_FILE not readable"
    printf 'file'; return 0
  fi
  if [ -n "${GUVFX_BACKUP_PASSPHRASE:-}" ]; then printf 'env'; return 0; fi
  return 1
}

# --- disk / size preflight -------------------------------------------------
rec_free_bytes() {
  # Free bytes on the filesystem holding $1 (portable-ish).
  df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4 * 1024}'
}

rec_psql_scalar() {
  # Run a read-only scalar query against the configured target. No password handling here.
  psql -v ON_ERROR_STOP=1 -tAqc "$1"
}

# A target DB is "empty" only if it has NO user relations of any persistent kind in ANY
# non-system schema: ordinary tables ('r'), partitioned parents ('p'), matviews ('m') and
# foreign tables ('f'). (Counting only ordinary tables in 'public' would mislabel a populated
# non-public schema / matview-only DB as empty and skip the overwrite guard.)
rec_target_is_empty() {
  local n
  n="$(rec_psql_scalar "select count(*) from pg_class c join pg_namespace s on s.oid=c.relnamespace where s.nspname not in ('pg_catalog','information_schema','pg_toast') and s.nspname !~ '^pg_temp' and c.relkind in ('r','p','m','f')")" || return 2
  [ "${n:-0}" = "0" ]
}

# Portable ISO8601-UTC -> epoch seconds (GNU date, then BSD/macOS date). Returns 1 on failure.
rec_epoch() {
  local ts="$1" e
  e="$(date -u -d "$ts" +%s 2>/dev/null)" && { printf '%s' "$e"; return 0; }
  e="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null)" && { printf '%s' "$e"; return 0; }
  return 1
}
