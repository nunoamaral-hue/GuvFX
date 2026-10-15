#!/usr/bin/env bash
# GuvFX recovery tooling — ISOLATED restore certification (Gate F) + failure containment (Gate H)
# + R1/R2 adversarial-fix regressions. Runs ENTIRELY against isolated throwaway scratch databases on
# an INDEPENDENTLY-ATTESTED local PostgreSQL, as a LEAST-PRIVILEGE non-superuser role. Synthetic data
# only; never touches a production/app DB.
#
# ISOLATION (HIGH-1, R2): isolation is established BEFORE any mutation and is NOT something this
# destructive harness can manufacture. The operator provisions it OUT-OF-BAND with
# tests/setup_isolated_cluster.sh (privileged), which creates a least-privilege role and plants the
# isolation marker. This harness then connects as that non-superuser role and calls
# attest_independent_test_cluster (tests/attest_isolated.sh), which:
#   - requires GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID (production positively identifiable),
#   - requires the operator-attested GUVFX_RECOVERY_ISOLATED_CLUSTER_ID to EQUAL the live cluster
#     (never derived from the connection),
#   - requires a PRE-EXISTING marker DB this harness did NOT create,
#   - requires a NON-SUPERUSER connecting role,
#   - never accepts localhost as proof.
# It NEVER runs createdb/dropdb/marker mutation until that attestation passes, and cleanup drops ONLY
# databases this run OWNS (never the operator's marker).
#
# Requires: a local throwaway Postgres, gpg, and the operator env from setup_isolated_cluster.sh:
#   export GUVFX_RECOVERY_ATTEST_ISOLATED=1 GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID=<prod sysid>
#   export GUVFX_RECOVERY_ISOLATED_CLUSTER_ID=<this cluster sysid> GUVFX_RECOVERY_ISOLATION_MARKER_DB=<marker>
#   export PGHOST=<loopback> PGUSER=<non-superuser role> PGPASSWORD=<throwaway> GUVFX_BACKUP_PASSPHRASE=<throwaway>
#   GUVFX_RECOVERY_ATTEST_ISOLATED=1 bash ops/recovery/tests/certify.sh
set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REC="$(cd "$HERE/.." && pwd)"
# shellcheck source=../lib.sh
. "$REC/lib.sh"
# shellcheck source=attest_isolated.sh
. "$HERE/attest_isolated.sh"

SUF="$$_$(date +%s)"
SRC="guvfx_recov_src_$SUF"; TGT="guvfx_recov_tgt_$SUF"
TGT2=""; TGT3=""; TGT4=""; TGT5=""; TGTH=""
MAINT="${GUVFX_RECOVERY_ATTEST_MAINT_DB:-postgres}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/guvfx_certify.XXXXXX")"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
note() { printf '[certify] %s\n' "$*"; }

# Cleanup drops ONLY databases owned by the (non-superuser) current role -- never the operator marker,
# never any unrelated database (R2 HIGH-1 I/J). A database this run did not create/own is left intact.
safe_dropdb() {
  local d="$1" owned
  [ -n "$d" ] || return 0
  owned="$(psql -tAqc "select count(*) from pg_database dd join pg_roles r on r.oid=dd.datdba where dd.datname='$d' and r.rolname=current_user" "$MAINT" 2>/dev/null || echo 0)"
  [ "${owned:-0}" = "1" ] && dropdb --if-exists "$d" 2>/dev/null
  return 0
}
cleanup() {
  for d in "$SRC" "$TGT" "$TGT2" "$TGT3" "$TGT4" "$TGT5" "$TGTH"; do safe_dropdb "$d"; done
  rm -rf "$WORK" 2>/dev/null || true; return 0
}
trap cleanup EXIT INT TERM
# Expect a non-production mutation to SUCCEED / to be REFUSED (fail-closed).
expect_ok()   { if "$@" >/dev/null 2>"$WORK/err"; then ok "$DESC"; else bad "$DESC (unexpected failure: $(tail -1 "$WORK/err"))"; fi; }
expect_fail() { if "$@" >/dev/null 2>"$WORK/err"; then bad "$DESC (SHOULD have been refused but succeeded)"; else ok "$DESC"; fi; }

