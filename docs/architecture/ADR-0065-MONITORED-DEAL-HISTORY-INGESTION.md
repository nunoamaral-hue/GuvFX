# ADR-0065 — Periodic read-only deal-history ingestion for monitored accounts

- **Status:** Accepted (additive; read-only ingestion; no order path). DARK until `MONITORED_DEALS_INGEST_ENABLED`.
- **Date:** 2026-10-29
- **Workstream:** Dashboard / data integrity (P0 trade-history reconciliation)
- **Relates to:** the `SYNC_POSITIONS` ingest worker (`mt5_trade_ingest_worker.py`), `trading.position_ingest`
  (`build_positions_from_deals`), `execution.breakeven._ensure_position_sync` (the open-plan freshness sync),
  `execution.snapshot_transport` (per-tenant read isolation), ADR LIVE_READONLY_MONITORING (live deals read).

## Context

The dashboard's realized metrics (Net P&L, Win Rate, Profit Factor, Max Drawdown, Expectancy, closed-trade
count, daily realized P&L) are computed in `analytics.portfolio` from `trading.Trade` rows
(`close_time__isnull=False`), with **no** `source_stage` / `is_demo` filter. Those rows are ingested by the
`SYNC_POSITIONS` worker, which reads each account's OWN per-tenant bridge `GET /mt5/snapshots/deals`
(a fixed 90-day window) and upserts one `Trade` per closed position — identity-firewalled and idempotent;
deposits/withdrawals/credits carry no `position_id` and are never entered as trades.

A `SYNC_POSITIONS` job was ENQUEUED only (a) as the auto-sync immediately after a placement, or (b) by the
breakeven/TP freshness sweep **for accounts that currently hold an OPEN execution plan**
(`_ensure_position_sync`). A **monitoring-only** account — connected, shown on the dashboard, but with no GuvFX
execution — therefore never received a sync. Its real broker closed trades were never ingested, so the
dashboard showed "no closed-trade history" / N/A metrics **even when the broker held trades**.

Verified at the source on 2026-10-29 (read-only probe of each account's own bridge): account **46**
(TradersWay-Live) held **48 closed trades** over 90 days (+$180.16 gross trading, −$7.44 commission, −$8.80 swap;
net **+$163.92**) while GuvFX had persisted **0** `Trade` rows. Triggering one `SYNC_POSITIONS` ingested all 48
(`inserted=48, deals_count=99`) and the dashboard API for account 46 immediately returned truthful metrics
(48 trades, 75.0% win rate, PF 2.295, max drawdown $98.50, expectancy $3.42, net +$163.92, `state=OK`). The
machinery was correct; the only gap was the **trigger scope**.

## Decision

Add a dedicated, **additive** periodic trigger — not a change to the execution/ingestion path.

- `execution/monitored_ingest.py`: `monitored_account_ids()` (connected accounts with a READY per-tenant
  endpoint) and `enqueue_monitored_deal_syncs()` which enqueues one read-only `SYNC_POSITIONS` per monitored
  account that lacks an in-flight sync. It reuses the single hosted-aware identity resolver
  (`execution.breakeven._sync_windows_username`) so monitored ingestion and the breakeven sweep can never
  diverge, and it creates **no** orders.
- `execution/management/commands/ingest_monitored_deals.py`: thin command, **DARK** unless
  `MONITORED_DEALS_INGEST_ENABLED` (mirrors `capture_equity_snapshots`).
- `deploy/monitored-deals/`: a ~15-minute cron (mirrors `deploy/equity-snapshots`), DARK-safe to install.

Guarantees: idempotent (skips an in-flight sync; `upsert_trades` is replay-safe), fail-closed on a missing
Windows identity, and a quiet re-probe backoff (an account whose last monitored probe FAILED within the
cooldown — default 180 min — is re-tried at most once per cooldown, and auto-recovers within one cooldown
once its bridge can serve the read).

## Why not change the existing sweep

Extending `_ensure_position_sync` to the whole estate would widen an execution-sweep concern and run deal
ingestion at the 1-minute monitor cadence. A separate command + cron keeps the data/monitoring concern
distinct from execution (per the architecture rule), is independently controllable and testable, and matches
the existing `capture_equity_snapshots` / FX-ingestion cron precedents.

## Consequences

- Monitored accounts with a **readable** bridge (e.g. #46, #50) get their true broker closed-trade history
  ingested and kept fresh; the dashboard shows truthful metrics or an honest empty state when the broker
  genuinely holds no trades (e.g. #50: one balance deposit, zero trades).
- Accounts whose bridge **cannot currently serve the live deals read** (see Boundary) will have their monitored
  probe FAIL (0 rows persisted — never wrong rows) and be backed off; their history ingests automatically once
  their bridge is updated. No order is ever placed.

## Boundary (reported, not changed here)

Accounts #48/#49/#51/#52 run a **stale bridge** whose `/mt5/snapshots/deals` still refuses a live account with
`account_not_demo` — the pre-LIVE_READONLY_MONITORING gate that `scripts/mt5_signal_bridge.py` (and the bridges
on #46/#50) have already removed. Updating those four bridges is an MT5-host action outside this workstream's
dashboard/data code scope and is reported for a separate, Sponsor-gated bridge update. Until then those four
accounts' history remains unreadable at the source (not hidden by GuvFX).

## Rollback

Remove the cron (`--remove`) and/or unset `MONITORED_DEALS_INGEST_ENABLED`, then `git revert` the PR. Ingested
`Trade` rows are true broker history and are left intact (idempotent; the revert does not falsify them).

## Evidence

- Source probe + before/after: account 46 `0 → 48` closed trades, net **+$163.92**; dashboard API `state=OK`.
- Tests: `execution/tests_monitored_ingest.py` (8) — monitored-set filter, idempotent dedup, fail-closed on no
  identity, cooldown (recent/old/non-monitored), and the trading-boundary invariant (only `SYNC_POSITIONS` is
  ever created).
