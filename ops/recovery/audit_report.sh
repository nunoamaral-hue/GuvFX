#!/usr/bin/env bash
# GuvFX recovery tooling — recovery-point audit report (DARK; read-only observability).
#
# Scans a directory of recovery points (*.meta.json sidecars) and reports FOUR distinct
# properties per point (MEDIUM-1), which must NOT be conflated:
#   RECOVERY_POINT_PRESENT          -- the artifact file referenced by metadata exists,
#   RECOVERY_POINT_FRESH            -- newest point within --max-age-hours,
#   RECOVERY_POINT_INTEGRITY_VERIFIED -- verify.sh passes (sha256 + decrypt + TOC)   [--verify-integrity]
#   RECOVERY_POINT_RESTORE_TESTED   -- NOT established by this tool (requires an actual isolated restore).
# Flags risks: no point; staleness; production point UNENCRYPTED; broken point (missing artifact);
# orphan artifact (no sidecar); and, with --verify-integrity, a present-but-CORRUPT artifact that
# inventory alone would pass. With --gate it exits non-zero on any such risk (monitoring hook).
# Never writes. With --verify-integrity it calls verify.sh, which decrypts to STDOUT (no plaintext temp).
#
# Usage: ops/recovery/audit_report.sh --dir DIR [--max-age-hours 24] [--verify-integrity] [--gate]
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

DIR=""; MAX_AGE_H=0; GATE=0; VERIFY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="${2:-}"; shift ;;
    --max-age-hours) MAX_AGE_H="${2:-0}"; shift ;;
    --verify-integrity) VERIFY=1 ;;
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
  integ="not-checked"
  if [ "$VERIFY" = "1" ] && [ "$present" = "ok" ]; then
    if "$HERE/verify.sh" --meta "$m" >/dev/null 2>&1; then integ="VERIFIED"; else integ="FAILED"; rec_err "INTEGRITY FAILED (present but not restorable-valid) for $m"; issues=$((issues+1)); fi
  fi
  printf '%-26s %-21s %-10s %-9s %-11s %s\n' "$label" "$created" "$cls" "$enc" "${bytes:-?}" "${artifact:-<none>} (present=$present; integrity=$integ)"
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

# orphan artifacts (no metadata). `</dev/null` guarantees grep never reads the terminal/pipe when the
# meta-glob expands to nothing (nullglob) -- otherwise grep with no file args would block on stdin and
# the orphan check (which exists for the no-sidecars case) would hang or misreport.
for a in "$DIR"/*.dump "$DIR"/*.dump.gpg; do
  base="$(basename "$a")"
  if ! grep -ql "\"artifact\": \"$base\"" "$DIR"/*.meta.json </dev/null 2>/dev/null; then
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
      if [ "$age_h" -lt 0 ]; then
        rec_err "FUTURE-DATED recovery point (age ${age_h}h) -- clock skew or a tampered/implausible timestamp; the staleness guarantee is unverifiable"; issues=$((issues+1))
      elif [ "$age_h" -gt "$MAX_AGE_H" ]; then
        rec_err "STALE: newest recovery point older than ${MAX_AGE_H}h"; issues=$((issues+1))
      fi
    else
      # Could not parse ANY created_utc -> do NOT silently skip the staleness gate (fail closed).
      rec_err "STALENESS UNVERIFIABLE: no recovery-point timestamp could be parsed (check 'date' availability)"; issues=$((issues+1))
    fi
  fi
  [ "$unparseable" -gt 0 ] && issues=$((issues+1))
fi

[ "$VERIFY" = "0" ] && rec_log "NOTE: integrity NOT checked (run with --verify-integrity to prove each present artifact is a valid, restorable archive -- inventory presence alone is not integrity)."
rec_log "RECOVERY_POINT_RESTORE_TESTED is NOT established by this tool -- it requires an actual isolated restore (ops/recovery/restore.sh into a fresh empty DB)."
rec_log "audit issues: $issues"
if [ "$GATE" = "1" ] && [ "$issues" -gt 0 ]; then
  rec_die "recovery audit GATE: $issues issue(s) -- fail-closed."
fi
