# Execution Observability (GFX-PKT-OPS-OBSERVABILITY-FOUNDATION)

End-to-end, structured visibility for **every shadow execution attempt** — with a
single correlation id threaded through all 9 lifecycle stages plus lightweight,
log-based metrics. **Additive and fail-open**: it changes no execution, risk, or
trading behaviour and places no order.

## Correlation id

One id per execution attempt, minted at signal receipt and propagated:

```
PendingSignalApproval.correlation_id      (minted in signal_intake.services.intake_parsed)
  → SignalExecutionPlan.correlation_id    (copied in execution.signal_planning; fresh fallback)
  → ExecutionJob.payload["correlation_id"](set in execution.signal_promotion._shadow_payload)
  → worker reads payload["correlation_id"](mt5_trade_ingest_worker.handle_shadow_job)
```

Both new columns are nullable/blank (migrations `signal_intake.0002`,
`execution.0008`) — pre-existing rows keep working; the planner mints a fresh id
if an approval predates the field.

## Lifecycle stages → emission points

| # | stage | emitted by |
|---|-------|-----------|
| 1 | `signal_received`     | `signal_intake/services.py` `intake_parsed` |
| 2 | `parse_complete`      | `signal_intake/services.py` `intake_parsed` |
| 3 | `planning_complete`   | `execution/signal_planning.py` `plan_demo_execution` |
| 4 | `shadow_job_created`  | `execution/signal_promotion.py` `promote_plan_to_shadow_jobs` |
| 5 | `worker_claimed`      | `execution/views.py` `next_job` (server-side) |
| 6 | `order_check_request` | `mt5_trade_ingest_worker.py` `handle_shadow_job` |
| 7 | `order_check_response`| `mt5_trade_ingest_worker.py` `handle_shadow_job` |
| 8 | `validation_outcome`  | `mt5_trade_ingest_worker.py` `handle_shadow_job` |
| 9 | `cleanup_complete`    | `mt5_trade_ingest_worker.py` `handle_shadow_job` |

## Log schema (Loki-ready)

Single-line JSON to two loggers (propagate to the root console handler → stdout →
container logs). Helper: `core/observability.py`.

**`guvfx.execution.lifecycle`**
```json
{"event":"execution_lifecycle","stage":"order_check_response","correlation_id":"<hex>","job_id":57,"retcode":0,"mt5_response_latency_ms":60}
```

**`guvfx.execution.metrics`**
```json
{"event":"execution_metric","metric":"mt5_response_latency","value":60,"unit":"ms","correlation_id":"<hex>"}
```

## Metrics

| metric | where | notes |
|--------|-------|-------|
| `worker_claim_latency` (ms) | `next_job` | `started_at − created_at` |
| `shadow_queue_depth` (jobs) | `next_job` | count of PENDING `PLACE_ORDER_SHADOW` |
| `mt5_response_latency` (ms)  | worker | bridge `/mt5/order_check` round-trip |
| `validation_success` (count) | worker | one per successful validation |
| `validation_failure` (count) | worker | one per failed/refused validation |
| `execution_duration` (ms)    | worker | `now − created_at` at completion |

**Validation success rate** is derived downstream (Grafana/Loki):
`sum(validation_success) / (sum(validation_success)+sum(validation_failure))`.
No metrics backend is added — this is intentional (no speculative infrastructure).

## Deployment

Additive; no behaviour change. Two parts:

1. **Backend** (models/migrations, planning/promotion/views instrumentation):
   rebuild + recreate `guvfx-backend` on the next routine deploy; apply
   `signal_intake.0002` and `execution.0008` (nullable columns — safe, online).
2. **Worker script** (`mt5_trade_ingest_worker.py`): copy to the mounted
   `/srv/guvfx/worker_scripts/` and restart `guvfx-mt5-trade-ingest-worker` /
   `guvfx-mt5-shadow-worker`. The worker imports `core.observability`.

The lifecycle/metric lines appear in the respective container logs immediately.

## Rollback

- **Worker**: restore the prior `mt5_trade_ingest_worker.py` from
  `/srv/guvfx/worker_scripts/*.bak.*` and restart the worker(s).
- **Backend**: redeploy the prior image. The two columns can be left in place
  (unused, harmless) or reverted with `migrate signal_intake 0001` +
  `migrate execution 0007`.
- Observability is fail-open, so it can also be silenced without a rollback by
  raising the log level of `guvfx.execution.*` (e.g. via `DJANGO_LOG_LEVEL`).

## Guarantees

- No change to execution/risk/trading logic; no `order_send`; no new order path.
- All emission is fail-open (`core.observability._emit` swallows exceptions).
- Backwards compatible (nullable columns, payload key addition, fresh-id fallback).
- Verified by `execution/tests_observability.py` (correlation propagation + stage
  and metric emission) and the unchanged shadow dry-run (`verify_shadow_dryrun.sh`).
