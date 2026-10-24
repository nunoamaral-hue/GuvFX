#!/usr/bin/env bash
# ADR-0054 / F-8 -- runtime-role separation CUTOVER wrapper (manifest-driven, credentials off argv).
# ============================================================================================================
# Three modes, each a SEPARATE Sponsor-authorised step:
#
#   --capture   READ-ONLY. Runs capture_app_manifest.sql and writes the expected cutover manifest to a file for
#               operator review + version control. No mutation. No credentials.
#
#   --execute   Builds a TEMP table `_cutover_manifest` from the REVIEWED manifest file (via \copy FROM STDIN,
#               so object names never go on argv), prepends it to separate_runtime_role.sql, and runs it. The
#               SCRAM verifiers are read from the ENVIRONMENT (GUVFX_APP_SCRAM, GUVFX_MIGRATE_SCRAM) by the SQL's
#               \getenv -- they are NEVER passed on the command line, written to the combined SQL, or logged.
#
#   --rollback  Same manifest preamble prepended to separate_runtime_role_rollback.sql (no new credentials).
#
# Secret hygiene (F-4): umask 077; the combined SQL (object names only, no secrets) lives in a private temp file
# removed on exit/error/interrupt; verifiers flow ONLY through the environment into the psql process (never argv,
# never the combined SQL) and are validated here in bash before psql is invoked; this script never echoes them.
# NOTE: `set +o history` below only affects THIS non-interactive shell. The real history sink is the operator's
# `export GUVFX_APP_SCRAM=...` step: do NOT type the verifier on the command line. Source it from a 0600 file:
#     umask 077; set -a; . ./cutover_secrets.env; set +a   # file holds GUVFX_APP_SCRAM=... / GUVFX_MIGRATE_SCRAM=...
# then delete that file. (mint_erasure_operator writes verifiers to such a file; it never prints them.)
#
# Connection: as the bootstrap superuser. Use --container <name> to run psql inside a docker container
# (rehearsal / containerised prod), else a local `psql` on PATH is used with the ambient PG* environment.
set -euo pipefail
set +o history 2>/dev/null || true
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=""; CONTAINER=""; MANIFEST=""; TARGET_DB=""; BOOTSTRAP=""; APP_ROLE=""; APP_SCHEMA="public"
CONNECT_USER=""; EMERGENCY=0; EXPECT_SHA=""
MANIFEST_RE='^(table|sequence|view|matview|foreign_table|function|procedure|aggregate)\|[^|]+\|[^|]*\|[^|]+\|(application|protected)$'

die() { echo "cutover.sh: $*" >&2; exit 2; }
usage() {
  cat >&2 <<'USAGE'
Usage:
  cutover.sh --capture  --app-schema public --out <manifest.psv> [--container <ctr>] [--connect-user <u>] --db <name>
  cutover.sh --execute  --manifest <manifest.psv> --db <name> --bootstrap-role <r> --app-role <r> --app-schema public
             [--container <ctr>] [--connect-user <u>] [--expect-manifest-sha256 <hex>]
             (requires env GUVFX_APP_SCRAM and GUVFX_MIGRATE_SCRAM -- SCRAM verifiers, never on argv)
  cutover.sh --rollback --manifest <manifest.psv> --db <name> --bootstrap-role <r> --app-role <r> --app-schema public
             [--container <ctr>] [--connect-user <u>] [--expect-manifest-sha256 <hex>] [--emergency-override]

  --expect-manifest-sha256 <hex>  refuse unless the manifest's SHA-256 (sorted bytes) equals this pinned value
                                  (the value --capture printed for the REVIEWED file); enforces F-3 integrity.
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --capture)  MODE="capture" ;;
    --execute)  MODE="execute" ;;
    --rollback) MODE="rollback" ;;
    --container) CONTAINER="$2"; shift ;;
    --connect-user) CONNECT_USER="$2"; shift ;;
    --manifest) MANIFEST="$2"; shift ;;
    --out) MANIFEST="$2"; shift ;;
    --db) TARGET_DB="$2"; shift ;;
    --bootstrap-role) BOOTSTRAP="$2"; shift ;;
    --app-role) APP_ROLE="$2"; shift ;;
    --app-schema) APP_SCHEMA="$2"; shift ;;
    --expect-manifest-sha256) EXPECT_SHA="$2"; shift ;;
    --emergency-override) EMERGENCY=1 ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[ -n "$MODE" ] || usage
