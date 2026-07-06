# Monitor Scheduling (E3-MONITOR-SCHEDULING)

Runbook for scheduling the **post-trade monitor chain** — the lifecycle that runs after a
(future) demo order closes. **Repo-only preparation:** these artefacts document and enable the
schedule; nothing here is deployed by merging, and installing the cron changes no trading
behaviour.

## What it schedules

One command, `run_monitor_chain`, which runs the three existing monitors in dependency order
in a single pass:

```
process_closed_trades()  # closed Trade  -> internal TradeOutcomeRecord (WIN/LOSS/BE)
route_outcomes()         # WIN record    -> internal PENDING NotificationCandidate
dispatch_pending()       # PENDING cand. -> DRY-RUN transport (no-op unless flag ON; never sends)
```

## Why it is safe at current defaults

- **No order.** None of the three functions places or checks an order — no `order_send`, no
  `ExecutionJob` creation, no bridge/agent call. Verified statically and behaviourally.
- **No Telegram.** `dispatch_pending` is behind `NOTIFICATION_DISPATCH_ENABLED` (**default OFF**),
  so it is a no-op. Even when enabled, the only transport is `TelegramDryRunTransport`, which
  renders a message but **never transmits** (no API, no token, no HTTP) — every
  `NotificationDelivery.transmitted` row is `False`.
- **No WIMS.** Nothing in the chain imports `wims` or creates a `ConsumptionContract`.
- **Only pre-existing rows.** Each step processes rows that already exist (closed trades /
  outcome records / candidates) and creates internal records only.
- **Idempotent.** `TradeOutcomeRecord` is a OneToOne on the trade; the outcome router filters
  `routed=False`; the dispatcher atomically claims `PENDING/FAILED → PROCESSING`. Running every
  minute never double-processes.
- **Resilient.** A step that raises is logged and the chain continues (each step is independent).

With no active demo outcomes today the chain is effectively a no-op; when E3 is later enabled it
begins classifying real closed demo trades — still internal records + dry-run only until the
notification transport is separately, explicitly enabled under its own gated packet.

## Files

| File | Purpose |
|------|---------|
| `crontab.monitor` | The cron snippet (recommended single ordered line + a commented 3-cron alternative). |
| `install_monitor_cron.sh` | Idempotent install/remove of the managed cron line (marker `# guvfx-monitor-chain`). Preserves the existing scheduler crons. |
| `verify_monitor_chain.sh` | Post-install check: runs the chain once, asserts no order / no WIMS contract / nothing transmitted. |
| (backend) `manage.py run_monitor_chain` | The ordered chain command (orchestration only; reuses the three shipped functions). |

## Scheduling approach

The prod estate schedules background work with the **host crontab** (the h1/m5/h4 strategy
schedulers already run this way, every minute, via `docker compose exec -T guvfx-backend …`
logging to `/var/log/guvfx/`). The monitor chain follows the same pattern — no new scheduler
infrastructure (no Celery/cron container/queue), matching the "simplest thing that works" rule.

## Install (does not touch trading)

Run on the prod app host as the user that owns the existing scheduler crontab:

```bash
deploy/monitor-scheduler/install_monitor_cron.sh
```

This appends one line (idempotent; re-running is a no-op) and creates `/var/log/guvfx` if
missing. It does **not** modify or remove the existing scheduler lines. Tunables via env:
`COMPOSE_DIR` (default `/home/ubuntu/guvfx-prod`), `LOG_DIR` (default `/var/log/guvfx`),
`BACKEND_SERVICE` (default `guvfx-backend`), `SCHEDULE` (default `* * * * *`).

## Verify

```bash
deploy/monitor-scheduler/verify_monitor_chain.sh
```

Expected: `== VERIFICATION PASSED ==`. It runs a structural `--limit 0` smoke pass (zero rows
touched) and a real bounded pass, then asserts the `ExecutionJob`, `ConsumptionContract`, and
transmitted-`NotificationDelivery` counts are all unchanged.

Watch the schedule live:

```bash
crontab -l | grep guvfx-monitor-chain      # confirm the line is present
tail -f /var/log/guvfx/monitor_chain.log   # one summary line per minute
```

Each log line reads e.g.
`monitor-chain: close[processed=0 …] outcome[routed=0 …] dispatch[enabled=False …] failures=none …`.

## Rollback

```bash
deploy/monitor-scheduler/install_monitor_cron.sh --remove
```

Removes only the marked line; the schedulers and everything else are untouched. There is no
state to unwind — the chain created only internal records, and those are harmless (and were the
point). No container, image, or trading path is modified by installing or removing this cron.

## Boundary

This schedules a **dry-run / internal-only** lifecycle. It does **not** enable E3, arm a
provider, place a demo or live order, or transmit any notification. Real demo placement (E3) and
real notification transport remain behind their own separate, Nuno-gated packets.
