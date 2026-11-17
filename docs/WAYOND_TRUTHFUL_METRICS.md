# Wayond truthful metrics (pkt-151 / ADR-0116)

A value-blind, DARK operator read-model for Wayond signal **performance** (latency) and
**quality/reliability**. Its one rule: **tell the truth**. A figure appears only when genuine
persisted evidence supports it; otherwise it reads `N/A` / `UNMEASURABLE` with a reason — never a
fabricated `0`, success, or invented duration. Broker-fill latency is a separate, offset-unverified
domain and is **never** combined with internal submission latency.

## Surfaces (all DARK / staff-only)

- **Service:** `reliability/services/wayond_metrics.py` → `build_wayond_metrics(now, window_days, include_synthetic)`.
- **Endpoint:** `GET /api/reliability/wayond-metrics/` — `IsAdminUser` + `WAYOND_METRICS_OPERATOR_ENABLED`
  (unset ⇒ **404**). Non-staff ⇒ 403; staff while dark ⇒ 404.
- **Command:** `python manage.py wayond_metrics [--window-days N] [--include-synthetic]` — read-only JSON.
- **UI:** `admin/operations/wayond-metrics` — coverage, latency stages, truthful N/A block. Backend is the real gate.

## How to read it

- **Latency status** — `MEASURED` (≥ 20 valid samples; p50/p95/p99 shown), `PARTIAL` (some samples, percentiles
  withheld), `N/A` (no valid samples), `UNMEASURABLE` (evidence structurally absent). `excluded_skew` counts
  negative/forward-skew deltas that were dropped fail-closed (never clamped to 0); `excluded_missing` counts rows
  with a missing endpoint.
- **Count status** — `OK` / `PARTIAL` / `UNAVAILABLE` / `EMPTY`. A rate is `null` (not `0.0`) when its denominator is 0.
- **Clock domains** — `provider_telegram_utc`, `platform_server_utc`, and `broker_wall_as_if_utc_offset_unverified`.
  A stage that crosses domains (e.g. source→acquisition) is labelled `cross_domain_...`.

## What is measured vs N/A

| Measured (where data exists) | N/A / UNMEASURABLE (genuinely absent) |
|---|---|
| source → acquisition (cross-domain) | parse start/complete (unstamped) |
| acquisition → approval (platform, via FK) | approval → submission (no ExecutionJob correlation column) |
| approval decision time (platform) | submission → broker ACK (MT5 carries no time) |
| outcome / stale / dead-letter counts | broker ACK → fill (separate unverified broker clock) |
| approval rate / time, plan held/voided + reasons | end-to-end source → submission (correlation incomplete) |
| correlation completeness (provenance) | duplicate rate, fill rate, requotes, slippage, listener liveness, flood-wait/retry |

Wayond is un-armed in production, so execution-side plan/job/trade metrics read `EMPTY` truthfully; the rich measured
data is at the signal-intake layer.

## Boundaries

- **Value-blind:** aggregate counts/rates/percentiles + provider slug + categorical reason codes only. No message
  body, Telegram handle, chat id, account number, email, credential or token is ever emitted.
- **Synthetic/genuine:** genuine = `non_genuine=False` (intake rows carry no per-row synthetic flag — a documented
  caveat). Downstream shadow/test exclusion (`source_stage=TEST`, `PLACE_ORDER_SHADOW`, `__ops_probe__`/`OPSPROBE`)
  applies when execution is armed.
- **Publication:** no public Wayond/broker page is activated. A future public page could at most publish coarse,
  k-anon-thresholded quality counts with small-cohort suppression — never raw latency percentiles or per-tenant
  figures. Documented only (see ADR-0116).

See `docs/architecture/ADR-0116-wayond-truthful-metrics.md` for the full metric contract and future-instrumentation
gaps.
