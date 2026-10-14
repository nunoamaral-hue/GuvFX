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
#     production). Unset => fail closed. The class is ADVISORY, not authoritative.
#   - AUTHORITATIVE target identity is the LIVE cluster's system_identifier, not a
#     caller label: a NON-production mutation is permitted ONLY when the live cluster
#     matches the operator-attested isolated cluster (GUVFX_RECOVERY_ISOLATED_CLUSTER_ID),
#     and is denied when it matches a declared production identity
#     (GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID / _DB). A mislabelled class cannot override this.
#   - A mutating op against a production-class target is refused unless BOTH
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

# --- AUTHORITATIVE target identity (not a caller-supplied label) ----------
# The live cluster's system_identifier is set at initdb, is unique per cluster, and cannot be
# forged through libpq env / PGSERVICE / connection defaults -- so it, not GUVFX_RECOVERY_TARGET_CLASS,
# is the authoritative target identity (HIGH-1/HIGH-4).
rec_cluster_sysid() { rec_psql_scalar "select system_identifier::text from pg_control_system()" 2>/dev/null; }

# Fail-closed isolation gate for a NON-PRODUCTION mutation. Permit ONLY when the LIVE cluster
# matches the operator-attested isolated cluster id (GUVFX_RECOVERY_ISOLATED_CLUSTER_ID == live
# system_identifier); DENY when it matches a declared production identity
# (GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID / _DB). Any inability to establish identity fails closed.
# A dev/test class label can NEVER override this: a mislabelled class pointed at production is
# refused because the live system_identifier will not match the attested isolated one.
# Positive, cluster-scoped isolation MARKER -- the PRIMARY authoritative signal. system_identifier is
# NOT unique across physical clones: pg_basebackup, filesystem/VM snapshots, PITR clones and streaming
# standbys all PRESERVE the source cluster's system_identifier, so prod and a physical copy of prod are
# indistinguishable by it. Therefore a non-production mutation ALSO requires a dedicated MARKER DATABASE
# to exist on the LIVE cluster -- one PRODUCTION never carries and that an operator creates DELIBERATELY
# only on a genuinely isolated cluster (even a restored clone). Absent => fail closed. (Cluster-scoped,
# so it works for a restore into an empty target; cannot be set via libpq env.)
rec_require_isolation_marker() {
  local op="$1" marker="${GUVFX_RECOVERY_ISOLATION_MARKER_DB:-guvfx_recovery_isolated_ok}" n
  case "$marker" in ""|*[!A-Za-z0-9_]*) rec_die "invalid GUVFX_RECOVERY_ISOLATION_MARKER_DB '$marker' (allowed [A-Za-z0-9_])." ;; esac
  n="$(rec_psql_scalar "select count(*) from pg_database where datname='$marker'" 2>/dev/null)" \
    || rec_die "REFUSED: cannot check the isolation marker database for '$op' -- fail-closed."
  [ "${n:-0}" = "1" ] || rec_die "REFUSED: isolation marker database '$marker' is ABSENT on the live cluster -- '$op' acts ONLY on a cluster EXPLICITLY marked isolated (on your throwaway cluster: createdb $marker). PRODUCTION (and a bare clone of it) must never carry it. system_identifier alone cannot distinguish a clone from production. Fail-closed."
}

rec_require_isolated_target() {
  local op="$1" sysid db
  sysid="$(rec_cluster_sysid)"
  [ -n "$sysid" ] || rec_die "REFUSED: cannot read the live cluster system_identifier for '$op' -- target identity unverifiable. Fail-closed."
  db="$(rec_psql_scalar "select current_database()" 2>/dev/null || true)"
  # DENY first (a clone shares prod's sysid, so this also denies a clone when the prod id is pinned).
  if [ -n "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID:-}" ] && [ "$sysid" = "$GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID" ]; then
    rec_die "REFUSED: live cluster system_identifier matches GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID -- '$op' would act on PRODUCTION (or a physical clone of it). Fail-closed."
  fi
  if [ -n "${GUVFX_RECOVERY_PRODUCTION_DB:-}" ] && [ "$db" = "$GUVFX_RECOVERY_PRODUCTION_DB" ]; then
    rec_die "REFUSED: live current_database='$db' matches GUVFX_RECOVERY_PRODUCTION_DB -- '$op' would act on PRODUCTION. Fail-closed."
  fi
  # AUTHORITATIVE positive signal: the operator-planted isolation marker DB (prod never has it).
  rec_require_isolation_marker "$op"
  # Secondary: the attested isolated cluster id must match the live cluster (defence in depth; a class
  # label can never substitute). NOTE: this is NOT proof of non-production for a physical clone -- the
  # marker above is the signal that distinguishes a prepared isolated clone from bare production.
  local att="${GUVFX_RECOVERY_ISOLATED_CLUSTER_ID:-}"
  [ -n "$att" ] || rec_die "REFUSED: GUVFX_RECOVERY_ISOLATED_CLUSTER_ID is not set -- a non-production mutation must attest the authorized isolated cluster's system_identifier (live cluster is '$sysid'). Fail-closed."
  [ "$sysid" = "$att" ] || rec_die "REFUSED: live system_identifier '$sysid' != attested GUVFX_RECOVERY_ISOLATED_CLUSTER_ID '$att' -- not the authorized isolated target; a class label cannot override authoritative identity. Fail-closed."
  rec_log "isolation OK: '$op' on attested+marked isolated cluster sysid=$sysid db=${db:-?}"
}

# Guard a MUTATING operation. $1 = operation name, $2 = "restore" to apply the
# categorical production-restore refusal. The caller must export REC_PROD_ACK=1
# (set from its --i-understand-production flag) for a production-allowed op.
rec_guard_mutation() {
  local op="$1" kind="${2:-generic}" cls
  cls="$(rec_target_class)"
  rec_assert_dsn_vs_class "$cls"   # host/db denylist backstop (additional layer)
  if [ "$cls" != "production" ]; then
    rec_require_isolated_target "$op"   # AUTHORITATIVE: live system_identifier must match the attested isolated cluster
    rec_log "mutation '$op' permitted against attested isolated target ($(rec_dsn_summary))"
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
  # "Empty" == indistinguishable from a freshly-created database (so a restore's result is
  # attributable to exactly one backup). Count ANY operator-added object: relations incl. views 'v'
  # and sequences 'S' (plus r/p/m/f) in non-system schemas; non-public non-system schemas; user
  # functions/procedures; and non-plpgsql extensions. A fresh createdb yields 0.
  local n
  n="$(rec_psql_scalar "select
      (select count(*) from pg_class c join pg_namespace s on s.oid=c.relnamespace
         where s.nspname not in ('pg_catalog','information_schema','pg_toast') and s.nspname !~ '^pg_'
           and c.relkind in ('r','p','m','f','v','S'))
    + (select count(*) from pg_namespace where nspname not in ('pg_catalog','information_schema','pg_toast','public') and nspname !~ '^pg_')
    + (select count(*) from pg_proc p join pg_namespace s on s.oid=p.pronamespace where s.nspname not in ('pg_catalog','information_schema') and s.nspname !~ '^pg_')
    + (select count(*) from pg_extension where extname <> 'plpgsql')")" || return 2
  [ "${n:-1}" = "0" ]
}

# Portable ISO8601-UTC -> epoch seconds (GNU date, then BSD/macOS date). Returns 1 on failure.
rec_epoch() {
  local ts="$1" e
  e="$(date -u -d "$ts" +%s 2>/dev/null)" && { printf '%s' "$e"; return 0; }
  e="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null)" && { printf '%s' "$e"; return 0; }
  return 1
}
