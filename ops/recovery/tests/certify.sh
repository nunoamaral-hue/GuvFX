#!/usr/bin/env bash
# GuvFX recovery tooling — ISOLATED restore certification (Gate F) + failure containment (Gate H).
#
# Runs ENTIRELY against isolated throwaway scratch databases on a LOCAL Postgres. It never
# touches any production or application database, never reads production data, and drops its
# scratch DBs on exit. Synthetic data only. Certifies the backup/verify/restore tooling end to
# end: backup -> integrity verify -> restore into a FRESH db -> row-count + referential-integrity
# + immutable-audit reconciliation, encrypted round-trip, and the fail-closed refusals.
#
# Requires: a local Postgres the current user can createdb/dropdb on, and gpg (for the encrypted
# case; skipped with a notice if absent). Run from the repo:  ops/recovery/tests/certify.sh
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REC="$(cd "$HERE/.." && pwd)"

SUF="$$_$(date +%s)"
SRC="guvfx_recov_src_$SUF"
TGT="guvfx_recov_tgt_$SUF"
TGT2=""   # set in the encrypted case; dropped by cleanup on any exit
TGT3=""   # set in the non-empty-detection case; dropped by cleanup on any exit
WORK="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_certify.XXXXXX")"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
note() { printf '[certify] %s\n' "$*"; }