[ -n "$TARGET_DB" ] || die "--db is required"
[ -n "$CONNECT_USER" ] || CONNECT_USER="$BOOTSTRAP"
[ -n "$CONNECT_USER" ] || CONNECT_USER="guvfx"

# psql_run: feed SQL on stdin to psql as the bootstrap superuser. Extra args (e.g. -v) follow.
# When --execute, the two SCRAM env vars are passed through WITHOUT their values appearing on argv.
psql_run() {
  if [ -n "$CONTAINER" ]; then
    if [ "$MODE" = "execute" ]; then
      docker exec -i -e GUVFX_APP_SCRAM -e GUVFX_MIGRATE_SCRAM "$CONTAINER" \
        psql -X -U "$CONNECT_USER" -d "$TARGET_DB" "$@"
    else
      docker exec -i "$CONTAINER" psql -X -U "$CONNECT_USER" -d "$TARGET_DB" "$@"
    fi
  else
    psql -X -U "$CONNECT_USER" -d "$TARGET_DB" "$@"
  fi
}

if [ "$MODE" = "capture" ]; then
  [ -n "$MANIFEST" ] || die "--out <manifest.psv> is required for --capture"
  [ -n "$APP_SCHEMA" ] || die "--app-schema is required"
  echo "cutover.sh: capturing read-only manifest for schema '$APP_SCHEMA' -> $MANIFEST" >&2
  # -q suppresses psql's own status chatter so only manifest rows reach the file.
  psql_run -q -v app_schema="$APP_SCHEMA" -f - < "$SCRIPT_DIR/capture_app_manifest.sql" > "$MANIFEST"
  # Positive assertion (RULE 11): every captured line must be a well-formed manifest row. Any stray psql output
  # is a hard failure, never silently dropped.
  BADLINE="$(grep -nvE "$MANIFEST_RE" "$MANIFEST" || true)"
  if [ -n "$BADLINE" ]; then
    echo "cutover.sh: REFUSED -- manifest contains non-manifest line(s):" >&2
    echo "$BADLINE" >&2
    rm -f "$MANIFEST"; exit 2
  fi
  LINES="$(wc -l < "$MANIFEST" | tr -d ' ')"
  DIGEST="$(LC_ALL=C sort "$MANIFEST" | shasum -a 256 | awk '{print $1}')"
  echo "cutover.sh: wrote $LINES manifest line(s). MANIFEST_SHA256=$DIGEST" >&2
  echo "cutover.sh: REVIEW and commit $MANIFEST before running --execute." >&2
  exit 0
fi

# execute / rollback: both need the reviewed manifest and the role/db params.
[ -n "$MANIFEST" ] || die "--manifest <manifest.psv> is required"
[ -f "$MANIFEST" ] || die "manifest file not found: $MANIFEST"
[ -n "$BOOTSTRAP" ] || die "--bootstrap-role is required"
[ -n "$APP_ROLE" ] || die "--app-role is required"
[ -n "$APP_SCHEMA" ] || die "--app-schema is required"