# --- HIGH-1: INDEPENDENT isolation attestation (before ANY mutation) -------
if ! REASON="$(attest_independent_test_cluster 2>&1)"; then
  echo "[certify] REFUSED: independent isolation could not be established -- $REASON" >&2
  echo "[certify] Provision an isolated throwaway cluster first: GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID=<prod> GUVFX_RECOVERY_TESTER_PASSWORD=<pw> bash $HERE/setup_isolated_cluster.sh" >&2
  exit 2
fi
SYSID="${GUVFX_RECOVERY_ISOLATED_CLUSTER_ID}"
MARKER="${GUVFX_RECOVERY_ISOLATION_MARKER_DB:-guvfx_recovery_isolated_ok}"
export GUVFX_RECOVERY_TARGET_CLASS=test
note "independent isolation OK: attested+marked cluster sysid=$SYSID marker=$MARKER role=$(psql -tAqc 'select current_user' "$MAINT") (non-superuser)"
note "scratch src=$SRC work=$WORK"
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
createdb "$TGT"; export GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT"
DESC="restore dry-run does not populate the target"; PGDATABASE="$TGT" "$REC/restore.sh" --meta "$META" >/dev/null 2>&1 || true
[ "$(PGDATABASE="$TGT" psql -tAqc "select count(*) from pg_class c join pg_namespace s on s.oid=c.relnamespace where s.nspname='public' and c.relkind='r'")" = "0" ] && ok "$DESC" || bad "$DESC"
DESC="restore --execute into the fresh empty target"; PGDATABASE="$TGT" expect_ok "$REC/restore.sh" --meta "$META" --execute
reconcile "$TGT"

echo; note "=== Gate F: ENCRYPTED streaming round-trip (no plaintext on disk) ==="
rec_require_cmd gpg
export GUVFX_BACKUP_PASSPHRASE="${GUVFX_BACKUP_PASSPHRASE:-certify-only-throwaway-$SUF}"
DESC="encrypted backup --execute produces a .gpg artifact"; expect_ok "$REC/backup.sh" --out "$WORK" --label enc --execute
EMETA="$(ls "$WORK"/guvfx_enc_*.meta.json 2>/dev/null | head -1)"
grep -q '"encrypted": true' "$EMETA" 2>/dev/null && ok "metadata marks encrypted=true" || bad "metadata marks encrypted=true"
DESC="HIGH-3: NO plaintext .dump exists for the encrypted backup (only .dump.gpg)"
if ls "$WORK"/guvfx_enc_*.dump.gpg >/dev/null 2>&1 && ! ls "$WORK"/guvfx_enc_*.dump >/dev/null 2>&1 && ! ls "$WORK"/.guvfx_enc_*.partial.* >/dev/null 2>&1; then ok "$DESC"; else bad "$DESC (a plaintext/partial artifact was found)"; fi
DESC="verify decrypts + reads TOC of the encrypted backup"; expect_ok "$REC/verify.sh" --meta "$EMETA"
TGT2="guvfx_recov_tgt2_$SUF"; createdb "$TGT2"; export GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT2"
DESC="restore the ENCRYPTED backup into a fresh empty target"; PGDATABASE="$TGT2" expect_ok "$REC/restore.sh" --meta "$EMETA" --execute
reconcile "$TGT2"
DESC="verify FAILS on wrong passphrase"; GUVFX_BACKUP_PASSPHRASE="wrong-$SUF" expect_fail "$REC/verify.sh" --meta "$EMETA"

