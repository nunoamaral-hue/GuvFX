#!/usr/bin/env bash
# GuvFX recovery tooling — ISOLATED restore certification (Gate F) + failure containment (Gate H)
# + R1 adversarial-fix regressions. Runs ENTIRELY against isolated throwaway scratch databases on
# an ATTESTED-ISOLATED local PostgreSQL. Synthetic data only; never touches a production/app DB.
#
# ISOLATION (HIGH-4): the operator must positively attest a throwaway cluster via
# GUVFX_RECOVERY_ATTEST_ISOLATED=1. The harness then reads the LIVE cluster system_identifier,
# refuses if it matches GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID, and pins it as the authorized
# isolated cluster for the tooling (GUVFX_RECOVERY_ISOLATED_CLUSTER_ID). "localhost" is NOT taken
# as proof of isolation.
#
# Requires: a local Postgres the current user can createdb/dropdb on, gpg, and the attestation env.
#   GUVFX_RECOVERY_ATTEST_ISOLATED=1 bash ops/recovery/tests/certify.sh
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REC="$(cd "$HERE/.." && pwd)"
# shellcheck source=../lib.sh
. "$REC/lib.sh"

SUF="$$_$(date +%s)"
SRC="guvfx_recov_src_$SUF"; TGT="guvfx_recov_tgt_$SUF"; TGT2=""; TGT3=""; TGT4=""; TGT5=""
MARKER="guvfx_recov_marker_$SUF"   # UNIQUE isolation-marker DB for this run (not a real operator marker)
WORK="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_certify.XXXXXX")"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
note() { printf '[certify] %s\n' "$*"; }
cleanup() {
  for d in "$SRC" "$TGT" "$TGT2" "$TGT3" "$TGT4" "$TGT5" "$MARKER"; do [ -n "$d" ] && dropdb --if-exists "$d" 2>/dev/null; done
  rm -rf "$WORK" 2>/dev/null || true; return 0
}
trap cleanup EXIT INT TERM
# Expect a non-production mutation to SUCCEED / to be REFUSED (fail-closed).
expect_ok()   { if "$@" >/dev/null 2>"$WORK/err"; then ok "$DESC"; else bad "$DESC (unexpected failure: $(tail -1 "$WORK/err"))"; fi; }
expect_fail() { if "$@" >/dev/null 2>"$WORK/err"; then bad "$DESC (SHOULD have been refused but succeeded)"; else ok "$DESC"; fi; }

