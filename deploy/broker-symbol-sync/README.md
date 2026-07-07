# Broker-symbol nightly sync (GFX-PKT-BROKER-SYMBOL-DEPLOY-AND-SYNC)

Keeps the `execution.BrokerInstrument` cache fresh so the broker-aware symbol registry
(`execution.broker_symbols`) resolves the current broker offering, not a stale snapshot.

## What it does
- **Nightly** (default `17 2 * * *`): `manage.py sync_broker_instruments --account <id>` fetches
  the broker's live symbol list from the bridge `GET /mt5/symbols` and upserts the cache. Stale
  symbols are marked `enabled=False` (kept for audit, never deleted). **Places no order.**
- **Staleness visibility**: `manage.py broker_instrument_status` (read-only) reports each account's
  last sync time, enabled/total counts, and a `** STALE **` warning past `BROKER_SYMBOLS_STALE_HOURS`
  (default 48h). Wire it as a morning cron (commented line in `crontab.broker-sync`) or run ad hoc.

## Why cron (Option D), not per-signal
A broker's tradeable symbol set changes rarely, so a nightly refresh is ample and reuses the exact
host-cron pattern already used by `deploy/monitor-scheduler` and the h1/m5/h4 schedulers — no new
scheduler service (per `.claude/rules/architecture.md`). Per-signal sync would add a network
round-trip on the hot path and risk the bridge throttle. A reconnect-triggered refresh (Option C)
is a sensible future enhancement on top of this.

## Install / remove (on the prod host, as the crontab owner)
```bash
ACCOUNTS="1" deploy/broker-symbol-sync/install_broker_sync_cron.sh     # idempotent
deploy/broker-symbol-sync/install_broker_sync_cron.sh --remove         # clears the guvfx-broker-sync line(s)
```
Overridable: `COMPOSE_DIR`, `LOG_DIR`, `BACKEND_SERVICE`, `SCHEDULE`, `ACCOUNTS`. The installer only
touches lines ending in the `# guvfx-broker-sync` marker; the strategy and monitor crons are untouched.

## Auth note
The bridge `GET /mt5/symbols` authenticates via the **`X-GuvFX-Agent-Token`** header (agent token,
worker-token fallback) — not the `X-Worker-Token` used by the POST order endpoints. The sync command
sends the correct header; a 401 usually means the backend env lacks a valid `GUVFX_WINDOWS_AGENT_TOKEN`.

## Rollback
`install_broker_sync_cron.sh --remove`. No data migration; the cache itself is rebuildable from the
broker at any time by re-running the sync.
