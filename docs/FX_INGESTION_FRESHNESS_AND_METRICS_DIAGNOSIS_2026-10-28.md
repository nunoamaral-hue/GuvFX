# FX Ingestion Freshness & Metrics Integrity — Diagnosis + DARK Correction (2026-10-28)

**Priority:** P1. **Authority:** read-only production diagnosis + repository-only DARK correction.
**Production unchanged:** no writes, schedule change, backfill, migration, deploy, restart, flag, credential,
role/grant. All FX flags remain OFF/default. The `--dry-run` reachability probes write nothing.

## 1. First failing boundary & root cause (freshness)
**Boundary = SCHEDULING (process is never launched).** The FX ingestion commands (`fetch_fx_boe`,
`fetch_fx_daily`) are **UNSCHEDULED in production by design** ("until a separate Sponsor GO", per their
docstrings). The host crontab + `/etc/cron.d` + `/etc/crontab` contain **no FX entry** and there is **no FX log**
in `/var/log/guvfx`. Existing observations came from ad-hoc operator runs; the last was ~2026-10-26.
Downstream stages are healthy: the `fx_ingest` DB alias is configured (`FX_INGEST_DB_USER=guvfx_fx_ingest`), the
BoE provider is reachable and fresh, validation/append-only controls are intact. So the cause is a **production
configuration gap, not a code defect** — repaired only under Sponsor GO (§6), not in this PR.

## 2. Current FX freshness & exact risk date (verified, read-only)
- Request path: `PORTFOLIO_FX_SOURCE_PRIORITY=BOE,FED_H10`; support@ in `PORTFOLIO_FX_COHORT_ALLOWLIST`.
- BoE latest effective **2026-10-22** (rate 1.3363), age_days **5**, `available: true` today.
- Fed H.10 latest effective **2026-10-16** (age 11 → beyond the 7-day cap); `fetch_fx_daily --dry-run` on prod
  returns **TimeoutError** (FRED unreachable from prod — why Fed is stale). BoE `--dry-run`: reachable, 22 rows,
  **latest upstream 2026-10-27** (5 days we have not ingested). A single run would refresh to Oct 27.
- **Exact risk date:** selection uses `target_date = London_today − 1` and refuses `age > 7`. With BoE stuck at
  2026-10-22, on **London date ≥ 2026-10-31** (target ≥ Oct 30) the rate ages past 7 days → GBP accounts 49 & 52
  drop to PARTIAL/UNAVAILABLE and `equity_usd` falls from OK $2,837.53 to ~$1,109 reported, unless ingestion
  resumes first.

## 3. Dashboard arithmetic today
Correct. `balance_usd` $2,637.53 (OK 6/6), `equity_usd` $2,837.53 (OK 6/6) at BoE 1.3363. No fabricated/0/1 rate
(selection never picks today/future, rejects zero/implausible, degrades to honest unavailable on any fault). No
double conversion: summary converts each native amount once; equity-curve uses `NullFxRateSource` (native,
point-in-time); realised metrics do not convert.

## 4. Performance "N/A" classification
**Expected / honest** for the 6 monitoring accounts (genuine absence of closed-trade history: 0 trades /
assignments / ExecutionJobs / plans ever). Not a collection failure, not a projection failure. The GBP accounts
(49, 52) additionally show realised metrics as UNAVAILABLE because `portfolio_metrics` **never FX-converts
realised trade P&L** (it takes no rate source) — a currency-exclusion, by design. No selected-account vs
All-Accounts divergence beyond that currency rule. **Important:** FX-converting historical realised P&L with
today's spot would violate point-in-time correctness (data rule) — the coherent fix is per-close-date historical
rates (a separate, larger packet), NOT a naive conversion. Not attempted here.

## 5. DARK correction in this PR (code defects proven) — one coherent FX contract for source/series + health
The rate/arithmetic were correct; the recorded/disclosed **series** and the **health** signal were incoherent
with the governed priority. Three tight fixes (request-path RATE selection unchanged):
1. `fx_ingest.append_fx_observation` hardcoded the Fed series for every source → BoE rows stored as `DEXUSUK`.
   Added a `source_series` param (defaults to Fed for the Fed caller); `fetch_fx_boe` now passes `XUDLUSS`.
2. `StoredDbFxRateSource.provenance`/`get_rate` now derive the series from the **selected source**
   (`series_for_source`), correcting disclosure for historical mislabelled rows without editing the append-only
   store (this is why the live dashboard showed `source_series: "DEXUSUK"` under `selected_source: BOE`).
3. `fx_ingestion_health` is now evaluated over the **governed priority** (was Fed-only), so the health signal
   matches the request path (it previously reported STALE/convertible=false while the BoE-first dashboard
   converted fine).
Tests: `backend/analytics/tests_fx_coherence.py` (ingest stamping; BoE series disclosure incl. legacy-mislabel
correction; priority-aware health incl. the Fed-stale/BoE-fresh case; unavailable/empty; USD-identity /
no-double-conversion). Full analytics FX suite green (100 tests, 1 pre-existing skip); migration drift none.
No flags/behaviour-of-rate changed; **not merged, not deployed.**

## 6. Production repair proposal for the freshness gap (Sponsor GO required — NOT performed)
- **Change:** add a scheduled run of `fetch_fx_boe` (BoE-first, reachable, T+1 fresh) — e.g. a daily host cron
  mirroring the existing `deploy/*/crontab.*` pattern:
  `30 6 * * * cd /home/ubuntu/guvfx-prod && docker compose exec -T guvfx-backend python manage.py fetch_fx_boe >> /var/log/guvfx/fetch_fx_boe.log 2>&1 # guvfx-fetch-fx-boe`
  (optionally also `fetch_fx_daily` once FRED egress is restored). Requires the `fx_ingest` role (already
  configured). Do **not** widen `MAX_STALENESS_DAYS` (that would present stale FX as fresh).
- **Expected effect:** DailyFxObservation refreshes to the latest BoE effective date within minutes; dashboard
  `fx.age_days` stays ≤ 7; GBP accounts keep converting past 2026-10-31.
- **Rollback:** remove the crontab line (append-only observations are never edited/deleted).
- **Verification:** `python manage.py fetch_fx_boe --dry-run` (reachability), then confirm a new ACCEPTED
  `DailyFxObservation` (BoE, effective ≥ 2026-10-26) and `fx_ingestion_health` → HEALTHY/BoE/XUDLUSS.
- **Also investigate (separate):** FRED egress TimeoutError from the prod container (network/firewall), so Fed
  H.10 can serve as the disclosed failover again.

## Boundaries honored
No production writes/schedule/backfill/migration/deploy/restart/credential/role/flag change; `is_active`
untouched; no Withdrawal Intelligence / Copy Trading / MT5 bridge / OAuth / Tailscale / n8n change; execution
controls unchanged; no secrets exposed.