echo; note "=== HIGH-1: INDEPENDENT isolation attestation negatives (precise refusal reason) ==="
DESC="verified isolated cluster attestation succeeds"; if attest_independent_test_cluster >/dev/null 2>&1; then ok "$DESC"; else bad "$DESC"; fi
DESC="production-like LOCAL cluster refused (live sysid == PRODUCTION_CLUSTER_ID)"
out="$( ( GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID="$SYSID" attest_independent_test_cluster ) 2>&1 )" && bad "$DESC (passed)" || case "$out" in *"declared production cluster"*) ok "$DESC";; *) bad "$DESC (wrong reason: $out)";; esac
DESC="refused when production denylist ABSENT (cannot self-attest without identifying prod)"
out="$( ( unset GUVFX_RECOVERY_PRODUCTION_CLUSTER_ID; attest_independent_test_cluster ) 2>&1 )" && bad "$DESC (passed)" || case "$out" in *"positively identifiable"*) ok "$DESC";; *) bad "$DESC (wrong reason: $out)";; esac
DESC="self-attestation against the WRONG cluster refused (attested id != live)"
out="$( ( GUVFX_RECOVERY_ISOLATED_CLUSTER_ID="0000000000000000000" attest_independent_test_cluster ) 2>&1 )" && bad "$DESC (passed)" || case "$out" in *"attested GUVFX_RECOVERY_ISOLATED_CLUSTER_ID"*) ok "$DESC";; *) bad "$DESC (wrong reason: $out)";; esac
DESC="missing independent attestation refused (ISOLATED_CLUSTER_ID unset)"
out="$( ( unset GUVFX_RECOVERY_ISOLATED_CLUSTER_ID; attest_independent_test_cluster ) 2>&1 )" && bad "$DESC (passed)" || case "$out" in *"independently attest"*) ok "$DESC";; *) bad "$DESC (wrong reason: $out)";; esac
DESC="inherited non-loopback PGHOST refused before any DB contact"
out="$( ( PGHOST="db.prod.example.com" attest_independent_test_cluster ) 2>&1 )" && bad "$DESC (passed)" || case "$out" in *"not a local/loopback"*) ok "$DESC";; *) bad "$DESC (wrong reason: $out)";; esac
DESC="attestation flag missing refused"
out="$( ( unset GUVFX_RECOVERY_ATTEST_ISOLATED; attest_independent_test_cluster ) 2>&1 )" && bad "$DESC (passed)" || case "$out" in *"GUVFX_RECOVERY_ATTEST_ISOLATED"*) ok "$DESC";; *) bad "$DESC (wrong reason: $out)";; esac
DESC="absent isolation MARKER refused (harness must NOT create it)"
out="$( ( GUVFX_RECOVERY_ISOLATION_MARKER_DB="guvfx_absent_marker_$SUF" attest_independent_test_cluster ) 2>&1 )" && bad "$DESC (passed)" || case "$out" in *"ABSENT"*) ok "$DESC";; *) bad "$DESC (wrong reason: $out)";; esac
SU_ROLE="$(psql -tAqc "select rolname from pg_roles where rolsuper and rolcanlogin order by 1 limit 1" "$MAINT" 2>/dev/null || true)"
if [ -n "$SU_ROLE" ]; then
  DESC="SUPERUSER connecting role refused (least-privilege required)"
  out="$( ( PGUSER="$SU_ROLE" PGPASSWORD="" attest_independent_test_cluster ) 2>&1 )" && bad "$DESC (passed)" || case "$out" in *"SUPERUSER"*) ok "$DESC";; *) bad "$DESC (wrong reason: $out)";; esac
else
  note "SKIP: no login-capable superuser role available to exercise the superuser-refusal case"
fi