# --- HIGH-4 attestation ----------------------------------------------------
[ "${GUVFX_RECOVERY_ATTEST_ISOLATED:-0}" = "1" ] || { echo "[certify] REFUSED: set GUVFX_RECOVERY_ATTEST_ISOLATED=1 to attest a throwaway test cluster (localhost is NOT isolation)." >&2; exit 2; }
case "${PGHOST:-}" in ""|localhost|127.0.0.1|::1|/tmp|/*) : ;; *) echo "[certify] REFUSED: PGHOST='$PGHOST' is not local." >&2; exit 2 ;; esac
[ -z "${PGHOSTADDR:-}" ] || case "${PGHOSTADDR}" in 127.0.0.1|::1) : ;; *) echo "[certify] REFUSED: PGHOSTADDR='$PGHOSTADDR' is not loopback." >&2; exit 2 ;; esac
unset PGDATABASE
SYSID="$(psql -tAqc 'select system_identifier::text from pg_control_system()' postgres 2>/dev/null || psql -tAqc 'select system_identifier::text from pg_control_system()' 2>/dev/null)"
[ -n "$SYSID" ] || { echo "[certify] REFUSED: cannot read local cluster system_identifier." >&2; exit 2; }
if [ -n "${GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID:-}" ] && [ "$SYSID" = "$GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID" ]; then
  echo "[certify] REFUSED: live cluster matches GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID -- will not certify against production." >&2; exit 2
fi
export GUVFX_RECOVERY_TARGET_CLASS=test
export GUVFX_RECOVERY_ISOLATED_CLUSTER_ID="$SYSID"
export GUVFX_RECOVERY_ISOLATION_MARKER_DB="$MARKER"
createdb "$MARKER"   # plant the positive isolation marker on this throwaway cluster (prod never has it)
note "attested isolated cluster sysid=$SYSID  marker=$MARKER  scratch src=$SRC  work=$WORK"
createdb "$SRC"

psql -v ON_ERROR_STOP=1 -q -d "$SRC" >/dev/null <<'SQL'
CREATE TABLE customer (id serial PRIMARY KEY, email text UNIQUE NOT NULL, created_utc timestamptz DEFAULT now());
CREATE TABLE trading_account (id serial PRIMARY KEY, customer_id int NOT NULL REFERENCES customer(id),
    login text NOT NULL, broker text NOT NULL, is_demo boolean NOT NULL DEFAULT true);
CREATE TABLE trade (id bigserial PRIMARY KEY, account_id int NOT NULL REFERENCES trading_account(id),
    symbol text NOT NULL, volume numeric(12,2) NOT NULL, profit numeric(14,2) NOT NULL, opened_utc timestamptz DEFAULT now());
CREATE TABLE execution_job (id bigserial PRIMARY KEY, account_id int NOT NULL REFERENCES trading_account(id), kind text NOT NULL, status text NOT NULL);
CREATE TABLE acquired_message (id bigserial PRIMARY KEY, provider text NOT NULL, chat_id text NOT NULL,
    message_id text NOT NULL, non_genuine boolean NOT NULL DEFAULT false, outcome text NOT NULL,
    UNIQUE(provider, chat_id, message_id, non_genuine));
CREATE TABLE provider_checkpoint (provider text PRIMARY KEY, checkpoint_message_id text NOT NULL, watermark text NOT NULL);
CREATE TABLE broker_event (id bigserial PRIMARY KEY, account_id int REFERENCES trading_account(id), kind text NOT NULL, amount numeric(14,2), observed_utc timestamptz DEFAULT now());
CREATE TABLE withdrawal_history (id bigserial PRIMARY KEY, account_id int NOT NULL REFERENCES trading_account(id), amount numeric(14,2) NOT NULL, classified text NOT NULL);
CREATE TABLE audit_event (id bigserial PRIMARY KEY, event text NOT NULL, payload jsonb NOT NULL, ts timestamptz DEFAULT now());
INSERT INTO customer(email) SELECT 'user'||g||'@example.test' FROM generate_series(1,25) g;
INSERT INTO trading_account(customer_id, login, broker, is_demo) SELECT ((g-1)%25)+1, 'L'||(100000+g), (ARRAY['IS6','TradersWay','Pepperstone'])[1+(g%3)], (g%4<>0) FROM generate_series(1,40) g;
INSERT INTO trade(account_id, symbol, volume, profit) SELECT ((g-1)%40)+1, (ARRAY['XAUUSD','EURUSD','GBPUSD'])[1+(g%3)], 0.10+(g%5)*0.1, (g%7)*1.25-3 FROM generate_series(1,500) g;
INSERT INTO execution_job(account_id, kind, status) SELECT ((g-1)%40)+1, 'PLACE_ORDER', (ARRAY['DONE','FAILED','RUNNING'])[1+(g%3)] FROM generate_series(1,120) g;
INSERT INTO acquired_message(provider, chat_id, message_id, non_genuine, outcome) SELECT 'wayond', '-100'||((g%3)), g::text, (g%11=0), CASE WHEN g%11=0 THEN 'DROPPED_NOT_ARMED' ELSE 'INTAKEN' END FROM generate_series(1,300) g;
INSERT INTO provider_checkpoint VALUES ('wayond','889','889'), ('ti_signals','151','151');
INSERT INTO broker_event(account_id, kind, amount) SELECT ((g-1)%40)+1, (ARRAY['DEPOSIT','INTERNAL_TRANSFER','UNKNOWN'])[1+(g%3)], (g%9)*10 FROM generate_series(1,60) g;
INSERT INTO withdrawal_history(account_id, amount, classified) SELECT ((g-1)%40)+1, (g%5)*25+10, 'EXTERNAL_WITHDRAWAL' FROM generate_series(1,12) g;
INSERT INTO audit_event(event, payload) SELECT 'lifecycle', jsonb_build_object('n', g, 'immutable', true) FROM generate_series(1,2000) g;
SQL
note "seeded synthetic data into $SRC"
export PGDATABASE="$SRC"

reconcile() { # $1=target db
  local t a b; local tdb="$1"; local okc=1
  for t in customer trading_account trade execution_job acquired_message provider_checkpoint broker_event withdrawal_history audit_event; do
    a="$(PGDATABASE="$SRC" psql -tAqc "select count(*) from $t")"; b="$(PGDATABASE="$tdb" psql -tAqc "select count(*) from $t")"
    [ "$a" = "$b" ] || { bad "row count $t: src=$a tgt=$b"; okc=0; }
  done
  [ "$okc" = 1 ] && ok "row counts reconcile (9 tables) into $tdb"
  [ "$(PGDATABASE="$tdb" psql -tAqc "select count(*) from trade t left join trading_account a on a.id=t.account_id where a.id is null")" = "0" ] && ok "referential integrity: no orphan trades ($tdb)" || bad "orphan trades ($tdb)"
  [ "$(PGDATABASE="$SRC" psql -tAqc "select md5(string_agg(event||payload::text, ',' order by id)) from audit_event")" = "$(PGDATABASE="$tdb" psql -tAqc "select md5(string_agg(event||payload::text, ',' order by id)) from audit_event")" ] && ok "immutable audit md5 match ($tdb)" || bad "audit md5 differs ($tdb)"
}

echo; note "=== Gate F: unencrypted backup -> verify -> restore -> reconcile ==="
DESC="backup dry-run refuses to act without --execute"
if "$REC/backup.sh" --out "$WORK" >/dev/null 2>&1 && ! ls "$WORK"/guvfx_*.dump >/dev/null 2>&1; then ok "$DESC"; else bad "$DESC"; fi
DESC="backup --execute creates artifact + metadata"; expect_ok "$REC/backup.sh" --out "$WORK" --label certify --no-encrypt --execute
META="$(ls "$WORK"/guvfx_certify_*.meta.json 2>/dev/null | head -1)"; [ -n "$META" ] && ok "metadata sidecar present" || bad "metadata sidecar present"
DESC="verify passes on a good backup"; expect_ok "$REC/verify.sh" --meta "$META"
createdb "$TGT"
DESC="restore dry-run does not populate the target"; PGDATABASE="$TGT" "$REC/restore.sh" --meta "$META" >/dev/null 2>&1 || true
[ "$(PGDATABASE="$TGT" psql -tAqc "select count(*) from pg_class c join pg_namespace s on s.oid=c.relnamespace where s.nspname='public' and c.relkind='r'")" = "0" ] && ok "$DESC" || bad "$DESC"
DESC="restore --execute into the fresh empty target"; PGDATABASE="$TGT" expect_ok "$REC/restore.sh" --meta "$META" --execute
reconcile "$TGT"

echo; note "=== Gate F: ENCRYPTED streaming round-trip (no plaintext on disk) ==="
rec_require_cmd gpg
export GUVFX_BACKUP_PASSPHRASE="certify-only-throwaway-$SUF"
DESC="encrypted backup --execute produces a .gpg artifact"; expect_ok "$REC/backup.sh" --out "$WORK" --label enc --execute
EMETA="$(ls "$WORK"/guvfx_enc_*.meta.json 2>/dev/null | head -1)"
grep -q '"encrypted": true' "$EMETA" 2>/dev/null && ok "metadata marks encrypted=true" || bad "metadata marks encrypted=true"
DESC="HIGH-3: NO plaintext .dump exists for the encrypted backup (only .dump.gpg)"
if ls "$WORK"/guvfx_enc_*.dump.gpg >/dev/null 2>&1 && ! ls "$WORK"/guvfx_enc_*.dump >/dev/null 2>&1 && ! ls "$WORK"/.guvfx_enc_*.partial.* >/dev/null 2>&1; then ok "$DESC"; else bad "$DESC (a plaintext/partial artifact was found)"; fi
DESC="verify decrypts + reads TOC of the encrypted backup"; expect_ok "$REC/verify.sh" --meta "$EMETA"
TGT2="guvfx_recov_tgt2_$SUF"; createdb "$TGT2"
DESC="restore the ENCRYPTED backup into a fresh empty target"; PGDATABASE="$TGT2" expect_ok "$REC/restore.sh" --meta "$EMETA" --execute
reconcile "$TGT2"
DESC="verify FAILS on wrong passphrase"; GUVFX_BACKUP_PASSPHRASE="wrong-$SUF" expect_fail "$REC/verify.sh" --meta "$EMETA"

echo; note "=== HIGH-1: authoritative target isolation (system_identifier) ==="
DESC="mutation refused when attested ISOLATED_CLUSTER_ID != live cluster (mislabel cannot override)"
( GUVFX_RECOVERY_ISOLATED_CLUSTER_ID="0000000000000000000" "$REC/backup.sh" --out "$WORK" --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="mutation refused when no isolated-cluster attestation is set"
( unset GUVFX_RECOVERY_ISOLATED_CLUSTER_ID; "$REC/backup.sh" --out "$WORK" --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="mutation refused when live cluster matches GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID (deny)"
( GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$SYSID" "$REC/backup.sh" --out "$WORK" --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="restore refused when PGDATABASE matches GUVFX_RECOVERY_PRODUCTION_DB (deny)"
( GUVFX_RECOVERY_PRODUCTION_DB="$TGT" PGDATABASE="$TGT" "$REC/restore.sh" --meta "$META" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="mutation refused when the isolation MARKER db is ABSENT (a bare clone-of-prod cannot masquerade; system_identifier alone is insufficient)"
dropdb --if-exists "$MARKER" 2>/dev/null
( "$REC/backup.sh" --out "$WORK" --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
createdb "$MARKER"   # restore the positive marker for the remaining tests

echo; note "=== Gate H: production + undeclared refusals ==="
DESC="undeclared target class is refused"; ( unset GUVFX_RECOVERY_TARGET_CLASS; "$REC/backup.sh" --out "$WORK" --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="PRODUCTION backup refused without the double-gate"; ( GUVFX_RECOVERY_TARGET_CLASS=production "$REC/backup.sh" --out "$WORK" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="RESTORE into a PRODUCTION target refused categorically (by the guard)"
if rout="$( GUVFX_RECOVERY_TARGET_CLASS=production PGDATABASE="$TGT" "$REC/restore.sh" --meta "$META" --execute --i-understand-production 2>&1 )"; then bad "$DESC (succeeded)"; elif printf '%s' "$rout" | grep -q "never restores into production"; then ok "$DESC"; else bad "$DESC (wrong reason)"; fi

echo; note "=== HIGH-2: clean-target restore contract (no --force) ==="
DESC="restore into a NON-EMPTY target refused (ordinary table), no override"; PGDATABASE="$TGT" expect_fail "$REC/restore.sh" --meta "$META" --execute
DESC="--force is rejected as an unknown arg (removed)"; PGDATABASE="$TGT" expect_fail "$REC/restore.sh" --meta "$META" --execute --force
TGT3="guvfx_recov_tgt3_$SUF"; createdb "$TGT3"; psql -q -d "$TGT3" -c "create materialized view mv_probe as select 1 as x" >/dev/null 2>&1
DESC="restore refuses a matview-only (non-empty) target"; PGDATABASE="$TGT3" expect_fail "$REC/restore.sh" --meta "$META" --execute
TGT4="guvfx_recov_tgt4_$SUF"; createdb "$TGT4"; psql -q -d "$TGT4" -c "create schema extra; create table extra.t(x int)" >/dev/null 2>&1
DESC="restore refuses a target with data only in a NON-public schema"; PGDATABASE="$TGT4" expect_fail "$REC/restore.sh" --meta "$META" --execute
TGT5="guvfx_recov_tgt5_$SUF"; createdb "$TGT5"; psql -q -d "$TGT5" -c "create view v as select 1 as x; create sequence s; create function f() returns int language sql as 'select 1'" >/dev/null 2>&1
DESC="restore refuses a target holding only a view/sequence/function (non-empty; attribution guarantee)"; PGDATABASE="$TGT5" expect_fail "$REC/restore.sh" --meta "$META" --execute
DESC="idempotent re-restore via explicit drop+recreate (clean lifecycle)"
dropdb "$TGT"; createdb "$TGT"; PGDATABASE="$TGT" expect_ok "$REC/restore.sh" --meta "$META" --execute
[ "$(PGDATABASE="$TGT" psql -tAqc "select count(*) from trade")" = "500" ] && ok "re-restore reconciles (trade=500)" || bad "re-restore reconciles"

echo; note "=== HIGH-3: backup safety (label, fail-closed finalize) + MEDIUM-2 capacity ==="
DESC="invalid --label is refused"; expect_fail "$REC/backup.sh" --out "$WORK" --label 'bad;rm -rf /' --no-encrypt --execute
DESC="non-integer reserved-floor override is refused (fail-closed, not fail-open)"
( GUVFX_BACKUP_MIN_FREE_BYTES=not-a-number "$REC/backup.sh" --out "$WORK" --label floorbad --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="MEDIUM-2: backup refused at the reserved free floor; leaves NO artifact/metadata (fail-closed finalize)"
countfloor() { find "$WORK" -maxdepth 1 -name 'guvfx_floor_*' 2>/dev/null | wc -l | tr -d ' '; }
before="$(countfloor)"
( GUVFX_BACKUP_MIN_FREE_BYTES=9223372036854775807 "$REC/backup.sh" --out "$WORK" --label floor --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || {
  after="$(countfloor)"; [ "$after" = "$before" ] && ok "$DESC" || bad "$DESC (left an artifact/metadata)"; }

echo; note "=== integrity + audit (MEDIUM-1) ==="
# Build the corrupt recovery point in its OWN subdir so the healthy $WORK audit stays healthy.
CD="$WORK/corruptdir"; mkdir -p "$CD"
cart="$(sed -n 's/.*"artifact": "\([^"]*\)".*/\1/p' "$META")"; goodsha="$(sed -n 's/.*"sha256": "\([^"]*\)".*/\1/p' "$META")"
cp "$WORK/$cart" "$CD/corrupt_copy.dump"; printf 'corrupt' >> "$CD/corrupt_copy.dump"
printf '{\n "artifact": "corrupt_copy.dump",\n "sha256": "%s",\n "encrypted": false\n}\n' "$goodsha" > "$CD/corrupt.meta.json"
DESC="verify FAILS on a corrupted artifact (sha mismatch vs recorded)"; expect_fail "$REC/verify.sh" --meta "$CD/corrupt.meta.json"
DESC="verify FAILS on missing metadata file"; expect_fail "$REC/verify.sh" --meta "$WORK/nope.meta.json"
DESC="audit --verify-integrity --gate FAILS on a present-but-corrupt artifact"
( "$REC/audit_report.sh" --dir "$CD" --verify-integrity --gate ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="audit --gate fails closed on an unparseable recovery-point timestamp"
mkdir -p "$WORK/badts"; printf '{\n "label":"badts",\n "created_utc":"not-a-timestamp",\n "target_class":"test",\n "encrypted":false,\n "bytes":1,\n "artifact":"d.dump"\n}\n' > "$WORK/badts/x.meta.json"; : > "$WORK/badts/d.dump"
( "$REC/audit_report.sh" --dir "$WORK/badts" --max-age-hours 1 --gate ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="audit --verify-integrity --gate PASSES on the healthy recovery-point dir"
( "$REC/audit_report.sh" --dir "$WORK" --verify-integrity --gate ) >/dev/null 2>&1 && ok "$DESC" || bad "$DESC (healthy dir should pass)"

echo; note "=== audit report (informational) ==="
"$REC/audit_report.sh" --dir "$WORK" --verify-integrity || true

echo; note "================= CERTIFY SUMMARY: PASS=$PASS FAIL=$FAIL ================="
[ "$FAIL" -eq 0 ] || { note "CERTIFICATION FAILED"; exit 1; }
note "CERTIFICATION PASSED"