# Host-locality guard: this harness creates/drops throwaway databases and must run ONLY against a
# LOCAL Postgres. Refuse a non-local PGHOST, and clear any inherited PGDATABASE so we can never
# createdb/dropdb against, or dump, an application/production database by accident.
case "${PGHOST:-}" in
  ""|localhost|127.0.0.1|::1|/tmp|/*) : ;;
  *) echo "[certify] REFUSED: PGHOST='$PGHOST' is not local -- run this harness only against a local Postgres." >&2; exit 2 ;;
esac
unset PGDATABASE

cleanup() {
  dropdb --if-exists "$SRC" 2>/dev/null || true
  dropdb --if-exists "$TGT" 2>/dev/null || true
  [ -n "$TGT2" ] && dropdb --if-exists "$TGT2" 2>/dev/null; :
  [ -n "$TGT3" ] && dropdb --if-exists "$TGT3" 2>/dev/null; :
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Expect a mutating-but-NON-production run to SUCCEED.
expect_ok()   { if "$@" >/dev/null 2>"$WORK/err"; then ok "$DESC"; else bad "$DESC (unexpected failure: $(tail -1 "$WORK/err"))"; fi; }
# Expect a command to FAIL (fail-closed refusal / rejection).
expect_fail() { if "$@" >/dev/null 2>"$WORK/err"; then bad "$DESC (SHOULD have been refused but succeeded)"; else ok "$DESC"; fi; }

note "scratch src=$SRC tgt=$TGT work=$WORK"
createdb "$SRC"

# --- representative synthetic schema + data (FKs, immutable audit) ---------
psql -v ON_ERROR_STOP=1 -q -d "$SRC" >/dev/null <<'SQL'
CREATE TABLE customer (id serial PRIMARY KEY, email text UNIQUE NOT NULL, created_utc timestamptz DEFAULT now());
CREATE TABLE trading_account (id serial PRIMARY KEY, customer_id int NOT NULL REFERENCES customer(id),
    login text NOT NULL, broker text NOT NULL, is_demo boolean NOT NULL DEFAULT true);
CREATE TABLE trade (id bigserial PRIMARY KEY, account_id int NOT NULL REFERENCES trading_account(id),
    symbol text NOT NULL, volume numeric(12,2) NOT NULL, profit numeric(14,2) NOT NULL, opened_utc timestamptz DEFAULT now());
CREATE TABLE execution_job (id bigserial PRIMARY KEY, account_id int NOT NULL REFERENCES trading_account(id),
    kind text NOT NULL, status text NOT NULL);
CREATE TABLE acquired_message (id bigserial PRIMARY KEY, provider text NOT NULL, chat_id text NOT NULL,
    message_id text NOT NULL, non_genuine boolean NOT NULL DEFAULT false, outcome text NOT NULL,
    UNIQUE(provider, chat_id, message_id, non_genuine));
CREATE TABLE provider_checkpoint (provider text PRIMARY KEY, checkpoint_message_id text NOT NULL, watermark text NOT NULL);
CREATE TABLE broker_event (id bigserial PRIMARY KEY, account_id int REFERENCES trading_account(id),
    kind text NOT NULL, amount numeric(14,2), observed_utc timestamptz DEFAULT now());
CREATE TABLE withdrawal_history (id bigserial PRIMARY KEY, account_id int NOT NULL REFERENCES trading_account(id),
    amount numeric(14,2) NOT NULL, classified text NOT NULL);
CREATE TABLE audit_event (id bigserial PRIMARY KEY, event text NOT NULL, payload jsonb NOT NULL, ts timestamptz DEFAULT now());

INSERT INTO customer(email) SELECT 'user'||g||'@example.test' FROM generate_series(1,25) g;
INSERT INTO trading_account(customer_id, login, broker, is_demo)
    SELECT ((g-1)%25)+1, 'L'||(100000+g), (ARRAY['IS6','TradersWay','Pepperstone'])[1+(g%3)], (g%4<>0) FROM generate_series(1,40) g;
INSERT INTO trade(account_id, symbol, volume, profit)
    SELECT ((g-1)%40)+1, (ARRAY['XAUUSD','EURUSD','GBPUSD'])[1+(g%3)], 0.10+(g%5)*0.1, (g%7)*1.25-3 FROM generate_series(1,500) g;
INSERT INTO execution_job(account_id, kind, status)
    SELECT ((g-1)%40)+1, 'PLACE_ORDER', (ARRAY['DONE','FAILED','RUNNING'])[1+(g%3)] FROM generate_series(1,120) g;
INSERT INTO acquired_message(provider, chat_id, message_id, non_genuine, outcome)
    SELECT 'wayond', '-100'||((g%3)), g::text, (g%11=0), CASE WHEN g%11=0 THEN 'DROPPED_NOT_ARMED' ELSE 'INTAKEN' END FROM generate_series(1,300) g;
INSERT INTO provider_checkpoint VALUES ('wayond','889','889'), ('ti_signals','151','151');
INSERT INTO broker_event(account_id, kind, amount) SELECT ((g-1)%40)+1, (ARRAY['DEPOSIT','INTERNAL_TRANSFER','UNKNOWN'])[1+(g%3)], (g%9)*10 FROM generate_series(1,60) g;
INSERT INTO withdrawal_history(account_id, amount, classified) SELECT ((g-1)%40)+1, (g%5)*25+10, 'EXTERNAL_WITHDRAWAL' FROM generate_series(1,12) g;
INSERT INTO audit_event(event, payload) SELECT 'lifecycle', jsonb_build_object('n', g, 'immutable', true) FROM generate_series(1,2000) g;
SQL
note "seeded synthetic data into $SRC"

export GUVFX_RECOVERY_TARGET_CLASS=test
export PGDATABASE="$SRC"

echo; note "=== Gate F: backup -> verify -> restore -> reconcile (UNENCRYPTED) ==="
DESC="backup dry-run refuses to act without --execute (produces no artifact)"
if "$REC/backup.sh" --out "$WORK" >/dev/null 2>&1 && [ -z "$(ls "$WORK"/*.dump 2>/dev/null)" ]; then ok "$DESC"; else bad "$DESC"; fi

DESC="backup --execute creates an archive + metadata"; expect_ok "$REC/backup.sh" --out "$WORK" --label certify --no-encrypt --execute
META="$(ls "$WORK"/guvfx_certify_*.meta.json 2>/dev/null | head -1)"
[ -n "$META" ] && ok "metadata sidecar present" || bad "metadata sidecar present"

DESC="verify passes on a good backup"; expect_ok "$REC/verify.sh" --meta "$META"

createdb "$TGT"
DESC="restore dry-run does not populate the target"
PGDATABASE="$TGT" "$REC/restore.sh" --meta "$META" >/dev/null 2>&1
n_tgt="$(PGDATABASE="$TGT" psql -tAqc "select count(*) from pg_class c join pg_namespace s on s.oid=c.relnamespace where s.nspname='public' and c.relkind='r'")"
[ "$n_tgt" = "0" ] && ok "$DESC" || bad "$DESC (target has $n_tgt tables after dry-run)"

DESC="restore --execute into the fresh target"; PGDATABASE="$TGT" expect_ok "$REC/restore.sh" --meta "$META" --execute

echo; note "--- reconciliation (row counts, referential integrity, immutable audit) ---"
recon_ok=1
for t in customer trading_account trade execution_job acquired_message provider_checkpoint broker_event withdrawal_history audit_event; do
  a="$(PGDATABASE="$SRC" psql -tAqc "select count(*) from $t")"
  b="$(PGDATABASE="$TGT" psql -tAqc "select count(*) from $t")"
  if [ "$a" = "$b" ]; then ok "row count $t = $a"; else bad "row count $t: src=$a tgt=$b"; recon_ok=0; fi
done
orphans="$(PGDATABASE="$TGT" psql -tAqc "select count(*) from trade t left join trading_account a on a.id=t.account_id where a.id is null")"
[ "$orphans" = "0" ] && ok "referential integrity: no orphan trades" || bad "referential integrity: $orphans orphan trades"
fkc="$(PGDATABASE="$TGT" psql -tAqc "select count(*) from pg_constraint where contype='f'")"
[ "${fkc:-0}" -ge 5 ] && ok "foreign-key constraints restored ($fkc)" || bad "foreign-key constraints restored ($fkc)"
asrc="$(PGDATABASE="$SRC" psql -tAqc "select md5(string_agg(event||payload::text, ',' order by id)) from audit_event")"
atgt="$(PGDATABASE="$TGT" psql -tAqc "select md5(string_agg(event||payload::text, ',' order by id)) from audit_event")"
[ "$asrc" = "$atgt" ] && ok "immutable audit_event content preserved (md5 match)" || bad "audit_event content differs (md5 src=$asrc tgt=$atgt)"

echo; note "=== Gate F: encrypted round-trip ==="
if command -v gpg >/dev/null 2>&1; then
  export GUVFX_BACKUP_PASSPHRASE="certify-only-throwaway-$SUF"
  DESC="encrypted backup --execute produces a .gpg artifact"; PGDATABASE="$SRC" expect_ok "$REC/backup.sh" --out "$WORK" --label enc --execute
  EMETA="$(ls "$WORK"/guvfx_enc_*.meta.json 2>/dev/null | head -1)"
  grep -q '"encrypted": true' "$EMETA" && ok "metadata marks encrypted=true" || bad "metadata marks encrypted=true"
  DESC="verify decrypts + reads TOC of the encrypted backup"; expect_ok "$REC/verify.sh" --meta "$EMETA"
  TGT2="guvfx_recov_tgt2_$SUF"; createdb "$TGT2"
  DESC="restore the ENCRYPTED backup into a fresh target"; PGDATABASE="$TGT2" expect_ok "$REC/restore.sh" --meta "$EMETA" --execute
  et="$(PGDATABASE="$TGT2" psql -tAqc "select count(*) from trade")"
  [ "$et" = "500" ] && ok "encrypted restore reconciles (trade=500)" || bad "encrypted restore reconciles (trade=$et)"
  DESC="verify FAILS on wrong passphrase"; GUVFX_BACKUP_PASSPHRASE="wrong-$SUF" expect_fail "$REC/verify.sh" --meta "$EMETA"
  export GUVFX_BACKUP_PASSPHRASE="certify-only-throwaway-$SUF"
  dropdb --if-exists "$TGT2" 2>/dev/null || true
else
  note "gpg not available -> SKIPPED encrypted round-trip (recorded as not-covered)"
fi

echo; note "=== Gate H: failure containment (fail-closed) ==="
DESC="undeclared target class is refused"; ( unset GUVFX_RECOVERY_TARGET_CLASS; PGDATABASE="$SRC" "$REC/backup.sh" --out "$WORK" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="backup of a PRODUCTION target refused without the double-gate"; ( GUVFX_RECOVERY_TARGET_CLASS=production PGDATABASE="$SRC" "$REC/backup.sh" --out "$WORK" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="RESTORE into a PRODUCTION target refused categorically (by the guard, not arg-parse)"
if rout="$( GUVFX_RECOVERY_TARGET_CLASS=production PGDATABASE="$TGT" "$REC/restore.sh" --meta "$META" --execute --i-understand-production 2>&1 )"; then
  bad "$DESC (SHOULD have been refused but succeeded)"
elif printf '%s' "$rout" | grep -q "never restores into production"; then
  ok "$DESC"
else
  bad "$DESC (refused for the WRONG reason: $(printf '%s' "$rout" | tail -1))"
fi
DESC="restore into a NON-EMPTY target refused without --force"; PGDATABASE="$TGT" expect_fail "$REC/restore.sh" --meta "$META" --execute
DESC="idempotent restore into non-empty target WITH --force succeeds"; PGDATABASE="$TGT" expect_ok "$REC/restore.sh" --meta "$META" --execute --force
rt="$(PGDATABASE="$TGT" psql -tAqc "select count(*) from trade")"; [ "$rt" = "500" ] && ok "re-restore reconciles (trade=500)" || bad "re-restore reconciles (trade=$rt)"
# corrupt-artifact rejection
CM="$WORK/guvfx_corrupt_${SUF}.meta.json"; CA="$WORK/guvfx_corrupt_${SUF}.dump"
head -c 4096 "$(ls "$WORK"/guvfx_certify_*.dump 2>/dev/null | head -1)" > "$CA" 2>/dev/null || printf 'not-a-dump' > "$CA"
good_dump="$(ls "$WORK"/guvfx_certify_*.dump 2>/dev/null | head -1)"
if [ -n "$good_dump" ]; then
  # metadata claims the FULL sha but the artifact is truncated -> sha mismatch must be caught
  full_sha="$(sed -n 's/.*"sha256": "\([^"]*\)".*/\1/p' "$META")"
  printf '{\n "artifact": "%s",\n "sha256": "%s",\n "encrypted": false\n}\n' "$(basename "$CA")" "$full_sha" > "$CM"
  DESC="verify REJECTS a truncated/corrupt artifact (sha mismatch)"; expect_fail "$REC/verify.sh" --meta "$CM"
