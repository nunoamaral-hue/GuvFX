# Runbook — Portfolio GBP→USD conversion (ADR-0058, DARK)

Operational procedure for the Fed H.10 `DEXUSUK` portfolio conversion. **DARK**: every step below is GATED on a
separate governed GO. Nothing here runs automatically; the request path never contacts FRED.

## Components
- Model `analytics.DailyFxObservation` (append-only revisions, DB check constraints, reproducibility fields)
  + migrations `analytics/0002_dailyfxobservation` and `analytics/0003_fx_observation_append_only_triggers`
  (BEFORE UPDATE/DELETE triggers).
- Parser `analytics.fx_fred` · stored source `analytics.fx_source.StoredFredFxRateSource` (DB-only, Europe/London).
- Commands: `fetch_fx_daily` (ingest/backfill) · `fx_ingestion_health` (readiness, read-only).
- Flag `PORTFOLIO_FX_CONVERSION_ENABLED` (settings/env; **default OFF**).
- F-8 grants: `ops/fx/grants_fx_observation.sql` (SELECT-only runtime + SELECT+INSERT ingestion writer + sequence USAGE).
- A3 ingestion identity: a dedicated Django DB alias `fx_ingest`, configured ONLY from distinct `FX_INGEST_DB_USER`
  / `FX_INGEST_DB_PASSWORD` (+ optional `FX_INGEST_DB_NAME/HOST/PORT`, defaulting to the same physical DB as
  `default`). `fetch_fx_daily` routes its advisory lock + idempotency read + insert through `fx_ingest` on one
  connection and **fails closed** (CommandError) when the alias is absent — it never falls back to the web-runtime
  `default` connection. `analytics.fx_db_router.FxIngestRouter` blocks migrations on `fx_ingest`. The ingestion
  credential MUST be injected ONLY into the FX cron job's environment, never the web/worker runtime.

## ENABLEMENT PRECONDITION (F-8) — BLOCKING
Do not enable the flag in production until the runtime-role cutover is complete and `ops/fx/grants_fx_observation.sql`
is applied (SELECT-only web runtime; INSERT-only governed ingestion writer). The append-only triggers hold
against every ordinary role, but a superuser/owner app identity can still TRUNCATE or bypass triggers — so DB
immutability is only authoritative once the runtime identity is separated (ADR-0058).

## State invariants
- Flag OFF ⇒ dashboard byte-identical to today (`NullFxRateSource`); payload has no `fx` key.
- Request path is **database-only** — `fetch_fx_daily` is the only component that touches the network, and only
  the keyless FRED CSV over HTTPS. No credentials anywhere.

## Procedure (each step needs a separate GO)

### 1. Apply the migrations (schema + append-only triggers)
```
python manage.py migrate analytics 0003_fx_observation_append_only_triggers
```
Additive table + check constraints (0002) and BEFORE UPDATE/DELETE triggers (0003); no data change. Reversal is
explicit and audit-safe: `migrate analytics 0002` drops only the triggers (rows preserved); `migrate analytics
0001` drops the table (destructive — export first; see Rollback). Forward/rollback rehearsed on a disposable
PostgreSQL DB (triggers installed, UPDATE/DELETE blocked, rows survive trigger-reversal). Apply
`ops/fx/grants_fx_observation.sql` as part of the F-8 cutover.

### 2. Seed / backfill the rate store (flag still OFF)
Live (keyless, HTTPS) — ingests the full `DEXUSUK` history, idempotent:
```
python manage.py fetch_fx_daily            # live FRED CSV download
python manage.py fetch_fx_daily --dry-run  # parse + report only, writes nothing
python manage.py fetch_fx_daily --fixture /path/to/dexusuk.csv   # offline/air-gapped backfill
```
Re-running is a no-op for unchanged observations; a revised source value appends a new revision. Review the JSON
summary (`accepted`, `rejected`, `rejected_reasons`, `written_new`, `duplicate_skipped`, `latest_effective_date`).

### 3. Check readiness (read-only)
```
python manage.py fx_ingestion_health
# {"status":"HEALTHY|STALE|EMPTY", "latest_effective_date":..., "age_days":..., "convertible":true|false, ...}
```
Proceed to enablement only when `status == HEALTHY` and `convertible == true`.

### 4. Enable the flag (reversible, no deploy)
Set `PORTFOLIO_FX_CONVERSION_ENABLED=1` (settings override or env; read live). The summary payload then carries
an `fx` block and GBP converts into the USD total with disclosed provenance.

### 5. Verify (read-only)
- `GET /api/analytics/portfolio/summary/?scope=ALL` → `fx.available == true`, `fx.rate`, `fx.effective_date`,
  `fx.source == "FED_H10"`; `equity_usd.state` moves toward `OK` with the GBP account included.
- Dashboard shows `GBP→USD <rate> · <effective_date> · Fed H.10` under Equity (USD).
- Single-account GBP view (49) unchanged (still native GBP).

## Rollback
- **Immediate (no deploy):** unset `PORTFOLIO_FX_CONVERSION_ENABLED` (or set to `0`). The dashboard instantly
  reverts to honest PARTIAL (GBP excluded); the `fx` key disappears. The store is inert while OFF.
- **Code:** redeploy the prior image (the FX code is additive and dormant while the flag is OFF).
- **Schema (last resort):** `migrate analytics 0001` drops the table (only after the flag is OFF and no consumer
  reads it).

## Scheduling (DOCUMENTED — NOT APPLIED in this PR)
Once enabled, schedule `fetch_fx_daily` **once per business day** (after the Fed H.10 publication, ~mid-afternoon
ET) via the platform's existing scheduled-task mechanism (a systemd timer / compose cron entry), mirroring the
equity-snapshot cron. The job is idempotent, so overlap or a missed day is safe (carry-forward + backfill cover
gaps). **Do not** register the schedule until a separate GO. Monitor `fx_ingestion_health` → page/alert on
`STALE` or `EMPTY`; a bounded alert fires on fetch/parse failure (never logs the payload).

## Hard boundaries (unchanged by this runbook)
No Copy Trader execution, no MT5 bridge change, no Withdrawal Intelligence change, no production external-provider
request, and no cross-source substitution (Bank of England stays monitoring-only).