echo; note "=== HIGH-2: restore TARGET-database authorization (per-DB, not just per-cluster) ==="
DESC="restore refused when NO target allowlist is set (class label is not authorization)"
( unset GUVFX_RECOVERY_ALLOWED_TARGET_DB; createdb "guvfx_recov_noal_$SUF" 2>/dev/null; PGDATABASE="guvfx_recov_noal_$SUF" "$REC/restore.sh" --meta "$META" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
safe_dropdb "guvfx_recov_noal_$SUF"
DESC="restore refused into an administrative database (postgres), regardless of class/marker"
( GUVFX_RECOVERY_ALLOWED_TARGET_DB="$MAINT" PGDATABASE="$MAINT" "$REC/restore.sh" --meta "$META" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="restore refused when target not in the allowlist (unknown target)"
TGTX="guvfx_recov_tgtx_$SUF"; createdb "$TGTX"
( GUVFX_RECOVERY_ALLOWED_TARGET_DB="some_other_db" PGDATABASE="$TGTX" "$REC/restore.sh" --meta "$META" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
safe_dropdb "$TGTX"
DESC="restore refused into a database the role does NOT own (the operator marker)"
( GUVFX_RECOVERY_ALLOWED_TARGET_DB="$MARKER" PGDATABASE="$MARKER" "$REC/restore.sh" --meta "$META" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="restore refused when target matches GUVFX_RECOVERY_PRODUCTION_DB (even if allowlisted)"
( GUVFX_RECOVERY_PRODUCTION_DB="$TGT" GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT" PGDATABASE="$TGT" "$REC/restore.sh" --meta "$META" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
# R2 review MEDIUM: a GLOB allowlist entry must be treated LITERALLY (no CWD pathname expansion), even
# when a file matching the target name exists in the invocation directory -- else EXACT-name is defeated.
TGTG="guvfx_recov_tgtg_$SUF"; createdb "$TGTG"; globdir="$WORK/globcwd"; mkdir -p "$globdir"; : > "$globdir/$TGTG"
DESC="restore refused when the allowlist entry is a GLOB ('*') and a CWD file matches the target (no globbing)"
( cd "$globdir"; GUVFX_RECOVERY_ALLOWED_TARGET_DB='*' PGDATABASE="$TGTG" "$REC/restore.sh" --meta "$META" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
safe_dropdb "$TGTG"
DESC="cleanup CANNOT drop an unrelated / non-owned database (operator marker survives)"
safe_dropdb "$MARKER"; [ "$(psql -tAqc "select count(*) from pg_database where datname='$MARKER'" "$MAINT")" = "1" ] && ok "$DESC" || bad "$DESC (marker was dropped!)"

echo; note "=== Gate H: production + undeclared refusals ==="
export GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT"
DESC="undeclared target class is refused"; ( unset GUVFX_RECOVERY_TARGET_CLASS; "$REC/backup.sh" --out "$WORK" --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="PRODUCTION backup refused without the double-gate"; ( GUVFX_RECOVERY_TARGET_CLASS=production "$REC/backup.sh" --out "$WORK" --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="RESTORE into a PRODUCTION target refused categorically (by the guard)"
if rout="$( GUVFX_RECOVERY_TARGET_CLASS=production PGDATABASE="$TGT" "$REC/restore.sh" --meta "$META" --execute --i-understand-production 2>&1 )"; then bad "$DESC (succeeded)"; elif printf '%s' "$rout" | grep -q "never restores into production"; then ok "$DESC"; else bad "$DESC (wrong reason)"; fi

echo; note "=== HIGH-2: clean-target restore contract (no --force) ==="
DESC="restore into a NON-EMPTY target refused (ordinary table), no override"; PGDATABASE="$TGT" expect_fail "$REC/restore.sh" --meta "$META" --execute
DESC="--force is rejected as an unknown arg (removed)"; PGDATABASE="$TGT" expect_fail "$REC/restore.sh" --meta "$META" --execute --force
TGT3="guvfx_recov_tgt3_$SUF"; createdb "$TGT3"; psql -q -d "$TGT3" -c "create materialized view mv_probe as select 1 as x" >/dev/null 2>&1
DESC="restore refuses a matview-only (non-empty) target"; GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT3" PGDATABASE="$TGT3" expect_fail "$REC/restore.sh" --meta "$META" --execute
TGT4="guvfx_recov_tgt4_$SUF"; createdb "$TGT4"; psql -q -d "$TGT4" -c "create schema extra; create table extra.t(x int)" >/dev/null 2>&1
DESC="restore refuses a target with data only in a NON-public schema"; GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT4" PGDATABASE="$TGT4" expect_fail "$REC/restore.sh" --meta "$META" --execute
TGT5="guvfx_recov_tgt5_$SUF"; createdb "$TGT5"; psql -q -d "$TGT5" -c "create view v as select 1 as x; create sequence s; create function f() returns int language sql as 'select 1'" >/dev/null 2>&1
DESC="restore refuses a target holding only a view/sequence/function (non-empty; attribution guarantee)"; GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT5" PGDATABASE="$TGT5" expect_fail "$REC/restore.sh" --meta "$META" --execute
DESC="idempotent re-restore via explicit drop+recreate (clean lifecycle)"
dropdb "$TGT"; createdb "$TGT"; GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT" PGDATABASE="$TGT" expect_ok "$REC/restore.sh" --meta "$META" --execute
[ "$(PGDATABASE="$TGT" psql -tAqc "select count(*) from trade")" = "500" ] && ok "re-restore reconciles (trade=500)" || bad "re-restore reconciles"

echo; note "=== HIGH-3 + §6: backup safety, capacity estimate, enforceable ceiling (ENOSPC-equivalent) ==="
DESC="invalid --label is refused"; expect_fail "$REC/backup.sh" --out "$WORK" --label 'bad;rm -rf /' --no-encrypt --execute
DESC="non-integer reserved-floor override is refused (fail-closed, not fail-open)"
( GUVFX_BACKUP_MIN_FREE_BYTES=not-a-number "$REC/backup.sh" --out "$WORK" --label floorbad --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="non-integer GUVFX_BACKUP_MAX_ARTIFACT_BYTES is refused (fail-closed)"
( GUVFX_BACKUP_MAX_ARTIFACT_BYTES=huge "$REC/backup.sh" --out "$WORK" --label capbad --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="MEDIUM-2(R1): backup refused at the reserved free floor; leaves NO artifact/metadata"
countfloor() { find "$WORK" -maxdepth 1 -name 'guvfx_floor_*' 2>/dev/null | wc -l | tr -d ' '; }
before="$(countfloor)"
( GUVFX_BACKUP_MIN_FREE_BYTES=9223372036854775807 "$REC/backup.sh" --out "$WORK" --label floor --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC" || {
  after="$(countfloor)"; [ "$after" = "$before" ] && ok "$DESC" || bad "$DESC (left an artifact/metadata)"; }
# NEGATIVE + POSITIVE controls (RULE 11): a cap BELOW the real dump must fail; a cap ABOVE it must
# succeed -- so the enforced ceiling tracks the actual byte value and a future ulimit-unit regression
# (512-vs-1024) would be caught (a cap at exactly realbytes would be ambiguous, so we use /4 and x4).
realbytes="$(sed -n 's/.*"bytes": \([0-9]*\).*/\1/p' "$META" | head -1)"; realbytes="${realbytes:-100000}"
caplo=$(( realbytes / 4 )); [ "$caplo" -lt 1024 ] && caplo=1024
caphi=$(( realbytes * 4 ))
cntlbl() { find "$WORK" -maxdepth 1 \( -name "guvfx_$1_*" -o -name ".guvfx_$1_*" \) 2>/dev/null | wc -l | tr -d ' '; }
DESC="§6: dump EXCEEDING the enforceable output ceiling fails safely -- NO artifact/metadata/partial (ENOSPC-equivalent)"
b6="$(cntlbl enospc)"
( GUVFX_BACKUP_MAX_ARTIFACT_BYTES="$caplo" "$REC/backup.sh" --out "$WORK" --label enospc --no-encrypt --execute ) >/dev/null 2>&1 && bad "$DESC (should have failed on the ceiling)" || {
  a6="$(cntlbl enospc)"; [ "$a6" = "$b6" ] && ok "$DESC" || bad "$DESC (left an artifact/partial/metadata)"; }
DESC="§6: dump WITHIN a generous ceiling SUCCEEDS (positive control -- ceiling tracks bytes, not always-fail)"
if ( GUVFX_BACKUP_MAX_ARTIFACT_BYTES="$caphi" "$REC/backup.sh" --out "$WORK" --label ceilok --no-encrypt --execute ) >/dev/null 2>&1 && ls "$WORK"/guvfx_ceilok_*.dump >/dev/null 2>&1; then ok "$DESC"; else bad "$DESC (a cap well above the dump should have allowed it)"; fi

echo; note "=== MEDIUM-1: encrypted archive FORMAT verification (not just decryptability) ==="
# Build the fake artifact in its OWN subdir so $WORK stays a HEALTHY dir (the gate-usable-on-healthy
# test below audits $WORK; the audit globs non-recursively, so subdir fixtures do not pollute it).
FD="$WORK/fakedir"; mkdir -p "$FD"
printf 'this is not a postgres custom-format archive, just arbitrary text\n' \
  | gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase-fd 3 -o "$FD/fake.dump.gpg" 3< <(printf '%s' "$GUVFX_BACKUP_PASSPHRASE")
fsha="$(rec_sha256 "$FD/fake.dump.gpg")"
printf '{\n "label": "fake",\n "created_utc": "%s",\n "target_class": "test",\n "encrypted": true,\n "bytes": 1,\n "sha256": "%s",\n "artifact": "fake.dump.gpg"\n}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$fsha" > "$FD/fake.meta.json"
DESC="verify REJECTS an encrypted NON-PostgreSQL artifact (decrypts, but not an archive)"; expect_fail "$REC/verify.sh" --meta "$FD/fake.meta.json"
DESC="audit --gate-usable FAILS on the encrypted non-PostgreSQL artifact"
( "$REC/audit_report.sh" --dir "$FD" --gate-usable --max-age-hours 100000 ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
# R2 review MEDIUM: a TRUNCATED encrypted artifact whose sidecar sha256 was updated to MATCH it still
# fails verification -- because the gpg decrypt/MDC exit status is enforced, not masked by pg_restore
# --list reading the front TOC before the tail/MDC failure surfaces.
TR="$WORK/truncdir"; mkdir -p "$TR"; cp "$(ls "$WORK"/guvfx_enc_*.dump.gpg | head -1)" "$TR/trunc.dump.gpg"
tsz="$(wc -c < "$TR/trunc.dump.gpg" | tr -d ' ')"; keep=$(( tsz * 6 / 10 ))
head -c "$keep" "$TR/trunc.dump.gpg" > "$TR/trunc.cut" && mv "$TR/trunc.cut" "$TR/trunc.dump.gpg"
tsha="$(rec_sha256 "$TR/trunc.dump.gpg")"
printf '{\n "label": "trunc",\n "created_utc": "%s",\n "target_class": "test",\n "encrypted": true,\n "bytes": 1,\n "sha256": "%s",\n "artifact": "trunc.dump.gpg"\n}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tsha" > "$TR/trunc.meta.json"
DESC="R2: verify REJECTS a TRUNCATED encrypted artifact even with a MATCHING sha256 (gpg MDC enforced, not masked by front-TOC)"; expect_fail "$REC/verify.sh" --meta "$TR/trunc.meta.json"

echo; note "=== integrity + audit gate semantics (MEDIUM-1/MEDIUM-2) ==="
CD="$WORK/corruptdir"; mkdir -p "$CD"
cart="$(sed -n 's/.*"artifact": "\([^"]*\)".*/\1/p' "$META" | head -1)"; goodsha="$(sed -n 's/.*"sha256": "\([^"]*\)".*/\1/p' "$META" | head -1)"
cp "$WORK/$cart" "$CD/corrupt_copy.dump"; printf 'corrupt' >> "$CD/corrupt_copy.dump"
printf '{\n "artifact": "corrupt_copy.dump",\n "sha256": "%s",\n "encrypted": false\n}\n' "$goodsha" > "$CD/corrupt.meta.json"
DESC="verify FAILS on a corrupted artifact (sha mismatch vs recorded)"; expect_fail "$REC/verify.sh" --meta "$CD/corrupt.meta.json"
DESC="verify FAILS on missing metadata file"; expect_fail "$REC/verify.sh" --meta "$WORK/nope.meta.json"
DESC="audit --verify-integrity --gate FAILS on a present-but-corrupt artifact"
( "$REC/audit_report.sh" --dir "$CD" --verify-integrity --gate ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
# MEDIUM-2: a present+fresh+corrupt artifact passes the INVENTORY gate but fails the USABLE gate.
M2="$WORK/m2dir"; mkdir -p "$M2"; cp "$WORK/$cart" "$M2/rp.dump"; rp_sha="$(rec_sha256 "$M2/rp.dump")"
printf '{\n "label": "m2",\n "created_utc": "%s",\n "target_class": "test",\n "encrypted": false,\n "bytes": 1,\n "sha256": "%s",\n "artifact": "rp.dump"\n}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rp_sha" > "$M2/rp.meta.json"
printf 'x' >> "$M2/rp.dump"   # corrupt AFTER recording its sha
DESC="MEDIUM-2: plain --gate (INVENTORY) PASSES a present+fresh+corrupt artifact (inventory != usability)"
( "$REC/audit_report.sh" --dir "$M2" --gate ) >/dev/null 2>&1 && ok "$DESC" || bad "$DESC (inventory gate should pass; corruption is not an inventory property)"
DESC="MEDIUM-2: --gate-usable FAILS the same present+fresh+corrupt artifact (usable-recovery gate)"
( "$REC/audit_report.sh" --dir "$M2" --gate-usable --max-age-hours 100000 ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="R2: --gate-usable WITHOUT --max-age-hours is refused (must evaluate freshness, not assume it)"
( "$REC/audit_report.sh" --dir "$WORK" --gate-usable ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="R2: non-integer --max-age-hours is refused (fail-closed, not silently skipped)"
( "$REC/audit_report.sh" --dir "$WORK" --max-age-hours not-a-number --gate ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="R2: --gate-usable FAILS a present+integrity-valid but STALE recovery point (freshness bound enforced)"
( "$REC/audit_report.sh" --dir "$WORK" --gate-usable --max-age-hours 0 ) >/dev/null 2>&1 && bad "$DESC (max-age 0 must be refused as no bound)" || ok "$DESC"
DESC="audit --gate fails closed on an unparseable recovery-point timestamp"
mkdir -p "$WORK/badts"; printf '{\n "label":"badts",\n "created_utc":"not-a-timestamp",\n "target_class":"test",\n "encrypted":false,\n "bytes":1,\n "artifact":"d.dump"\n}\n' > "$WORK/badts/x.meta.json"; : > "$WORK/badts/d.dump"
( "$REC/audit_report.sh" --dir "$WORK/badts" --max-age-hours 1 --gate ) >/dev/null 2>&1 && bad "$DESC" || ok "$DESC"
DESC="audit --gate-usable PASSES on the healthy recovery-point dir (fresh + integrity-verified)"
( "$REC/audit_report.sh" --dir "$WORK" --gate-usable --max-age-hours 100000 ) >/dev/null 2>&1 && ok "$DESC" || bad "$DESC (healthy dir should pass)"

echo; note "=== §6-H: target populated between preflight and execute is DETECTED ==="
TGTH="guvfx_recov_tgth_$SUF"; createdb "$TGTH"; export GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGTH"
PGDATABASE="$TGTH" "$REC/restore.sh" --meta "$META" >/dev/null 2>&1 || true   # dry-run passes (empty)
psql -q -d "$TGTH" -c "create table injected_by_concurrent_writer(x int)" >/dev/null 2>&1
DESC="restore --execute aborts when the target became NON-EMPTY after preflight (re-assert empty)"
PGDATABASE="$TGTH" expect_fail "$REC/restore.sh" --meta "$META" --execute

echo; note "=== audit report (informational) ==="
export GUVFX_RECOVERY_ALLOWED_TARGET_DB="$TGT"
"$REC/audit_report.sh" --dir "$WORK" --verify-integrity || true

echo; note "================= CERTIFY SUMMARY: PASS=$PASS FAIL=$FAIL ================="
[ "$FAIL" -eq 0 ] || { note "CERTIFICATION FAILED"; exit 1; }
note "CERTIFICATION PASSED"
