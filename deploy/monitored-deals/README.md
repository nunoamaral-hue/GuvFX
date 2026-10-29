# Monitored deal-history ingestion

Keeps the dashboard's realized metrics (Net P&L, Win Rate, Profit Factor, Max Drawdown, Expectancy,
closed-trade count, daily realized P&L) **truthful for monitoring-only accounts** — accounts that are
connected and shown on the dashboard but hold no open GuvFX execution plan.

## Why

Closed-trade history is ingested by the `SYNC_POSITIONS` worker (reads each account's own per-tenant
bridge `GET /mt5/snapshots/deals`, a 90-day window, and upserts one `trading.Trade` per closed position —
identity-firewalled, idempotent). Historically a sync was enqueued only while an account held an **open
plan** (`execution.breakeven._ensure_position_sync`) or right after a placement. A monitoring-only account
therefore never got a sync, so its real broker closed trades were never ingested and the dashboard showed
"no closed-trade history" / N/A — **even when the broker held trades**. Observed 2026-10-29 on account 46:
the broker held 48 closed trades (+$163.92 net) while GuvFX had 0; one sync ingested all 48 and the metrics
populated correctly.

This cron is the durable trigger: every ~15 min it enqueues one read-only `SYNC_POSITIONS` per monitored
account. It places **no** orders and changes **no** execution semantics (`execution.monitored_ingest`).

## Install

```bash
deploy/monitored-deals/install_monitored_deals_cron.sh          # install (DARK until the flag is set)
deploy/monitored-deals/install_monitored_deals_cron.sh --remove # remove
```

## Arm

The command is **DARK** until `MONITORED_DEALS_INGEST_ENABLED=1` is set in the `guvfx-backend` container env
(then recreate the backend). Until then it no-ops. Validate once without arming:

```bash
docker compose exec -T guvfx-backend python manage.py ingest_monitored_deals --force
```

## Guarantees

- Scoped to **connected accounts with a READY per-tenant read endpoint** (exactly the dashboard's monitored set).
- **Idempotent**: skips an account that already has a PENDING/RUNNING sync.
- **Fail-closed**: an account with no resolvable Windows identity is skipped (never guessed).
- **Quiet backoff**: an account whose last monitored probe FAILED within the cooldown (default 180 min) is
  re-probed at most once per cooldown — and auto-recovers within one cooldown once its bridge can serve the read.
- Read-only: the enqueued job only reads deals and upserts closed-trade rows; it never touches the order path.

## Rollback

Remove the cron (`--remove`) and/or unset `MONITORED_DEALS_INGEST_ENABLED`. Ingested `trading.Trade` rows are
true broker history and are left intact (idempotent; a revert does not falsify them).