# F-3/F-4 injection guard: re-validate the reviewed manifest on the MUTATING path too (not just --capture). Every
# line must be a well-formed manifest row, and NO line may be a psql \copy terminator ("\." ) or carry the '|'
# count wrong -- otherwise a hand-edited/tampered manifest could break out of the \copy FROM STDIN stream into
# arbitrary SQL under the bootstrap superuser. Hard-fail (fail-closed), never silently truncate.
BADLINE="$(grep -nvE "$MANIFEST_RE" "$MANIFEST" || true)"
if [ -n "$BADLINE" ]; then
  echo "cutover.sh: REFUSED -- manifest contains non-conforming line(s); refusing to build the combined SQL:" >&2
  echo "$BADLINE" >&2
  exit 2
fi
if grep -qxF '\.' "$MANIFEST"; then
  die "REFUSED -- manifest contains a bare '\\.' line (would terminate the \\copy stream early)"
fi

# F-3 integrity: if a pinned digest was supplied, the reviewed manifest must match it exactly.
MAN_DIGEST="$(LC_ALL=C sort "$MANIFEST" | shasum -a 256 | awk '{print $1}')"
if [ -n "$EXPECT_SHA" ] && [ "$EXPECT_SHA" != "$MAN_DIGEST" ]; then
  die "REFUSED -- manifest SHA-256 $MAN_DIGEST does not match --expect-manifest-sha256 $EXPECT_SHA"
fi

if [ "$MODE" = "execute" ]; then
  : "${GUVFX_APP_SCRAM:?set GUVFX_APP_SCRAM (app-role SCRAM-SHA-256 verifier) in the environment}"
  : "${GUVFX_MIGRATE_SCRAM:?set GUVFX_MIGRATE_SCRAM (migrate-role SCRAM-SHA-256 verifier) in the environment}"
  # F-4: validate verifier FORMAT in bash (no server round-trip) before psql is ever invoked. Fail-closed.
  for v in GUVFX_APP_SCRAM GUVFX_MIGRATE_SCRAM; do
    val="${!v}"
    case "$val" in
      'SCRAM-SHA-256$'*) [ "${#val}" -gt 20 ] || die "REFUSED -- $v is too short to be a SCRAM verifier" ;;
      *) die "REFUSED -- $v must be a SCRAM-SHA-256 verifier (starts with 'SCRAM-SHA-256\$')" ;;
    esac
  done
  SCRIPT_SQL="$SCRIPT_DIR/separate_runtime_role.sql"
else
  SCRIPT_SQL="$SCRIPT_DIR/separate_runtime_role_rollback.sql"
fi

# Build the combined SQL (NO secrets: object names + the chosen script). Removed on any exit.
COMBINED="$(mktemp "${TMPDIR:-/tmp}/guvfx_cutover.XXXXXX.sql")"
cleanup() { rm -f "$COMBINED" 2>/dev/null || true; }
trap cleanup EXIT INT TERM HUP QUIT

{
  echo "\\set ON_ERROR_STOP on"
  echo "CREATE TEMP TABLE _cutover_manifest (obj_kind text, obj_schema text, obj_identity text, expected_owner text, classification text);"
  echo "\\copy _cutover_manifest (obj_kind,obj_schema,obj_identity,expected_owner,classification) FROM STDIN WITH (FORMAT csv, DELIMITER '|')"
  cat "$MANIFEST"
  echo "\\."
  cat "$SCRIPT_SQL"
} > "$COMBINED"

echo "cutover.sh: mode=$MODE db=$TARGET_DB app_role=$APP_ROLE schema=$APP_SCHEMA MANIFEST_SHA256=$MAN_DIGEST" >&2

PSQL_VARS=( -v target_db="$TARGET_DB" -v allow_destructive_provisioning=1
            -v bootstrap_role="$BOOTSTRAP" -v app_role="$APP_ROLE" -v app_schema="$APP_SCHEMA" )
if [ "$MODE" = "rollback" ] && [ "$EMERGENCY" = "1" ]; then
  PSQL_VARS+=( -v emergency_override=1 )
fi

psql_run "${PSQL_VARS[@]}" < "$COMBINED"
echo "cutover.sh: $MODE completed." >&2