else
  note "(unencrypted .dump was shredded post-encrypt; generating a fresh one for corruption test)"
  PGDATABASE="$SRC" "$REC/backup.sh" --out "$WORK" --label corrupt --no-encrypt --execute >/dev/null 2>&1
  CMETA="$(ls "$WORK"/guvfx_corrupt_*.meta.json | head -1)"; CART="$(sed -n 's/.*"artifact": "\([^"]*\)".*/\1/p' "$CMETA")"
  printf 'corrupted' >> "$WORK/$CART"
  DESC="verify REJECTS a corrupted artifact (sha mismatch)"; expect_fail "$REC/verify.sh" --meta "$CMETA"
fi
DESC="verify FAILS on missing metadata file"; expect_fail "$REC/verify.sh" --meta "$WORK/does_not_exist.meta.json"
# DSN-vs-class backstop: a prod DSN under a non-prod class must be refused even when mislabeled.
DESC="backup refused when PGDATABASE matches GUVFX_RECOVERY_PRODUCTION_DB but class!=production"
( GUVFX_RECOVERY_PRODUCTION_DB="$SRC" PGDATABASE="$SRC" "$REC/backup.sh" --out "$WORK" --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
# Broadened non-empty detection: a target whose only object is a MATVIEW is NOT "empty".
TGT3="guvfx_recov_tgt3_$SUF"; createdb "$TGT3"; psql -q -d "$TGT3" -c "create materialized view mv_probe as select 1 as x" >/dev/null 2>&1
DESC="restore refuses a target that holds a matview (non-empty) without --force"
PGDATABASE="$TGT3" expect_fail "$REC/restore.sh" --meta "$META" --execute
dropdb --if-exists "$TGT3" 2>/dev/null || true
# audit staleness gate fails CLOSED on an unparseable timestamp (portable-date hardening).
BADMETA="$WORK/guvfx_badts_${SUF}.meta.json"
printf '{\n "label": "badts",\n "created_utc": "not-a-timestamp",\n "target_class": "test",\n "encrypted": false,\n "bytes": 1,\n "artifact": "guvfx_certify_dummy.dump"\n}\n' > "$BADMETA"
: > "$WORK/guvfx_certify_dummy.dump"
DESC="audit --gate fails closed on an unparseable recovery-point timestamp"
mkdir -p "$WORK/badts"; cp "$BADMETA" "$WORK/badts/"; cp "$WORK/guvfx_certify_dummy.dump" "$WORK/badts/"
( "$REC/audit_report.sh" --dir "$WORK/badts" --max-age-hours 1 --gate ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"

echo; note "=== audit report ==="
"$REC/audit_report.sh" --dir "$WORK" || true

echo; note "================= CERTIFY SUMMARY: PASS=$PASS FAIL=$FAIL ================="
[ "$FAIL" -eq 0 ] || { note "CERTIFICATION FAILED"; exit 1; }
note "CERTIFICATION PASSED"
