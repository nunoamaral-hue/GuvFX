#!/usr/bin/env bash
# GuvFX recovery tooling — recovery-point audit report (DARK; read-only observability).
#
# Scans a directory of recovery points (*.meta.json sidecars) and reports provenance +
# coverage: label, age, class, encryption, size, artifact presence. Flags risks:
#   - no recovery point at all,
#   - newest recovery point older than --max-age-hours (staleness),
#   - a production-class recovery point that is UNENCRYPTED,
#   - a metadata sidecar whose artifact is missing (broken recovery point),
#   - an artifact (*.dump/*.dump.gpg) with no metadata sidecar (unverifiable orphan).
# With --gate it exits non-zero when any critical risk is present (monitoring hook).
# Never connects to a database; never writes. Pure local inspection.
#
# Usage: ops/recovery/audit_report.sh --dir /path/to/backups [--max-age-hours 24] [--gate]
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

DIR=""; MAX_AGE_H=0; GATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="${2:-}"; shift ;;
    --max-age-hours) MAX_AGE_H="${2:-0}"; shift ;;
    --gate) GATE=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) rec_die "unknown arg: $1" ;;
  esac
  shift
done
[ -n "$DIR" ] && [ -d "$DIR" ] || rec_die "--dir DIR is required and must exist"

issues=0; count=0; newest_epoch=0; unparseable=0
printf '%-28s %-22s %-11s %-9s %-12s %s\n' "LABEL" "CREATED_UTC" "CLASS" "ENCRYPTED" "BYTES" "ARTIFACT"
printf '%-28s %-22s %-11s %-9s %-12s %s\n' "-----" "-----------" "-----" "---------" "-----" "--------"

shopt -s nullglob
for m in "$DIR"/*.meta.json; do
  count=$((count+1))
  g() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$m" | head -1; }
  label="$(g label)"; created="$(g created_utc)"; cls="$(g target_class)"
  enc="$(g encrypted)"; bytes="$(g bytes)"; artifact="$(g artifact)"
  if [ -z "$artifact" ]; then present="MISSING(no-artifact-field)"; elif [ -r "$DIR/$artifact" ]; then present="ok"; else present="MISSING"; fi
  printf '%-28s %-22s %-11s %-9s %-12s %s\n' "$label" "$created" "$cls" "$enc" "${bytes:-?}" "${artifact:-<none>} ($present)"
  case "$present" in MISSING*) rec_err "broken recovery point: artifact missing/unresolved for $m"; issues=$((issues+1)) ;; esac
  if [ "$cls" = "production" ] && [ "$enc" != "true" ]; then rec_err "production recovery point UNENCRYPTED: $m"; issues=$((issues+1)); fi
  # Track newest by created_utc (portable parse). Count timestamps we COULD NOT parse so the
  # staleness gate can fail closed rather than silently skip (non-GNU date would otherwise no-op).
  if e="$(rec_epoch "$created")"; then
    [ "$e" -gt "$newest_epoch" ] && newest_epoch="$e"
  else
    unparseable=$((unparseable+1)); rec_err "unparseable created_utc '$created' in $m"
  fi
done

# orphan artifacts (no metadata)
for a in "$DIR"/*.dump "$DIR"/*.dump.gpg; do
  base="$(basename "$a")"
  if ! grep -ql "\"artifact\": \"$base\"" "$DIR"/*.meta.json 2>/dev/null; then
    rec_err "orphan artifact (no metadata sidecar): $a"; issues=$((issues+1))
  fi
done
shopt -u nullglob

echo
if [ "$count" -eq 0 ]; then
  rec_err "NO recovery points found in $DIR"; issues=$((issues+1))
else
  rec_log "$count recovery point(s) found."
  if [ "$MAX_AGE_H" -gt 0 ]; then
    if [ "$newest_epoch" -gt 0 ]; then
      now="$(date -u +%s)"; age_h=$(( (now - newest_epoch) / 3600 ))
      rec_log "newest recovery point age: ${age_h}h (threshold ${MAX_AGE_H}h)"
      [ "$age_h" -gt "$MAX_AGE_H" ] && { rec_err "STALE: newest recovery point older than ${MAX_AGE_H}h"; issues=$((issues+1)); }
    else
      # Could not parse ANY created_utc -> do NOT silently skip the staleness gate (fail closed).
      rec_err "STALENESS UNVERIFIABLE: no recovery-point timestamp could be parsed (check 'date' availability)"; issues=$((issues+1))
    fi
  fi
  [ "$unparseable" -gt 0 ] && issues=$((issues+1))
fi

rec_log "audit issues: $issues"
if [ "$GATE" = "1" ] && [ "$issues" -gt 0 ]; then
  rec_die "recovery audit GATE: $issues issue(s) -- fail-closed."
fi
