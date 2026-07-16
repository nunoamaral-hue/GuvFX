# TP-protection watcher — runbook

`GFX-PKT-TP-PROTECTION-LATENCY`. A dedicated, VPS-side, adaptive watcher that makes the incremental
TP-protection ladder react in ~1 second (instead of the ~1-minute monitor cadence) **during the
narrow protection window only**, without the blast radius of running the whole monitor chain every
second.

## What it is (and is not)
- **Is:** a thin adaptive *driver* over the existing state machine (`execution.breakeven.sweep_breakeven`),
  scoped to **ti_signals only**, enqueue-only, holding **no MT5/bridge credentials**. It reuses
  `ExecutionJob` + the ingest worker + the bridge exactly as the minute chain does.
- **Is not:** a second protection implementation, a global 1-second poller, or a Claude/laptop process.
  Wayond is never touched. The minute monitor chain **remains** as the slower reconciliation fallback.

## Safety properties
- **Single-flight:** a Postgres advisory lock (`pg_try_advisory_lock`) — a duplicate start idles.
- **Idempotent:** the ladder's per-(ticket,stage) in-flight guard + monotonic `protection_stage` mean
  a faster poll **never** enqueues a duplicate MODIFY, so bridge calls do not scale with cadence.
- **Self-healing ingestion:** each tick also reclaims lease-expired RUNNING SYNC/MODIFY jobs. Combined
  with the short protection-sync lease (`EXECUTION_SYNC_LEASE_TTL_SECONDS`, default 60s), a stranded
  sync frees ingestion within ~a minute instead of blinding the ladder for several minutes.
- **Fail-safe:** every tick is wrapped; a transient error logs and the loop continues. If the process
  dies, the advisory lock frees automatically and `restart: unless-stopped` brings it back.
- **Inert unless armed:** requires **both** `BREAKEVEN_ENABLED` (the ladder) and `TP_WATCHER_ENABLED`
  (this watcher). Deploy dark, verify, then arm.

## Cadence (env-tunable)
| Env | Default | When |
|-----|---------|------|
| `TP_WATCHER_IDLE_INTERVAL`   | 30s | no eligible ti_signals plan open |
| `TP_WATCHER_PRE_INTERVAL`    | 3s  | plan open, before any protection is due |
| `TP_WATCHER_ACTIVE_INTERVAL` | 1s  | a protection stage is due / in-flight / softly deferred |

## Deploy (from `/home/ubuntu/guvfx-prod`)
```bash
# 1) Bring it up DARK (TP_WATCHER_ENABLED defaults to 0):
GUVFX_COMPOSE_BASE=docker-compose.yml \
docker compose -f docker-compose.yml \
  -f /home/ubuntu/guvfx-app/deploy/tp-protection-watcher/docker-compose.tp-watcher.yml \
  up -d --no-deps guvfx-tp-protection-watcher

# 2) Verify (no writes):
bash /home/ubuntu/guvfx-app/deploy/tp-protection-watcher/verify_watcher.sh

# 3) Arm: set TP_WATCHER_ENABLED=1 in telegram.env, then recreate:
docker compose -f docker-compose.yml \
  -f /home/ubuntu/guvfx-app/deploy/tp-protection-watcher/docker-compose.tp-watcher.yml \
  up -d --no-deps --force-recreate guvfx-tp-protection-watcher
```

## Disarm / rollback
Set `TP_WATCHER_ENABLED=0` (recreate) **or** `docker compose ... stop guvfx-tp-protection-watcher`.
The minute monitor chain keeps protecting at its slower cadence — nothing is lost, no state to unwind.

## Benchmark
`docker exec guvfx-backend python manage.py run_tp_protection_watcher --once --dry-run` prints
`{cadence_s, elapsed_ms, queries, ...}` for one tick against the current DB, persisting nothing.
